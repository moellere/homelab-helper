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
                "action_kind": "start" | "stop" | "shutdown" | "restart" | "migrate" | "cpu-type",
                "target": {"node": "pve1", "vmid": 105, "vm_kind": "qemu" | "lxc",
                           "target_node": "pve2", "online": true,     # migrate only
                           "cpu_type": "x86-64-v3"},                  # cpu-type only
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
ARGOCD_DOMAIN = TrustDomain.CONTAINERS
DNS_DOMAIN = TrustDomain.DNS
POWER_ACTION_KINDS: tuple[str, ...] = ("start", "stop", "shutdown", "restart")
GUEST_ACTION_KINDS: tuple[str, ...] = (*POWER_ACTION_KINDS, "migrate", "cpu-type")
WORKLOAD_ACTION_KINDS: tuple[str, ...] = ("workload-restart", "workload-scale")
ARGOCD_ACTION_KINDS: tuple[str, ...] = ("argocd-sync",)
DNS_ACTION_KINDS: tuple[str, ...] = ("dns-record",)
ACTION_KINDS: tuple[str, ...] = (
    *GUEST_ACTION_KINDS,
    *WORKLOAD_ACTION_KINDS,
    *ARGOCD_ACTION_KINDS,
    *DNS_ACTION_KINDS,
)
DNS_RECORD_TYPES: tuple[str, ...] = ("A", "AAAA", "CNAME", "TXT")
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
    "start",
    "stop",
    "shutdown",
    "restart",
    "migrate",
    "cpu-type",
    "workload-restart",
    "workload-scale",
    "argocd-sync",
    "dns-record",
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
    cpu_type: str | None = Field(default=None, min_length=1, max_length=64)


class WorkloadTarget(BaseModel):
    """A Kubernetes workload. ``replicas`` only means something for ``workload-scale``."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(min_length=1)
    kind: WorkloadKind
    name: str = Field(min_length=1)
    replicas: int | None = Field(default=None, ge=0)


class ArgoAppTarget(BaseModel):
    """An Argo CD application. ``revision`` pins the sync; default is the app's target."""

    model_config = ConfigDict(extra="forbid")

    application: str = Field(min_length=1)
    revision: str | None = Field(default=None, min_length=1)
    prune: bool = False


class DnsRecordTarget(BaseModel):
    """One static DNS record on a UniFi controller (upsert by name + type)."""

    model_config = ConfigDict(extra="forbid")

    hostname: str = Field(min_length=1)
    value: str = Field(min_length=1)
    record_type: Literal["A", "AAAA", "CNAME", "TXT"] = "A"
    ttl: int = Field(default=0, ge=0)
    controller: str | None = Field(default=None, min_length=1)
    """Which UniFi controller (``HOMELAB_HELPER_UNIFI_CONTROLLERS`` name); default = the single one."""


class RollbackSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: bool = False
    strategy: str | None = None


class ActionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: TrustDomain
    action_kind: ActionKind
    target: ActionTarget | WorkloadTarget | ArgoAppTarget | DnsRecordTarget
    hostnames: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _target_fits_kind_and_domain(self) -> ActionSpec:
        expected_domain, kinds, noun = _TARGET_RULES[type(self.target)](self.target)
        if self.domain is not expected_domain:
            raise ValueError(
                f"declared domain {self.domain.value!r} does not match {noun} "
                f"(which is {expected_domain.value!r})"
            )
        if self.action_kind not in kinds:
            raise ValueError(f"{self.action_kind!r} is not an action on {noun}")
        if isinstance(self.target, ActionTarget):
            _check_guest_fields(self.action_kind, self.target)
        elif (
            isinstance(self.target, WorkloadTarget)
            and self.action_kind == "workload-scale"
            and self.target.replicas is None
        ):
            raise ValueError("workload-scale needs target.replicas")
        return self


def _guest_rule(t: ActionTarget) -> tuple[TrustDomain, tuple[str, ...], str]:
    return VM_KIND_DOMAIN[t.vm_kind], GUEST_ACTION_KINDS, f"guest kind {t.vm_kind!r}"


def _workload_rule(t: WorkloadTarget) -> tuple[TrustDomain, tuple[str, ...], str]:
    return WORKLOAD_DOMAIN, WORKLOAD_ACTION_KINDS, "a Kubernetes workload"


def _argocd_rule(t: ArgoAppTarget) -> tuple[TrustDomain, tuple[str, ...], str]:
    return ARGOCD_DOMAIN, ARGOCD_ACTION_KINDS, "an Argo CD application"


def _dns_rule(t: DnsRecordTarget) -> tuple[TrustDomain, tuple[str, ...], str]:
    return DNS_DOMAIN, DNS_ACTION_KINDS, "a DNS record"


_TARGET_RULES: dict[type, Any] = {
    ActionTarget: _guest_rule,
    WorkloadTarget: _workload_rule,
    ArgoAppTarget: _argocd_rule,
    DnsRecordTarget: _dns_rule,
}


def _check_guest_fields(action_kind: str, target: ActionTarget) -> None:
    if action_kind == "migrate":
        if not target.target_node:
            raise ValueError("migrate needs target.target_node")
        if target.target_node == target.node:
            raise ValueError("migrate target_node must differ from the source node")
    if action_kind == "cpu-type":
        if target.vm_kind != "qemu":
            raise ValueError("cpu-type applies to QEMU guests only")
        if not target.cpu_type:
            raise ValueError("cpu-type needs target.cpu_type")


class ActionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["action"]
    action: ActionSpec
    rollback: RollbackSpec = Field(default_factory=RollbackSpec)

    def as_artifact(self) -> dict[str, Any]:
        """The JSON-safe dict for ``ProposalLog.artifact``."""
        return self.model_dump(mode="json", exclude_none=True)


_VARIANT_BY_KEY = (
    ("vmid", "ActionTarget"),
    ("node", "ActionTarget"),
    ("application", "ArgoAppTarget"),
    ("hostname", "DnsRecordTarget"),
    ("namespace", "WorkloadTarget"),
)


def _guess_variant(raw: Any) -> str | None:
    target = (raw.get("action") or {}).get("target") if isinstance(raw, dict) else None
    if not isinstance(target, dict):
        return None
    return next((variant for key, variant in _VARIANT_BY_KEY if key in target), None)


def validate_artifact(raw: Any) -> ActionArtifact:
    """Validate an untrusted dict; :class:`ManifestError` names the first hole.

    The target is a union, so pydantic reports one error per variant; the one
    for the variant the target's own keys point at is the useful one.
    """
    try:
        return ActionArtifact.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors()
        variant = _guess_variant(raw)
        variants = {"ActionTarget", "WorkloadTarget", "ArgoAppTarget", "DnsRecordTarget"}
        preferred = [e for e in errors if not (set(map(str, e["loc"])) & variants)] + [
            e for e in errors if variant and variant in map(str, e["loc"])
        ]
        first = (preferred or errors)[0]
        loc = ".".join(str(p) for p in first["loc"] if str(p) not in variants) or "manifest"
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
    cpu_type: str | None = None,
) -> dict[str, Any]:
    """An executor-ready guest artifact; the domain follows from ``vm_kind``."""
    if vm_kind not in VM_KIND_DOMAIN:
        raise ManifestError(f'vm_kind must be "qemu" or "lxc", not {vm_kind!r}')
    target: dict[str, Any] = {"node": node, "vmid": vmid, "vm_kind": vm_kind}
    if action_kind == "migrate":
        target["target_node"] = target_node
        target["online"] = online
    if action_kind == "cpu-type":
        target["cpu_type"] = cpu_type
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


def build_argocd_artifact(
    *,
    application: str,
    revision: str | None = None,
    prune: bool = False,
    rollback_verified: bool = False,
    rollback_strategy: str | None = None,
) -> dict[str, Any]:
    """An executor-ready Argo CD sync artifact (domain ``containers``)."""
    target: dict[str, Any] = {"application": application, "prune": prune}
    if revision:
        target["revision"] = revision
    artifact = validate_artifact(
        {
            "kind": "action",
            "action": {
                "domain": ARGOCD_DOMAIN.value,
                "action_kind": "argocd-sync",
                "target": target,
                "hostnames": [],
            },
            "rollback": {"verified": rollback_verified, "strategy": rollback_strategy},
        }
    )
    return artifact.as_artifact()


def build_dns_artifact(
    *,
    hostname: str,
    value: str,
    record_type: str = "A",
    ttl: int = 0,
    controller: str | None = None,
    rollback_verified: bool = False,
    rollback_strategy: str | None = None,
) -> dict[str, Any]:
    """An executor-ready static-DNS upsert artifact (domain ``dns``)."""
    target: dict[str, Any] = {
        "hostname": hostname,
        "value": value,
        "record_type": record_type,
        "ttl": ttl,
    }
    if controller:
        target["controller"] = controller
    artifact = validate_artifact(
        {
            "kind": "action",
            "action": {
                "domain": DNS_DOMAIN.value,
                "action_kind": "dns-record",
                "target": target,
                "hostnames": [],
            },
            "rollback": {"verified": rollback_verified, "strategy": rollback_strategy},
        }
    )
    return artifact.as_artifact()


__all__ = [
    "ACTION_KINDS",
    "ARGOCD_ACTION_KINDS",
    "ARGOCD_DOMAIN",
    "BLAST_RADII",
    "DNS_ACTION_KINDS",
    "DNS_DOMAIN",
    "DNS_RECORD_TYPES",
    "GUEST_ACTION_KINDS",
    "POWER_ACTION_KINDS",
    "VM_KIND_DOMAIN",
    "WORKLOAD_ACTION_KINDS",
    "WORKLOAD_DOMAIN",
    "WORKLOAD_KINDS",
    "ActionArtifact",
    "ActionSpec",
    "ActionTarget",
    "ArgoAppTarget",
    "DnsRecordTarget",
    "ManifestError",
    "RollbackSpec",
    "WorkloadTarget",
    "build_argocd_artifact",
    "build_artifact",
    "build_dns_artifact",
    "build_workload_artifact",
    "validate_artifact",
]
