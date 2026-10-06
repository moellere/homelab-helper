"""The ``resize`` action kind: cores/memory through the trust gate, rollback to the prior config."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from homelab_helper.adapters.proxmox import ProxmoxAdapter
from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    AutonomyLevel,
    FindingKind,
    FindingSeverity,
    FindingStatus,
    TrustDomain,
)
from homelab_helper.db.models import ExecutionReceipt, ProposalLog, ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.category_findings import reconcile_category_findings
from homelab_helper.engine.escalation import is_promotable
from homelab_helper.engine.executor import (
    ManifestError,
    execute_proposal,
    parse_manifest,
    rollback_receipt,
)
from homelab_helper.engine.manifest import build_artifact
from homelab_helper.engine.playbooks import playbook_for, run_playbooks
from homelab_helper.engine.rightsizing import guest_issues
from homelab_helper.engine.trust import grant_cell, seed_domains
from tests.test_phase7_execution import _PVE, make_proposal

GIB = 1024**3


@pytest.fixture
async def sessionmaker():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(eng)
    await eng.dispose()


def make_pve(
    requests: list[httpx.Request],
    *,
    config: dict[str, Any] | None = None,
    pending_keys: tuple[str, ...] = ("cores", "memory"),
    maxcpu: int = 8,
    maxmem: int = 32 * GIB,
) -> ProxmoxAdapter:
    """One node; a guest whose config can be read and set, with a pending view."""
    cfg = dict(config or {"cores": 2, "memory": 12288})

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/cluster/resources"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "node",
                            "node": "pve1",
                            "status": "online",
                            "maxcpu": maxcpu,
                            "maxmem": maxmem,
                        }
                    ]
                },
            )
        if path.endswith("/pending"):
            rows = [{"key": k, "value": cfg.get(k), "pending": 99} for k in pending_keys]
            return httpx.Response(200, json={"data": rows})
        if path.endswith("/config") and request.method == "GET":
            return httpx.Response(200, json={"data": cfg})
        if path.endswith("/config"):
            return httpx.Response(200, json={"data": None})
        if path.endswith("/status/current"):
            return httpx.Response(200, json={"data": {"status": "running", "name": "ha"}})
        return httpx.Response(200, json={"data": "UPID:x"})

    client = httpx.AsyncClient(
        base_url=_PVE.url.rstrip("/") + "/api2/json", transport=httpx.MockTransport(handler)
    )
    return ProxmoxAdapter(_PVE, client=client)


async def _confirm(manifest, decision) -> bool:
    return True


# ------------------------------------------------------------------ manifest


def test_resize_manifest_round_trips_and_is_promotable() -> None:
    artifact = build_artifact(action_kind="resize", node="pve1", vmid=105, vm_kind="qemu", cores=3)
    assert artifact["action"]["target"] == {
        "node": "pve1",
        "vmid": 105,
        "vm_kind": "qemu",
        "cores": 3,
    }
    assert is_promotable("resize", "single-host")


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({}, "cores and/or"),
        ({"cores": 0}, "cores"),
        ({"memory_mib": 128}, "memory"),  # QEMU floor is 256
    ],
)
def test_resize_authoring_refuses_holes(kwargs, needle) -> None:
    with pytest.raises(ManifestError, match=needle):
        build_artifact(action_kind="resize", node="pve1", vmid=1, vm_kind="qemu", **kwargs)


async def test_executor_refuses_resize_fields_on_other_kinds(sessionmaker) -> None:
    artifact = build_artifact(action_kind="restart", node="pve1", vmid=1, vm_kind="qemu")
    artifact["action"]["target"]["cores"] = 4  # an agent smuggling a resize into a restart
    async with session_scope(sessionmaker) as s:
        p = await make_proposal(s, artifact, "single-host")
        with pytest.raises(ManifestError, match="only apply to resize"):
            parse_manifest(p)


# ------------------------------------------------------------------ execution


async def test_qemu_resize_is_pending_and_rolls_back_to_prior_values(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_pve(requests)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s, TrustDomain.HYPERVISOR, "resize", "single-host", AutonomyLevel.CONFIRM, actor="op"
        )
        artifact = build_artifact(
            action_kind="resize", node="pve1", vmid=105, vm_kind="qemu", cores=3, memory_mib=8192
        )
        p = await make_proposal(s, artifact, "single-host")
        assert parse_manifest(p).target_label == "qemu/105 on pve1 cores=3 memory=8192MiB"
        result = await execute_proposal(s, p, adapter, actor="op", confirm_cb=_confirm)
        assert result.outcome == "succeeded"
        receipt = (await s.execute(select(ExecutionReceipt))).scalar_one()
        assert receipt.rollback_state["strategy"] == "prior-config"
        assert receipt.rollback_state["verified"] is True
        assert receipt.rollback_state["prior"] == {"cores": 2, "memory": 12288, "balloon": None}
        assert (
            receipt.action["dispatched"] == "set cores=3 memory=8192 (applies at next stop/start)"
        )
        undo = await rollback_receipt(s, receipt, adapter, actor="op")
    await adapter.aclose()
    puts = [dict(r.url.params) for r in requests if r.method == "PUT"]
    assert puts == [
        {"cores": "3", "memory": "8192"},
        {"cores": "2", "memory": "12288", "delete": "balloon"},
    ]
    assert "cores=2" in undo.detail


async def test_container_resize_is_live(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_pve(requests, config={"cores": 2, "memory": 512})
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s, TrustDomain.CONTAINERS, "resize", "single-host", AutonomyLevel.AUTONOMOUS, actor="op"
        )
        p = await make_proposal(
            s,
            build_artifact(action_kind="resize", node="pve1", vmid=109, vm_kind="lxc", cores=1),
            "single-host",
        )
        result = await execute_proposal(s, p, adapter, actor="op")
        receipt = (await s.execute(select(ExecutionReceipt))).scalar_one()
    await adapter.aclose()
    assert result.decision.level is AutonomyLevel.AUTONOMOUS
    assert receipt.action["dispatched"] == "set cores=1 (live)"
    assert receipt.rollback_state["prior"] == {"cores": 2}
    assert not any(r.url.path.endswith("/pending") for r in requests)


async def test_a_balloon_floor_above_the_new_memory_is_lowered_with_it(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_pve(requests, config={"cores": 2, "memory": 8192, "balloon": 6144})
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s, TrustDomain.HYPERVISOR, "resize", "single-host", AutonomyLevel.CONFIRM, actor="op"
        )
        p = await make_proposal(
            s,
            build_artifact(
                action_kind="resize", node="pve1", vmid=105, vm_kind="qemu", memory_mib=4096
            ),
            "single-host",
        )
        await execute_proposal(s, p, adapter, actor="op", confirm_cb=_confirm)
        receipt = (await s.execute(select(ExecutionReceipt))).scalar_one()
    await adapter.aclose()
    put = next(dict(r.url.params) for r in requests if r.method == "PUT")
    assert put == {"memory": "4096", "balloon": "4096"}
    assert receipt.rollback_state["prior"]["balloon"] == 6144


@pytest.mark.parametrize(
    ("kwargs", "needle"), [({"cores": 16}, "CPUs"), ({"memory_mib": 65536}, "physical memory")]
)
async def test_more_than_the_node_has_is_refused_without_writing(
    sessionmaker, kwargs, needle
) -> None:
    requests: list[httpx.Request] = []
    adapter = make_pve(requests, maxcpu=8, maxmem=32 * GIB)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s, TrustDomain.HYPERVISOR, "resize", "single-host", AutonomyLevel.CONFIRM, actor="op"
        )
        p = await make_proposal(
            s,
            build_artifact(action_kind="resize", node="pve1", vmid=105, vm_kind="qemu", **kwargs),
            "single-host",
        )
        result = await execute_proposal(s, p, adapter, actor="op", confirm_cb=_confirm)
    await adapter.aclose()
    assert result.outcome == "failed"
    assert needle in (result.error or "")
    assert not [r for r in requests if r.method == "PUT"]


async def test_resize_executes_nothing_at_propose(sessionmaker) -> None:
    from homelab_helper.engine.executor import ExecutionRefused

    requests: list[httpx.Request] = []
    adapter = make_pve(requests)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        p = await make_proposal(
            s,
            build_artifact(action_kind="resize", node="pve1", vmid=105, vm_kind="qemu", cores=3),
            "single-host",
        )
        with pytest.raises(ExecutionRefused, match="propose"):
            await execute_proposal(s, p, adapter, actor="agent:mcp", confirm_cb=_confirm)
    await adapter.aclose()
    assert requests == []


# ------------------------------------------------------------------- playbook


async def test_rightsize_playbook_drafts_a_resize_but_never_for_idle(sessionmaker) -> None:
    summary = {
        "samples": 720,
        "cpus": 2.0,
        "cpu_p95": 0.62,
        "cpu_peak": 1.04,
        "mem_total": 12 * GIB,
        "mem_p95": 11 * GIB,
        "mem_peak": 11.5 * GIB,
        "net_mean": 100.0,
    }
    issues = guest_issues("lab/105", "ha", "qemu", summary, 30, node="pve1")
    idle = guest_issues(
        "lab/109",
        "esp",
        "lxc",
        {
            **summary,
            "cpu_p95": 0.01,
            "cpu_peak": 0.02,
            "mem_total": GIB,
            "mem_p95": 0.3 * GIB,
            "mem_peak": 0.3 * GIB,
        },
        30,
        node="pve1",
    )
    async with session_scope(sessionmaker) as s:
        await reconcile_category_findings(
            s, FindingKind.RIGHTSIZING, issues + idle, {"cpu-grow", "idle", "cpu-shrink"}
        )
        findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert {f.status for f in findings} == {FindingStatus.OPEN}
        assert all(playbook_for(f) is not None for f in findings)
        result = await run_playbooks(s, min_age=timedelta(0))
        proposals = (await s.execute(select(ProposalLog))).scalars().all()
    by_title = {p.title: p for p in proposals}
    assert "Resize ha: cores 2 → 3" in by_title
    resize = by_title["Resize ha: cores 2 → 3"]
    assert resize.proposed_by == "playbook:rightsize"
    assert resize.artifact["action"]["target"] == {
        "node": "pve1",
        "vmid": 105,
        "vm_kind": "qemu",
        "cores": 3,
    }
    assert not any("esp" in t and "idle" in t for t in by_title)
    assert len(result.drafted) == len(proposals)
    assert {f.severity for f in findings} >= {FindingSeverity.MEDIUM}
