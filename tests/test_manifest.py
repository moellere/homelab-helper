"""The authoring-side manifest schema agrees with the executor's parser."""

from __future__ import annotations

import itertools

import pytest

from homelab_helper.db.base import Base
from homelab_helper.db.enums import TrustDomain
from homelab_helper.db.models import ProposalLog
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.executor import ManifestError as ExecutorManifestError
from homelab_helper.engine.executor import parse_manifest
from homelab_helper.engine.manifest import (
    POWER_ACTION_KINDS,
    VM_KIND_DOMAIN,
    ManifestError,
    build_artifact,
    validate_artifact,
)


def test_executor_reexports_the_same_error_type() -> None:
    assert ExecutorManifestError is ManifestError


@pytest.mark.parametrize(
    ("action_kind", "vm_kind"), list(itertools.product(POWER_ACTION_KINDS, VM_KIND_DOMAIN))
)
async def test_built_artifacts_parse_in_the_executor(action_kind: str, vm_kind: str) -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as s:
            proposal = ProposalLog(
                title="t",
                artifact=build_artifact(
                    action_kind=action_kind, node="pve1", vmid=101, vm_kind=vm_kind
                ),
                blast_radius="single-host",
                proposed_by="agent:mcp",
            )
            s.add(proposal)
            await s.flush()
            manifest = parse_manifest(proposal)
            assert manifest.domain is VM_KIND_DOMAIN[vm_kind]
            assert manifest.action_kind == action_kind
            assert manifest.hostnames == ("pve1",)
            assert manifest.rollback_verified is False
    finally:
        await engine.dispose()


def test_validate_rejects_domain_shopping() -> None:
    raw = build_artifact(action_kind="restart", node="n", vmid=1, vm_kind="qemu")
    raw["action"]["domain"] = TrustDomain.CONTAINERS.value
    with pytest.raises(ManifestError, match="does not match guest kind"):
        validate_artifact(raw)


def test_validate_rejects_unknown_kind_and_extra_keys() -> None:
    with pytest.raises(ManifestError, match="action_kind"):
        build_artifact(action_kind="explode", node="n", vmid=1, vm_kind="lxc")
    with pytest.raises(ManifestError, match="vm_kind"):
        build_artifact(action_kind="stop", node="n", vmid=1, vm_kind="docker")
    raw = build_artifact(action_kind="stop", node="n", vmid=1, vm_kind="lxc")
    raw["action"]["extra"] = "smuggled"
    with pytest.raises(ManifestError, match="extra"):
        validate_artifact(raw)
    with pytest.raises(ManifestError):
        validate_artifact({"kind": "shell-command", "command": "rm -rf /"})


def test_build_carries_hostnames_and_rollback_claim() -> None:
    raw = build_artifact(
        action_kind="restart",
        node="pve1",
        vmid=7,
        vm_kind="lxc",
        hostnames=["pve1", "ct7"],
        rollback_verified=True,
        rollback_strategy="power-state",
    )
    assert raw["action"]["hostnames"] == ["pve1", "ct7"]
    assert raw["rollback"] == {"verified": True, "strategy": "power-state"}
