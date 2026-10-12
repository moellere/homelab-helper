"""``k8s-upgrade`` (Phase 11.2) — a cluster-wide action with no inverse.

What is load-bearing: the manifest is refused at any blast radius but
``cluster``; every preflight (newer by at most one minor, one Talos release
across members, healthy cluster) is read-only and runs before the write; the
receipt only succeeds once the kubelet reports the target and health passes
again; and the kind can never be auto-promoted.
"""

from __future__ import annotations

from typing import Any

import pytest

from homelab_helper.adapters.talos import TalosError
from homelab_helper.db.enums import AutonomyLevel, ProposalOutcome, TrustDomain
from homelab_helper.db.models import ProposalLog
from homelab_helper.db.session import session_scope
from homelab_helper.engine.escalation import REVERSIBLE_ACTION_KINDS, is_promotable
from homelab_helper.engine.executor import execute_proposal, parse_manifest
from homelab_helper.engine.manifest import (
    ManifestError,
    build_k8s_upgrade_artifact,
    validate_artifact,
)
from homelab_helper.engine.rollback import NO_INVERSE, select_strategy
from homelab_helper.engine.trust import grant_cell, seed_domains
from tests.test_node_update import sessionmaker_  # noqa: F401 - fixture

CP = "10.0.6.13"


def proposal_for(version: str = "1.38.0", blast_radius: str = "cluster") -> ProposalLog:
    return ProposalLog(
        title=f"Kubernetes -> {version}",
        artifact=build_k8s_upgrade_artifact(control_plane=CP, version=version),
        blast_radius=blast_radius,
        proposed_by="agent:mcp",
    )


class _FakeTalos:
    def __init__(
        self,
        *,
        current: str = "1.37.1",
        after: str | None = None,
        releases: tuple[str, ...] = ("v1.14.2", "v1.14.2", "v1.14.2"),
        healthy: tuple[bool, ...] = (True, True),
        upgrade_error: str | None = None,
    ) -> None:
        self._versions = [current, after or current]
        self._releases, self._health = releases, list(healthy)
        self._upgrade_error = upgrade_error
        self.upgrades: list[tuple[str, str]] = []
        self.calls: list[str] = []

    async def kubernetes_version(self, node: str, **_: Any) -> str:
        self.calls.append("kubernetes_version")
        return self._versions.pop(0) if len(self._versions) > 1 else self._versions[0]

    async def members(self, node: str, **_: Any) -> list[dict[str, Any]]:
        self.calls.append("members")
        return [
            {
                "hostname": f"n{i}",
                "machine_type": "worker",
                "address": f"10.0.6.{20 + i}",
                "talos": r,
            }
            for i, r in enumerate(self._releases)
        ]

    async def cluster_health(self, node: str, **_: Any) -> tuple[bool, str]:
        self.calls.append("cluster_health")
        ok = self._health.pop(0) if len(self._health) > 1 else self._health[0]
        return ok, "ok" if ok else "waiting for etcd to be healthy: timeout"

    async def upgrade_k8s(self, node: str, version: str, **_: Any) -> str:
        self.calls.append("upgrade_k8s")
        if self._upgrade_error:
            raise TalosError(self._upgrade_error)
        self.upgrades.append((node, version))
        return ""


class _NoProxmox:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Proxmox adapter called: {name}")


async def seeded(session: Any) -> None:
    await seed_domains(session)
    await grant_cell(
        session,
        TrustDomain.CONTAINERS,
        "k8s-upgrade",
        "cluster",
        AutonomyLevel.CONFIRM,
        actor="enoch",
    )
    await session.flush()


async def _yes(manifest: Any, decision: Any) -> bool:
    return True


def test_the_manifest_is_cluster_wide_and_has_no_inverse() -> None:
    raw = build_k8s_upgrade_artifact(control_plane=CP, version="1.38.0")
    validate_artifact(raw)
    assert raw["rollback"] == {"verified": False, "strategy": NO_INVERSE}
    manifest = parse_manifest(proposal_for())
    assert manifest.is_k8s_upgrade
    assert manifest.uses_talosctl
    assert not manifest.is_talos
    assert not manifest.is_node
    assert manifest.cell_key == "containers/k8s-upgrade/cluster"
    assert manifest.target_label == f"kubernetes via {CP} -> 1.38.0"
    assert select_strategy(manifest) == NO_INVERSE


def test_a_narrower_blast_radius_is_refused() -> None:
    with pytest.raises(ManifestError, match="blast radius 'cluster'"):
        parse_manifest(proposal_for(blast_radius="single-host"))


@pytest.mark.parametrize(
    "target",
    [
        {"control_plane": CP, "version": "v1.38.0"},
        {"control_plane": CP, "version": "1.38"},
        {"control_plane": CP, "version": "1.38.0", "image": "ghcr.io/x/kubelet:v1.38.0"},
        {"control_plane": "", "version": "1.38.0"},
    ],
)
def test_bad_targets_are_refused(target: dict[str, Any]) -> None:
    raw = build_k8s_upgrade_artifact(control_plane=CP, version="1.38.0")
    raw["action"]["target"] = target
    with pytest.raises(ManifestError):
        validate_artifact(raw)
    with pytest.raises(ManifestError):
        parse_manifest(ProposalLog(title="x", artifact=raw, blast_radius="cluster"))


def test_k8s_upgrade_can_never_be_auto_promoted() -> None:
    assert "k8s-upgrade" not in REVERSIBLE_ACTION_KINDS
    assert not is_promotable("k8s-upgrade", "cluster")


async def _run(session: Any, talos: _FakeTalos | None, version: str = "1.38.0") -> Any:
    p = proposal_for(version)
    session.add(p)
    await session.flush()
    result = await execute_proposal(
        session,
        p,
        _NoProxmox(),  # type: ignore[arg-type]
        actor="enoch",
        confirm_cb=_yes,
        talos_adapter=talos,  # type: ignore[arg-type]
    )
    return p, result


async def test_a_healthy_cluster_upgrades_and_is_checked_afterwards(sessionmaker_) -> None:  # noqa: F811
    talos = _FakeTalos(current="1.37.1", after="1.38.0")
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p, result = await _run(s, talos)
        assert result.outcome == "succeeded", result.error
        assert p.outcome is ProposalOutcome.USER_ACCEPTED
    assert talos.upgrades == [(CP, "1.38.0")]
    assert talos.calls == [
        "kubernetes_version",
        "members",
        "cluster_health",
        "upgrade_k8s",
        "kubernetes_version",
        "cluster_health",
    ], "every preflight read happens before the one write"


@pytest.mark.parametrize(
    ("talos", "version", "message"),
    [
        (_FakeTalos(current="1.38.0"), "1.38.0", "not newer"),
        (_FakeTalos(current="1.38.1"), "1.38.0", "not newer"),
        (_FakeTalos(current="1.36.2"), "1.38.0", "skips a minor"),
        (_FakeTalos(releases=("v1.14.2", "v1.13.5", "v1.14.2")), "1.38.0", "mixed Talos"),
        (_FakeTalos(healthy=(False,)), "1.38.0", "not healthy before"),
    ],
)
async def test_preflight_failures_refuse_before_the_write(
    sessionmaker_,  # noqa: F811
    talos: _FakeTalos,
    version: str,
    message: str,
) -> None:
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        p, result = await _run(s, talos, version)
        assert result.outcome == "failed"
        assert message in (result.error or "")
        assert p.outcome is ProposalOutcome.PENDING
    assert talos.upgrades == []


async def test_a_kubelet_that_did_not_move_fails_the_receipt(sessionmaker_) -> None:  # noqa: F811
    talos = _FakeTalos(current="1.37.1", after="1.37.1")
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        _, result = await _run(s, talos)
        assert result.outcome == "failed"
        assert "reports 1.37.1, not 1.38.0" in (result.error or "")
    assert talos.upgrades == [(CP, "1.38.0")]


async def test_an_unhealthy_cluster_after_the_upgrade_fails_the_receipt(sessionmaker_) -> None:  # noqa: F811
    talos = _FakeTalos(current="1.37.1", after="1.38.0", healthy=(True, False))
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        _, result = await _run(s, talos)
        assert result.outcome == "failed"
        assert "not healthy after" in (result.error or "")


async def test_a_talosctl_failure_is_a_failed_receipt_not_a_crash(sessionmaker_) -> None:  # noqa: F811
    talos = _FakeTalos(upgrade_error="upgrade-k8s exited 1: context deadline exceeded")
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        _, result = await _run(s, talos)
        assert result.outcome == "failed"
        assert "context deadline" in (result.error or "")


async def test_without_talosctl_nothing_runs(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        await seeded(s)
        _, result = await _run(s, None)
        assert result.outcome == "failed"
        assert "no Talos adapter" in (result.error or "")
