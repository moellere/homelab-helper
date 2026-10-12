"""``node-reboot`` (Phase 10.4) — the second no-inverse action.

What is load-bearing: the dispatch only succeeds once the node is back (online,
quorate, SSH answering) and fails after a timeout rather than the moment the
command returned; an undrained node is refused before any write; and the kind
can never be auto-promoted.
"""

from __future__ import annotations

from typing import Any

import pytest

from homelab_helper.adapters.kernel_ssh import CommandResult
from homelab_helper.db.enums import AutonomyLevel, ProposalOutcome, TrustDomain
from homelab_helper.db.models import Host, ProposalLog
from homelab_helper.db.session import session_scope
from homelab_helper.engine import executor as executor_mod
from homelab_helper.engine.escalation import REVERSIBLE_ACTION_KINDS, is_promotable
from homelab_helper.engine.executor import execute_proposal, parse_manifest
from homelab_helper.engine.manifest import build_node_artifact, validate_artifact
from homelab_helper.engine.rollback import NO_INVERSE, select_strategy
from homelab_helper.engine.trust import grant_cell, seed_domains
from tests.test_node_update import sessionmaker_  # noqa: F401 - fixture

NODE = "bmax2"


def proposal_for(node: str = NODE) -> ProposalLog:
    return ProposalLog(
        title=f"Reboot {node}",
        artifact=build_node_artifact(node=node, action_kind="node-reboot"),
        blast_radius="single-host",
        proposed_by="maintenance:enoch",
    )


def test_reboot_is_a_node_action_with_no_inverse() -> None:
    raw = build_node_artifact(node=NODE, action_kind="node-reboot")
    validate_artifact(raw)
    assert raw["rollback"] == {"verified": False, "strategy": "no-inverse"}
    manifest = parse_manifest(proposal_for())
    assert select_strategy(manifest) == NO_INVERSE
    assert manifest.is_node
    assert manifest.node == NODE
    assert manifest.action_kind == "node-reboot"


def test_node_reboot_can_never_be_auto_promoted() -> None:
    assert "node-reboot" not in REVERSIBLE_ACTION_KINDS
    assert not is_promotable("node-reboot", "single-host")


class _FakeProxmox:
    """Node status is a script: each ``list_nodes`` call pops the next status."""

    def __init__(self, guests: list[dict[str, Any]], statuses: list[str], quorate: bool = True):
        self._guests, self._statuses, self._quorate = guests, list(statuses), quorate
        self.calls: list[str] = []

    async def cluster_resources(self, kind: str) -> list[dict[str, Any]]:
        self.calls.append(f"cluster_resources:{kind}")
        return self._guests

    async def list_nodes(self) -> list[dict[str, Any]]:
        status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        self.calls.append(f"list_nodes:{status}")
        return [{"node": NODE, "status": status}, {"node": "bmax0", "status": "online"}]

    async def cluster_status(self) -> dict[str, Any]:
        return {"name": "bmax", "quorate": self._quorate}

    async def vm_current_status(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}


class _FakeSSH:
    def __init__(self, *, exit_code: int = 0, healthy_after: int = 0) -> None:
        self.reboots: list[dict[str, Any]] = []
        self.health_checks = 0
        self._exit_code, self._healthy_after = exit_code, healthy_after

    async def reboot_node(self, host: str, **kw: Any) -> CommandResult:
        self.reboots.append({"host": host, **kw})
        return CommandResult(
            command="systemctl reboot", stdout="", stderr="", exit_code=self._exit_code
        )

    async def health_check(self, host: str, **kw: Any) -> tuple[bool, str | None]:
        self.health_checks += 1
        if self.health_checks > self._healthy_after:
            return True, None
        return False, "connection refused"


@pytest.fixture
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """No real sleeping: every poll advances a fake clock by the poll interval."""
    clock = [0.0]

    async def _sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(executor_mod.asyncio, "sleep", _sleep)
    monkeypatch.setattr(executor_mod.time, "monotonic", lambda: clock[0])
    return clock


async def seeded(session: Any) -> None:
    await seed_domains(session)
    session.add(
        Host(
            hostname=NODE, primary_ip="10.0.6.22", credentials_ref="ssh:root:/root/.ssh/id_ed25519"
        )
    )
    await grant_cell(
        session,
        TrustDomain.HOST_OS,
        "node-reboot",
        "single-host",
        AutonomyLevel.CONFIRM,
        actor="enoch",
    )
    await session.flush()


async def _yes(manifest: Any, decision: Any) -> bool:
    return True


async def test_a_drained_node_reboots_and_the_dispatch_waits_for_its_return(
    sessionmaker_,  # noqa: F811
    fast_clock,
) -> None:  # noqa: F811
    # online (stale) → offline → offline → online, SSH answers on the second try.
    pve = _FakeProxmox([], ["online", "offline", "offline", "online"])
    ssh = _FakeSSH(healthy_after=1)
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(s, p, pve, actor="enoch", confirm_cb=_yes, ssh_adapter=ssh)
        assert result.outcome == "succeeded", result.error
        assert p.outcome is ProposalOutcome.USER_ACCEPTED
    assert len(ssh.reboots) == 1
    assert ssh.reboots[0]["host"] == "10.0.6.22"
    assert ssh.health_checks == 2
    assert fast_clock[0] >= executor_mod.REBOOT_POLL_S * 2
    assert "list_nodes:offline" in pve.calls, "waited for the node to actually leave"


async def test_a_node_that_never_comes_back_is_a_failed_receipt_not_a_success(
    sessionmaker_,  # noqa: F811
    fast_clock,
) -> None:  # noqa: F811
    pve = _FakeProxmox([], ["offline"])
    ssh = _FakeSSH()
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(s, p, pve, actor="enoch", confirm_cb=_yes, ssh_adapter=ssh)
        assert result.outcome == "failed"
        assert "did not come back" in (result.error or "")
        assert p.outcome is ProposalOutcome.PENDING
    assert len(ssh.reboots) == 1
    assert fast_clock[0] >= executor_mod.REBOOT_TIMEOUT_S


async def test_an_undrained_node_is_refused_before_the_reboot(sessionmaker_, fast_clock) -> None:  # noqa: F811
    running = [{"node": NODE, "vmid": 105, "name": "ha", "status": "running", "template": False}]
    pve = _FakeProxmox(running, ["online"])
    ssh = _FakeSSH()
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(s, p, pve, actor="enoch", confirm_cb=_yes, ssh_adapter=ssh)
        assert result.outcome == "refused"
        assert "drain it first" in (result.error or "")
    assert ssh.reboots == []


async def test_a_reboot_command_that_fails_is_reported(sessionmaker_, fast_clock) -> None:  # noqa: F811
    pve = _FakeProxmox([], ["online"])
    ssh = _FakeSSH(exit_code=1)
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(s, p, pve, actor="enoch", confirm_cb=_yes, ssh_adapter=ssh)
        assert result.outcome == "failed"
        assert "reboot command exited 1" in (result.error or "")
