"""The executable action manifest — one typed schema for ``ProposalLog.artifact``.

The executor validates a manifest by hand at execution time (``parse_manifest``)
because that input is untrusted. This module is the *authoring* side: a
pydantic model that anything drafting a proposal — the MCP ``propose_action``
tool, a planner, a test — validates against before a row is written, and a
:func:`build_artifact` helper that produces the exact shape the executor
accepts. A regression test holds the two in agreement.

Shape::

    {"kind": "action",
     "action": {"domain": "hypervisor" | "containers",
                "action_kind": "start" | "stop" | "shutdown" | "restart",
                "target": {"node": "pve1", "vmid": 105, "vm_kind": "qemu" | "lxc"},
                "hostnames": ["pve1"]},
     "rollback": {"verified": false, "strategy": null}}

The guest kind fixes the trust domain (``lxc`` → containers, ``qemu`` →
hypervisor); a manifest may not claim a softer cell.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from homelab_helper.db.enums import TrustDomain

VM_KIND_DOMAIN: dict[str, TrustDomain] = {
    "lxc": TrustDomain.CONTAINERS,
    "qemu": TrustDomain.HYPERVISOR,
}
POWER_ACTION_KINDS: tuple[str, ...] = ("start", "stop", "shutdown", "restart")
BLAST_RADII: tuple[str, ...] = ("metadata-only", "single-host", "cluster", "site", "everything")

ActionKind = Literal["start", "stop", "shutdown", "restart"]
VMKind = Literal["qemu", "lxc"]


class ManifestError(ValueError):
    """The artifact is not a valid executable action manifest."""


class ActionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str = Field(min_length=1)
    vmid: int = Field(ge=0)
    vm_kind: VMKind


class RollbackSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: bool = False
    strategy: str | None = None


class ActionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: TrustDomain
    action_kind: ActionKind
    target: ActionTarget
    hostnames: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _domain_matches_guest_kind(self) -> ActionSpec:
        expected = VM_KIND_DOMAIN[self.target.vm_kind]
        if self.domain is not expected:
            raise ValueError(
                f"declared domain {self.domain.value!r} does not match guest kind "
                f"{self.target.vm_kind!r} (which is {expected.value!r})"
            )
        return self


class ActionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["action"]
    action: ActionSpec
    rollback: RollbackSpec = Field(default_factory=RollbackSpec)

    def as_artifact(self) -> dict[str, Any]:
        """The JSON-safe dict for ``ProposalLog.artifact``."""
        return self.model_dump(mode="json")


def validate_artifact(raw: Any) -> ActionArtifact:
    """Validate an untrusted dict; :class:`ManifestError` names the first hole."""
    try:
        return ActionArtifact.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "manifest"
        raise ManifestError(f"{loc}: {first['msg']}") from None


def build_artifact(
    *,
    action_kind: str,
    node: str,
    vmid: int,
    vm_kind: str,
    hostnames: tuple[str, ...] | list[str] | None = None,
    rollback_verified: bool = False,
    rollback_strategy: str | None = None,
) -> dict[str, Any]:
    """An executor-ready artifact; the domain follows from ``vm_kind``."""
    if vm_kind not in VM_KIND_DOMAIN:
        raise ManifestError(f'vm_kind must be "qemu" or "lxc", not {vm_kind!r}')
    artifact = validate_artifact(
        {
            "kind": "action",
            "action": {
                "domain": VM_KIND_DOMAIN[vm_kind].value,
                "action_kind": action_kind,
                "target": {"node": node, "vmid": vmid, "vm_kind": vm_kind},
                "hostnames": list(hostnames) if hostnames else [node],
            },
            "rollback": {"verified": rollback_verified, "strategy": rollback_strategy},
        }
    )
    return artifact.as_artifact()


__all__ = [
    "BLAST_RADII",
    "POWER_ACTION_KINDS",
    "VM_KIND_DOMAIN",
    "ActionArtifact",
    "ActionSpec",
    "ActionTarget",
    "ManifestError",
    "RollbackSpec",
    "build_artifact",
    "validate_artifact",
]
