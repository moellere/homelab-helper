"""Talos maintenance runs (Phase 11.3) — the cluster upgrade as one health-gated sequence.

What is load-bearing: control planes are upgraded before workers and members
already on the target are left alone; a closing Kubernetes step is drafted at
blast radius ``cluster``; the run asks ``talosctl health`` before every step
after the first and stops, rejecting the rest, when it fails; and every step is
still an ordinary proposal through the executor on its own cell.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from homelab_helper.adapters import talos as talos_mod
from homelab_helper.adapters.talos import TalosError
from homelab_helper.db.enums import AutonomyLevel, MaintenanceStatus, ProposalOutcome, TrustDomain
from homelab_helper.db.models import ProposalLog
from homelab_helper.db.session import session_scope
from homelab_helper.engine.maintenance import (
    MaintenanceError,
    execute_maintenance,
    plan_talos_maintenance,
    run_summary,
    talos_health_gate,
)
from homelab_helper.engine.trust import grant_cell, seed_domains
from tests.test_node_update import sessionmaker_  # noqa: F401 - fixture

CP = "10.0.6.13"
OLD, NEW = "v1.14.1", "v1.14.2"
MEMBERS = [
    {"hostname": "bmax4", "machine_type": "worker", "address": "10.0.6.24", "talos": OLD},
    {"hostname": "talos-cp2", "machine_type": "controlplane", "address": "10.0.6.14", "talos": OLD},
    {"hostname": "talos-cp1", "machine_type": "controlplane", "address": CP, "talos": OLD},
    {"hostname": "bmax5", "machine_type": "worker", "address": "10.0.6.25", "talos": NEW},
]


class _FakeCluster:
    """Per-node ``version()`` scripts (``None`` = not answering; the last entry sticks)
    and a scripted ``cluster_health``."""

    def __init__(
        self, members: list[dict[str, Any]], *, health: tuple[bool, ...] = (True,)
    ) -> None:
        self._members = members
        self._health = list(health)
        self._scripts: dict[str, list[str | None]] = {
            m["address"]: [m["talos"], m["talos"], m["talos"], None, NEW] for m in members
        }
        self.upgrades: list[str] = []
        self.health_calls = 0

    async def members(self, node: str, **_: Any) -> list[dict[str, Any]]:
        return self._members

    async def version(self, node: str, **_: Any) -> dict[str, str]:
        script = self._scripts[node]
        tag = script.pop(0) if len(script) > 1 else script[0]
        if tag is None:
            raise TalosError(f"{node} did not answer")
        return {"tag": tag}

    async def machine_ready(self, node: str, **_: Any) -> tuple[bool, str]:
        return True, "running"

    async def install_image(self, node: str, **_: Any) -> str:
        return f"factory.talos.dev/installer/abc:{OLD}"

    async def upgrade_node(self, node: str, image: str, **_: Any) -> str:
        self.upgrades.append(node)
        return ""

    async def cluster_health(self, node: str, **_: Any) -> tuple[bool, str]:
        self.health_calls += 1
        ok = self._health.pop(0) if len(self._health) > 1 else self._health[0]
        return ok, "ok" if ok else "waiting for all nodes to be ready: timeout"


class _NoProxmox:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Proxmox adapter called: {name}")


@pytest.fixture
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [0.0]

    async def _sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(talos_mod.asyncio, "sleep", _sleep)
    monkeypatch.setattr(talos_mod.time, "monotonic", lambda: clock[0])
    return clock


async def _grants(s: Any) -> None:
    await seed_domains(s)
    await grant_cell(
        s, TrustDomain.HOST_OS, "talos-upgrade", "single-host", AutonomyLevel.CONFIRM, actor="e"
    )


async def _yes(manifest: Any, decision: Any) -> bool:
    return True


async def test_plan_orders_control_planes_first_and_skips_members_on_the_target(
    sessionmaker_,  # noqa: F811
) -> None:
    cluster = _FakeCluster(MEMBERS)
    async with session_scope(sessionmaker_) as s:
        run = await plan_talos_maintenance(
            s,
            cluster,
            CP,
            NEW,
            actor="enoch",
            kubernetes="1.37.2",  # type: ignore[arg-type]
        )
        assert run.plane == "talos"
        assert run.node == CP
        assert [st["kind"] for st in run.steps] == [
            "talos-upgrade",
            "talos-upgrade",
            "talos-upgrade",
            "k8s-upgrade",
        ]
        assert [st["name"].split(" ")[1] for st in run.steps[:3]] == [
            "talos-cp1",
            "talos-cp2",
            "bmax4",
        ], "control planes first, workers after, bmax5 (already on target) absent"
        proposals = [await s.get(ProposalLog, uuid.UUID(st["proposal_id"])) for st in run.steps]
        assert [p.blast_radius for p in proposals] == ["single-host"] * 3 + ["cluster"]
        assert proposals[0].artifact["action"]["target"] == {"node": CP, "version": NEW}
        assert proposals[0].artifact["action"]["hostnames"] == ["talos-cp1"]
        assert proposals[3].artifact["action"]["target"] == {
            "control_plane": CP,
            "version": "1.37.2",
        }
        assert run_summary(run)["plane"] == "talos"


async def test_plan_refuses_bad_versions_nothing_to_do_and_a_second_open_run(
    sessionmaker_,  # noqa: F811
) -> None:
    async with session_scope(sessionmaker_) as s:
        with pytest.raises(MaintenanceError):
            await plan_talos_maintenance(s, _FakeCluster(MEMBERS), CP, "1.14.2", actor="e")  # type: ignore[arg-type]
        at_old = _FakeCluster([m for m in MEMBERS if m["talos"] == OLD])
        with pytest.raises(MaintenanceError, match="nothing to plan"):
            await plan_talos_maintenance(s, at_old, CP, OLD, actor="e")  # type: ignore[arg-type]
        with pytest.raises(MaintenanceError, match="no cluster members"):
            await plan_talos_maintenance(s, _FakeCluster([]), CP, NEW, actor="e")  # type: ignore[arg-type]
        await plan_talos_maintenance(s, _FakeCluster(MEMBERS), CP, NEW, actor="e")  # type: ignore[arg-type]
        with pytest.raises(MaintenanceError, match="unfinished run"):
            await plan_talos_maintenance(s, _FakeCluster(MEMBERS), CP, NEW, actor="e")  # type: ignore[arg-type]


async def test_run_upgrades_node_by_node_asking_health_between_steps(
    sessionmaker_,  # noqa: F811
    fast_clock,
) -> None:
    cluster = _FakeCluster(MEMBERS)
    async with session_scope(sessionmaker_) as s:
        await _grants(s)
        run = await plan_talos_maintenance(s, cluster, CP, NEW, actor="enoch")  # type: ignore[arg-type]
        run = await execute_maintenance(
            s,
            run,
            _NoProxmox(),  # type: ignore[arg-type]
            actor="enoch",
            confirm_cb=_yes,
            ssh_adapter=None,
            talos_adapter=cluster,  # type: ignore[arg-type]
            health_cb=talos_health_gate(cluster, CP),  # type: ignore[arg-type]
        )
        assert run.status is MaintenanceStatus.SUCCEEDED, run.notes
        assert [st["state"] for st in run.steps] == ["succeeded"] * 3
        assert all(st["receipt_id"] for st in run.steps)
    assert cluster.upgrades == [CP, "10.0.6.14", "10.0.6.24"]
    assert cluster.health_calls == 2, "asked before step 2 and step 3, not before the first"


async def test_an_unhealthy_cluster_stops_the_run_before_the_next_node(
    sessionmaker_,  # noqa: F811
    fast_clock,
) -> None:
    cluster = _FakeCluster(MEMBERS, health=(False,))
    async with session_scope(sessionmaker_) as s:
        await _grants(s)
        run = await plan_talos_maintenance(s, cluster, CP, NEW, actor="enoch")  # type: ignore[arg-type]
        run = await execute_maintenance(
            s,
            run,
            _NoProxmox(),  # type: ignore[arg-type]
            actor="enoch",
            confirm_cb=_yes,
            ssh_adapter=None,
            talos_adapter=cluster,  # type: ignore[arg-type]
            health_cb=talos_health_gate(cluster, CP),  # type: ignore[arg-type]
        )
        assert run.status is MaintenanceStatus.FAILED
        assert [st["state"] for st in run.steps] == ["succeeded", "failed", "skipped"]
        assert "not healthy before this step" in run.steps[1]["detail"]
        assert "timeout" in run.steps[1]["detail"]
        third = await s.get(ProposalLog, uuid.UUID(run.steps[2]["proposal_id"]))
        assert third is not None
        assert third.outcome is ProposalOutcome.USER_REJECTED
    assert cluster.upgrades == [CP], "the second node was never touched"


async def test_a_step_at_propose_refuses_and_ends_the_run(sessionmaker_, fast_clock) -> None:  # noqa: F811
    cluster = _FakeCluster(MEMBERS)
    async with session_scope(sessionmaker_) as s:
        await seed_domains(s)  # no grant: host-os/talos-upgrade sits at PROPOSE
        run = await plan_talos_maintenance(s, cluster, CP, NEW, actor="enoch")  # type: ignore[arg-type]
        run = await execute_maintenance(
            s,
            run,
            _NoProxmox(),  # type: ignore[arg-type]
            actor="enoch",
            confirm_cb=_yes,
            ssh_adapter=None,
            talos_adapter=cluster,  # type: ignore[arg-type]
        )
        assert run.status is MaintenanceStatus.FAILED
        assert run.steps[0]["state"] == "failed"
        assert "propose" in run.steps[0]["detail"].lower()
    assert cluster.upgrades == []
