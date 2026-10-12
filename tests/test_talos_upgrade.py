"""``talos-upgrade`` (Phase 11.1) — the first node action with a verified inverse.

What is load-bearing: the manifest names a release tag and never an image (the
executor derives the image from the node's own); the dispatch only succeeds
once the node is back on the new tag and ready, and fails after the timeout;
the ``prior-image`` verifier reads the live node rather than the manifest; and
``talosctl rollback`` is the tested restore path that earns the kind its place
in ``REVERSIBLE_ACTION_KINDS``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from homelab_helper.adapters import talos as talos_mod
from homelab_helper.adapters.talos import TalosError
from homelab_helper.db.enums import AutonomyLevel, ProposalOutcome, TrustDomain
from homelab_helper.db.models import ExecutionReceipt, ProposalLog
from homelab_helper.db.session import session_scope
from homelab_helper.engine.escalation import REVERSIBLE_ACTION_KINDS, is_promotable
from homelab_helper.engine.executor import execute_proposal, parse_manifest, rollback_receipt
from homelab_helper.engine.manifest import ManifestError, build_talos_artifact, validate_artifact
from homelab_helper.engine.rollback import PRIOR_IMAGE, RollbackError, select_strategy
from homelab_helper.engine.trust import grant_cell, seed_domains
from tests.test_node_update import sessionmaker_  # noqa: F401 - fixture

NODE = "10.0.6.13"
IMAGE = "factory.talos.dev/installer/ce4c9805:v1.14.1"
OLD, NEW = "v1.14.1", "v1.14.2"


def proposal_for(version: str = NEW) -> ProposalLog:
    return ProposalLog(
        title=f"Upgrade {NODE} to {version}",
        artifact=build_talos_artifact(node=NODE, version=version, hostnames=("talos-cp1",)),
        blast_radius="single-host",
        proposed_by="agent:mcp",
    )


class _FakeTalos:
    """``version()`` follows a script: each call pops the next tag, ``None`` is a node that
    does not answer, and the last entry sticks."""

    def __init__(
        self, versions: list[str | None], *, ready: bool = True, image: str = IMAGE
    ) -> None:
        self._versions, self._ready, self._image = list(versions), ready, image
        self.upgrades: list[tuple[str, str]] = []
        self.rollbacks: list[str] = []
        self.version_calls = 0

    async def version(self, node: str, **_: Any) -> dict[str, str]:
        self.version_calls += 1
        tag = self._versions.pop(0) if len(self._versions) > 1 else self._versions[0]
        if tag is None:
            raise TalosError(f"{node} did not answer")
        return {"tag": tag, "arch": "amd64", "os_arch": "linux/amd64"}

    async def machine_ready(self, node: str, **_: Any) -> tuple[bool, str]:
        return self._ready, "running" if self._ready else "booting"

    async def install_image(self, node: str, **_: Any) -> str:
        return self._image

    async def upgrade_node(self, node: str, image: str, **_: Any) -> str:
        self.upgrades.append((node, image))
        return ""

    async def rollback_node(self, node: str, **_: Any) -> str:
        self.rollbacks.append(node)
        return ""


class _NoProxmox:
    """A Talos action never touches Proxmox; any call here is a bug."""

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


async def seeded(session: Any, level: AutonomyLevel = AutonomyLevel.AUTONOMOUS) -> None:
    await seed_domains(session)
    await grant_cell(
        session, TrustDomain.HOST_OS, "talos-upgrade", "single-host", level, actor="enoch"
    )
    await session.flush()


async def _yes(manifest: Any, decision: Any) -> bool:
    return True


def test_the_manifest_names_a_release_and_nothing_else() -> None:
    raw = build_talos_artifact(node=NODE, version=NEW)
    validate_artifact(raw)
    assert raw["action"]["target"] == {"node": NODE, "version": NEW}
    assert raw["rollback"] == {"verified": False, "strategy": PRIOR_IMAGE}
    manifest = parse_manifest(proposal_for())
    assert manifest.is_talos
    assert not manifest.is_node
    assert manifest.cell_key == "host-os/talos-upgrade/single-host"
    assert manifest.hostnames == ("talos-cp1",)
    assert manifest.target_label == f"talos node {NODE} -> {NEW}"
    assert select_strategy(manifest) == PRIOR_IMAGE


@pytest.mark.parametrize(
    "target",
    [
        {"node": NODE, "version": NEW, "image": "ghcr.io/evil/installer:v1.14.2"},
        {"node": NODE, "version": "1.14.2"},
        {"node": NODE, "version": "v1.14"},
        {"node": NODE, "version": "latest"},
    ],
)
def test_an_image_or_a_non_release_version_is_refused(target: dict[str, Any]) -> None:
    raw = build_talos_artifact(node=NODE, version=NEW)
    raw["action"]["target"] = target
    with pytest.raises(ManifestError):
        validate_artifact(raw)
    with pytest.raises(ManifestError):
        parse_manifest(ProposalLog(title="x", artifact=raw, blast_radius="single-host"))


def test_a_talos_target_cannot_borrow_the_hypervisor_domain() -> None:
    raw = build_talos_artifact(node=NODE, version=NEW)
    raw["action"]["domain"] = "hypervisor"
    with pytest.raises(ManifestError):
        validate_artifact(raw)
    with pytest.raises(ManifestError):
        parse_manifest(ProposalLog(title="x", artifact=raw, blast_radius="single-host"))


def test_talos_upgrade_can_earn_autonomy() -> None:
    assert "talos-upgrade" in REVERSIBLE_ACTION_KINDS
    assert is_promotable("talos-upgrade", "single-host")


async def test_an_upgrade_waits_for_the_node_to_return_on_the_new_tag(
    sessionmaker_,  # noqa: F811
    fast_clock,
) -> None:
    # verify → preflight → still old → gone → gone → back on the new tag.
    talos = _FakeTalos([OLD, OLD, OLD, None, None, NEW])
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s,
            p,
            _NoProxmox(),
            actor="enoch",
            talos_adapter=talos,  # type: ignore[arg-type]
        )
        assert result.outcome == "succeeded", result.error
        assert p.outcome is ProposalOutcome.USER_ACCEPTED
        receipt = (
            await s.execute(select(ExecutionReceipt).where(ExecutionReceipt.proposal_id == p.id))
        ).scalar_one()
        assert receipt.decision_level is AutonomyLevel.AUTONOMOUS, "verified inverse, no ask"
        assert receipt.rollback_state["strategy"] == PRIOR_IMAGE
        assert receipt.rollback_state["prior"]["prior_tag"] == OLD
        assert receipt.action["dispatched"] == "upgrade"
    assert talos.upgrades == [(NODE, "factory.talos.dev/installer/ce4c9805:v1.14.2")]
    assert fast_clock[0] >= talos_mod.RETURN_POLL_S * 2, "polled while the node was away"


async def test_a_node_that_never_comes_back_is_a_failed_receipt(
    sessionmaker_,  # noqa: F811
    fast_clock,
) -> None:
    talos = _FakeTalos([OLD, OLD, None])
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s,
            p,
            _NoProxmox(),
            actor="enoch",
            talos_adapter=talos,  # type: ignore[arg-type]
        )
        assert result.outcome == "failed"
        assert "did not come back" in (result.error or "")
        assert p.outcome is ProposalOutcome.PENDING
    assert len(talos.upgrades) == 1
    assert fast_clock[0] >= talos_mod.RETURN_TIMEOUT_S


async def test_a_node_already_on_the_tag_is_unverified_and_refused(
    sessionmaker_,  # noqa: F811
    fast_clock,
) -> None:
    talos = _FakeTalos([NEW])
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s,
            p,
            _NoProxmox(),  # type: ignore[arg-type]
            actor="enoch",
            confirm_cb=_yes,
            talos_adapter=talos,
        )
        assert result.outcome == "failed"
        assert "already runs" in (result.error or "")
        receipt = (
            await s.execute(select(ExecutionReceipt).where(ExecutionReceipt.proposal_id == p.id))
        ).scalar_one()
        assert receipt.decision_level is AutonomyLevel.CONFIRM, "nothing to undo → degraded"
        assert receipt.rollback_state["verified"] is False
    assert talos.upgrades == []


async def test_without_talosctl_nothing_is_verified_and_nothing_runs(
    sessionmaker_,  # noqa: F811
) -> None:
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s,
            p,
            _NoProxmox(),
            actor="enoch",
            confirm_cb=_yes,  # type: ignore[arg-type]
        )
        assert result.outcome == "failed"
        assert "no Talos adapter" in (result.error or "")


async def test_a_node_that_is_not_ready_is_refused_before_the_write(
    sessionmaker_,  # noqa: F811
    fast_clock,
) -> None:
    talos = _FakeTalos([OLD], ready=False)
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s,
            p,
            _NoProxmox(),  # type: ignore[arg-type]
            actor="enoch",
            confirm_cb=_yes,
            talos_adapter=talos,
        )
        assert result.outcome == "failed"
        assert "not ready" in (result.error or "")
    assert talos.upgrades == []


async def test_rollback_boots_the_prior_release_and_waits_for_it(
    sessionmaker_,  # noqa: F811
    fast_clock,
) -> None:
    talos = _FakeTalos([OLD, OLD, OLD, None, NEW])
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p = proposal_for()
        s.add(p)
        await s.flush()
        result = await execute_proposal(
            s,
            p,
            _NoProxmox(),
            actor="enoch",
            talos_adapter=talos,  # type: ignore[arg-type]
        )
        assert result.outcome == "succeeded", result.error
        receipt = (
            await s.execute(select(ExecutionReceipt).where(ExecutionReceipt.proposal_id == p.id))
        ).scalar_one()

        undo_talos = _FakeTalos([NEW, None, OLD])
        with pytest.raises(RollbackError, match="no Talos adapter"):
            await rollback_receipt(s, receipt, _NoProxmox(), actor="enoch")  # type: ignore[arg-type]
        undone = await rollback_receipt(
            s,
            receipt,
            _NoProxmox(),  # type: ignore[arg-type]
            actor="enoch",
            talos_adapter=undo_talos,
        )
        assert undo_talos.rollbacks == [NODE]
        assert f"back to {OLD}" in undone.detail
        assert receipt.rolled_back_at is not None
