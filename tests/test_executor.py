"""Executor gate tests — MockTransport only, no live Proxmox anywhere.

The load-bearing assertions: BLOCK/PROPOSE decisions never reach the adapter
and never write a receipt; CONFIRM dispatches only after consent; rollback
state is captured before dispatch; every dispatch (success or failure) writes
exactly one receipt; and the executor module never imports the LLM package.
"""

from __future__ import annotations

import subprocess
import sys

import httpx
import pytest
from sqlalchemy import select

from homelab_helper.adapters.proxmox import ProxmoxAdapter, ProxmoxConfig
from homelab_helper.db.base import Base
from homelab_helper.db.enums import AutonomyLevel, ProposalOutcome, TrustDomain
from homelab_helper.db.models import ExecutionReceipt, ProposalLog
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.executor import (
    ExecutionRefused,
    ManifestError,
    execute_proposal,
    parse_manifest,
)
from homelab_helper.engine.trust import grant_cell, seed_domains

_CONFIG = ProxmoxConfig(url="https://pve.test:8006", token_id="t@pam!x", token_secret="s")


def make_adapter(requests: list[httpx.Request], *, power_status: int = 200) -> ProxmoxAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/status/current"):
            return httpx.Response(
                200, json={"data": {"status": "running", "name": "web01", "uptime": 4242}}
            )
        if power_status >= 400:
            return httpx.Response(power_status, json={"errors": {"boom": "nope"}})
        return httpx.Response(200, json={"data": "UPID:pve1:0000:reboot"})

    client = httpx.AsyncClient(
        base_url=_CONFIG.url.rstrip("/") + "/api2/json",
        transport=httpx.MockTransport(handler),
    )
    return ProxmoxAdapter(_CONFIG, client=client)


def make_artifact(
    *,
    domain: str = "containers",
    vm_kind: str = "lxc",
    action_kind: str = "restart",
    rollback_verified: bool = False,
) -> dict:
    return {
        "kind": "action",
        "action": {
            "domain": domain,
            "action_kind": action_kind,
            "target": {"cluster": "homelab", "node": "pve1", "vmid": 101, "vm_kind": vm_kind},
            "hostnames": ["pve1"],
        },
        "rollback": {"verified": rollback_verified, "strategy": "prior-power-state"},
    }


@pytest.fixture
async def engine():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine):
    return make_sessionmaker(engine)


async def make_proposal(session, artifact: dict) -> ProposalLog:
    proposal = ProposalLog(
        title="Restart web01 (lxc/101)",
        artifact=artifact,
        blast_radius="single-host",
        proposed_by="agent:planner",
    )
    session.add(proposal)
    await session.flush()
    return proposal


# ---------------------------------------------------------------------------
# Manifest parsing — untrusted input
# ---------------------------------------------------------------------------


async def test_parse_manifest_happy_path(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        p = await make_proposal(s, make_artifact())
        m = parse_manifest(p)
        assert m.domain is TrustDomain.CONTAINERS
        assert m.cell_key == "containers/restart/single-host"
        assert m.target_label == "lxc/101 on pve1"
        assert m.hostnames == ("pve1",)


async def test_parse_manifest_rejects_non_action_artifact(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        p = await make_proposal(s, {"kind": "shopping-list", "items": []})
        with pytest.raises(ManifestError, match="not executable"):
            parse_manifest(p)


async def test_parse_manifest_rejects_domain_shopping(sessionmaker) -> None:
    """A qemu guest declared under the containers domain is refused outright."""
    async with session_scope(sessionmaker) as s:
        p = await make_proposal(s, make_artifact(domain="containers", vm_kind="qemu"))
        with pytest.raises(ManifestError, match="does not match guest kind"):
            parse_manifest(p)


async def test_parse_manifest_rejects_unknown_action_kind(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        p = await make_proposal(s, make_artifact(action_kind="delete-everything"))
        with pytest.raises(ManifestError, match="not supported"):
            parse_manifest(p)


async def test_parse_manifest_rejects_missing_target(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        artifact = make_artifact()
        del artifact["action"]["target"]
        p = await make_proposal(s, artifact)
        with pytest.raises(ManifestError, match="target"):
            parse_manifest(p)


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


async def test_propose_decision_never_reaches_adapter(sessionmaker) -> None:
    """No grant ⇒ PROPOSE ⇒ refused: zero HTTP calls, zero receipts, PENDING."""
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        p = await make_proposal(s, make_artifact())
        with pytest.raises(ExecutionRefused, match="propose"):
            await execute_proposal(s, p, adapter, actor="enoch")
        assert requests == []
        assert (await s.execute(select(ExecutionReceipt))).scalars().all() == []
        assert p.outcome is ProposalOutcome.PENDING
    await adapter.aclose()


async def test_block_decision_never_reaches_adapter(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.BLOCK,
            actor="enoch",
        )
        p = await make_proposal(s, make_artifact())
        with pytest.raises(ExecutionRefused, match="block"):
            await execute_proposal(s, p, adapter, actor="enoch")
        assert requests == []
    await adapter.aclose()


async def test_confirm_flow_dispatches_after_consent(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    seen: list[str] = []

    async def confirm(manifest, decision) -> bool:
        seen.append(decision.level.value)
        return True

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.CONFIRM,
            actor="enoch",
        )
        p = await make_proposal(s, make_artifact())
        result = await execute_proposal(s, p, adapter, actor="enoch", confirm_cb=confirm)

        assert seen == ["confirm"]
        assert result.outcome == "succeeded"
        paths = [r.url.path for r in requests]
        assert paths == [
            "/api2/json/nodes/pve1/lxc/101/status/current",
            "/api2/json/nodes/pve1/lxc/101/status/reboot",
        ], "rollback capture must precede dispatch, restart maps to reboot"
        assert requests[1].method == "POST"

        receipt = (await s.execute(select(ExecutionReceipt))).scalar_one()
        assert receipt.decision_level is AutonomyLevel.CONFIRM
        assert receipt.outcome == "succeeded"
        assert receipt.rollback_state["prior"]["status"] == "running"
        assert receipt.action["upid"] == "UPID:pve1:0000:reboot"
        assert any("cell containers/restart/single-host" in r for r in receipt.decision_reasons)
        assert p.outcome is ProposalOutcome.USER_ACCEPTED
        assert p.outcome_by == "enoch"
    await adapter.aclose()


async def test_confirm_declined_leaves_proposal_pending(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)

    async def decline(manifest, decision) -> bool:
        return False

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.CONFIRM,
            actor="enoch",
        )
        p = await make_proposal(s, make_artifact())
        with pytest.raises(ExecutionRefused, match="declined"):
            await execute_proposal(s, p, adapter, actor="enoch", confirm_cb=decline)
        assert requests == []
        assert (await s.execute(select(ExecutionReceipt))).scalars().all() == []
        assert p.outcome is ProposalOutcome.PENDING
    await adapter.aclose()


async def test_confirm_without_callback_refuses(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.CONFIRM,
            actor="enoch",
        )
        p = await make_proposal(s, make_artifact())
        with pytest.raises(ExecutionRefused, match="no confirmer"):
            await execute_proposal(s, p, adapter, actor="enoch")
        assert requests == []
    await adapter.aclose()


async def test_autonomous_without_verified_rollback_degrades_to_confirm(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )
        p = await make_proposal(s, make_artifact(rollback_verified=False))
        with pytest.raises(ExecutionRefused, match="no confirmer"):
            await execute_proposal(s, p, adapter, actor="enoch")
        assert requests == []
    await adapter.aclose()


async def test_autonomous_with_verified_rollback_runs_without_callback(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )
        p = await make_proposal(s, make_artifact(rollback_verified=True))
        result = await execute_proposal(s, p, adapter, actor="enoch")
        assert result.outcome == "succeeded"
        assert result.decision.level is AutonomyLevel.AUTONOMOUS
        assert len(requests) == 2
    await adapter.aclose()


async def test_failed_dispatch_writes_failed_receipt_and_keeps_pending(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests, power_status=500)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )
        p = await make_proposal(s, make_artifact(rollback_verified=True))
        result = await execute_proposal(s, p, adapter, actor="enoch")
        assert result.outcome == "failed"
        assert result.error is not None
        assert "500" in result.error
        receipt = (await s.execute(select(ExecutionReceipt))).scalar_one()
        assert receipt.outcome == "failed"
        assert receipt.rollback_state["prior"]["status"] == "running"
        assert p.outcome is ProposalOutcome.PENDING, "failed dispatch stays retryable"
    await adapter.aclose()


async def test_non_pending_proposal_refused(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        p = await make_proposal(s, make_artifact())
        p.outcome = ProposalOutcome.USER_REJECTED
        with pytest.raises(ExecutionRefused, match="not pending"):
            await execute_proposal(s, p, adapter, actor="enoch")
        assert requests == []
    await adapter.aclose()


async def test_qemu_guest_uses_hypervisor_domain_cell(sessionmaker) -> None:
    """The same verbs on a qemu guest key into the hypervisor domain."""
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )
        p = await make_proposal(
            s, make_artifact(domain="hypervisor", vm_kind="qemu", rollback_verified=True)
        )
        with pytest.raises(ExecutionRefused, match="hypervisor/restart/single-host"):
            await execute_proposal(s, p, adapter, actor="enoch")
        assert requests == [], "the containers grant must not leak to hypervisor actions"
    await adapter.aclose()


def test_executor_path_never_imports_llm() -> None:
    code = (
        "import sys; import homelab_helper.engine.executor; "
        "bad = [m for m in sys.modules if m.startswith('homelab_helper.llm')]; "
        "assert not bad, f'LLM modules in the execution path: {bad}'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
