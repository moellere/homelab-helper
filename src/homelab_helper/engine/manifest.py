"""The executable action manifest — one typed schema for ``ProposalLog.artifact``.

The executor validates a manifest by hand at execution time (``parse_manifest``)
because that input is untrusted. This module is the *authoring* side: a
pydantic model that anything drafting a proposal — the MCP ``propose_action``
tools, a planner, a test — validates against before a row is written, and the
:func:`build_artifact` / :func:`build_workload_artifact` helpers that produce
the exact shape the executor accepts. A regression test holds the two in
agreement.

Two target shapes share the envelope::

    {"kind": "action",
     "action": {"domain": "hypervisor" | "containers",
                "action_kind": "start" | "stop" | "shutdown" | "restart" | "migrate",
                "target": {"node": "pve1", "vmid": 105, "vm_kind": "qemu" | "lxc",
                           "target_node": "pve2", "online": true},   # migrate only
                "hostnames": ["pve1"]},
     "rollback": {"verified": false, "strategy": null}}

    {"kind": "action",
     "action": {"domain": "containers",
                "action_kind": "workload-restart" | "workload-scale",
                "target": {"namespace": "media", "kind": "deployment",
                           "name": "jellyfin", "replicas": 2},           # scale only
                "hostnames": []},
     "rollback": {"verified": false, "strategy": null}}

The target fixes the trust domain (``lxc`` → containers, ``qemu`` →
hypervisor, a Kubernetes workload → containers); a manifest may not claim a
softer cell.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from homelab_helper.db.enums import TrustDomain

VM_KIND_DOMAIN: dict[str, TrustDomain] = {
    "lxc": TrustDomain.CONTAINERS,
    "qemu": TrustDomain.HYPERVISOR,
}
WORKLOAD_DOMAIN = TrustDomain.CONTAINERS
POWER_ACTION_KINDS: tuple[str, ...] = ("start", "stop", "shutdown", "restart")
GUEST_ACTION_KINDS: tuple[str, ...] = (*POWER_ACTION_KINDS, "migrate")
WORKLOAD_ACTION_KINDS: tuple[str, ...] = ("workload-restart", "workload-scale")
ACTION_KINDS: tuple[str, ...] = (*GUEST_ACTION_KINDS, *WORKLOAD_ACTION_KINDS)
WORKLOAD_KINDS: tuple[str, ...] = ("deployment", "statefulset", "daemonset")
BLAST_RADII: tuple[str, ...] = (
    "metadata-only",
    "single-service",
    "single-host",
    "cluster",
    "site",
    "everything",
)

ActionKind = Literal[
    "start", "stop", "shutdown", "restart", "migrate", "workload-restart", "workload-scale"
]
VMKind = Literal["qemu", "lxc"]
WorkloadKind = Literal["deployment", "statefulset", "daemonset"]


class ManifestError(ValueError):
    """The artifact is not a valid executable action manifest."""


class ActionTarget(BaseModel):
    """A Proxmox guest. ``target_node`` / ``online`` only mean something for ``migrate``."""

    model_config = ConfigDict(extra="forbid")

    node: str = Field(min_length=1)
    vmid: int = Field(ge=0)
    vm_kind: VMKind
    target_node: str | None = Field(default=None, min_length=1)
    online: bool | None = None


class WorkloadTarget(BaseModel):
    """A Kubernetes workload. ``replicas`` only means something for ``workload-scale``."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(min_length=1)
    kind: WorkloadKind
    name: str = Field(min_length=1)
    replicas: int | None = Field(default=None, ge=0)


class RollbackSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: bool = False
    strategy: str | None = None


class ActionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: TrustDomain
    action_kind: ActionKind
    target: ActionTarget | WorkloadTarget
    hostnames: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _target_fits_kind_and_domain(self) -> ActionSpec:
        if isinstance(self.target, ActionTarget):
            expected = VM_KIND_DOMAIN[self.target.vm_kind]
            if self.domain is not expected:
                raise ValueError(
                    f"declared domain {self.domain.value!r} does not match guest kind "
                    f"{self.target.vm_kind!r} (which is {expected.value!r})"
                )
            if self.action_kind not in GUEST_ACTION_KINDS:
                raise ValueError(f"{self.action_kind!r} is not a guest action")
            if self.action_kind == "migrate" and not self.target.target_node:
                raise ValueError("migrate needs target.target_node")
            if self.action_kind == "migrate" and self.target.target_node == self.target.node:
                raise ValueError("migrate target_node must differ from the source node")
        else:
            if self.domain is not WORKLOAD_DOMAIN:
                raise ValueError(
                    f"declared domain {self.domain.value!r} does not match a Kubernetes "
                    f"workload (which is {WORKLOAD_DOMAIN.value!r})"
                )
            if self.action_kind not in WORKLOAD_ACTION_KINDS:
                raise ValueError(f"{self.action_kind!r} is not a workload action")
            if self.action_kind == "workload-scale" and self.target.replicas is None:
                raise ValueError("workload-scale needs target.replicas")
        return self


class ActionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["action"]
    action: ActionSpec
    rollback: RollbackSpec = Field(default_factory=RollbackSpec)

    def as_artifact(self) -> dict[str, Any]:
        """The JSON-safe dict for ``ProposalLog.artifact``."""
        return self.model_dump(mode="json", exclude_none=True)


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
    target_node: str | None = None,
    online: bool = True,
) -> dict[str, Any]:
    """An executor-ready guest artifact; the domain follows from ``vm_kind``."""
    if vm_kind not in VM_KIND_DOMAIN:
        raise ManifestError(f'vm_kind must be "qemu" or "lxc", not {vm_kind!r}')
    target: dict[str, Any] = {"node": node, "vmid": vmid, "vm_kind": vm_kind}
    if action_kind == "migrate":
        target["target_node"] = target_node
        target["online"] = online
    hosts = list(hostnames) if hostnames else [node]
    if action_kind == "migrate" and target_node and target_node not in hosts:
        hosts.append(target_node)
    artifact = validate_artifact(
        {
            "kind": "action",
            "action": {
                "domain": VM_KIND_DOMAIN[vm_kind].value,
                "action_kind": action_kind,
                "target": target,
                "hostnames": hosts,
            },
            "rollback": {"verified": rollback_verified, "strategy": rollback_strategy},
        }
    )
    return artifact.as_artifact()


def build_workload_artifact(
    *,
    action_kind: str,
    namespace: str,
    kind: str,
    name: str,
    replicas: int | None = None,
    hostnames: tuple[str, ...] | list[str] | None = None,
    rollback_verified: bool = False,
    rollback_strategy: str | None = None,
) -> dict[str, Any]:
    """An executor-ready Kubernetes workload artifact (domain ``containers``)."""
    target: dict[str, Any] = {"namespace": namespace, "kind": kind, "name": name}
    if action_kind == "workload-scale":
        target["replicas"] = replicas
    artifact = validate_artifact(
        {
            "kind": "action",
            "action": {
                "domain": WORKLOAD_DOMAIN.value,
                "action_kind": action_kind,
                "target": target,
                "hostnames": list(hostnames) if hostnames else [],
            },
            "rollback": {"verified": rollback_verified, "strategy": rollback_strategy},
        }
    )
    return artifact.as_artifact()


__all__ = [
    "ACTION_KINDS",
    "BLAST_RADII",
    "GUEST_ACTION_KINDS",
    "POWER_ACTION_KINDS",
    "VM_KIND_DOMAIN",
    "WORKLOAD_ACTION_KINDS",
    "WORKLOAD_DOMAIN",
    "WORKLOAD_KINDS",
    "ActionArtifact",
    "ActionSpec",
    "ActionTarget",
    "ManifestError",
    "RollbackSpec",
    "WorkloadTarget",
    "build_artifact",
    "build_workload_artifact",
    "validate_artifact",
]
