"""Executor — the single enforcement point between ``decide()`` and any write.

Phase 6 PR B. The contract, per ``docs/architecture.md``:

- Input is a **pending** ``ProposalLog`` whose ``artifact`` is an action
  manifest (``{"kind": "action", ...}``). An LLM may have *drafted* the
  manifest; this module treats it as untrusted data — parsed, validated, and
  cross-checked (the declared trust domain must match the guest kind, so a
  manifest can't shop for a softer cell).
- Authorization is ``engine.trust.decide()`` over a DB-loaded context —
  deterministic, no LLM anywhere on the path (regression-tested).
- BLOCK and PROPOSE never dispatch and never write a receipt: absence of a
  receipt means nothing executed. CONFIRM dispatches only after the operator
  callback consents; declining leaves the proposal PENDING and untouched.
- Reversibility is **verified, not claimed**: ``engine/rollback.py`` probes the
  target, and that finding — not the manifest's own ``rollback.verified`` flag
  — is what ``decide()`` sees. The gate runs twice for this: pessimistically
  first (assuming no rollback), so a refused action never touches the target
  even to probe it, then again with the finding, which can only raise the
  outcome. Capture (which may take a snapshot) happens only after the action
  is authorized, and lands in the receipt either way.
- Every dispatch — success or failure — writes exactly one
  ``ExecutionReceipt``. Success also closes the proposal (USER_ACCEPTED);
  failure leaves it PENDING so it can be retried.
- A per-action :class:`OverrideGrant` crosses the soft-hard floors for one
  action, owner-only and interactively obtained. It is logged as a distinct
  ``TrustHistory`` event — but only when it actually changed the decided
  level, so the audit spine records authority changes rather than gestures.
- Every dispatch feeds the cell's trust record (``engine/escalation.py``):
  success extends the clean streak, failure demotes the cell to PROPOSE and
  flags probation. The feedback is one-way — escalation writes levels that a
  *later* ``decide()`` reads; it never influences the decision in flight.

Write surfaces (Phase 7 widened them): Proxmox guest power
(start | stop | shutdown | restart) and migrate, and Kubernetes workloads
(workload-restart | workload-scale). AC2's ``containers/restart/single-host``
cell remains the canonical first cell. At CONFIRM the operator's "yes" may come
from the CLI prompt or from an :mod:`engine.approval` channel (a phone tap);
either way it is one human's answer to one action, and a channel's answer is
written to ``TrustHistory`` as an ``approval`` event.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homelab_helper.adapters.argocd import ArgoCDAPIError
from homelab_helper.adapters.kubernetes import KubeError
from homelab_helper.adapters.proxmox import ProxmoxAPIError
from homelab_helper.adapters.unifi import UniFiAPIError
from homelab_helper.db.enums import AutonomyLevel, ProposalOutcome, TrustDomain
from homelab_helper.db.models import ExecutionReceipt, ProposalLog, TrustHistory
from homelab_helper.engine.approval import ApprovalResult
from homelab_helper.engine.escalation import (
    EscalationResult,
    record_bad_outcome,
    record_clean_outcome,
)
from homelab_helper.engine.manifest import (
    ACTION_KINDS,
    ARGOCD_ACTION_KINDS,
    ARGOCD_DOMAIN,
    DNS_ACTION_KINDS,
    DNS_DOMAIN,
    DNS_RECORD_TYPES,
    GUEST_ACTION_KINDS,
    MAX_CORES,
    MAX_MEMORY_MIB,
    MIN_MEMORY_MIB,
    VM_KIND_DOMAIN,
    WORKLOAD_ACTION_KINDS,
    WORKLOAD_DOMAIN,
    WORKLOAD_KINDS,
    ManifestError,
)
from homelab_helper.engine.notify import ExecutionNotice, Notifier, notify_after_run
from homelab_helper.engine.rollback import (
    RollbackError,
    RollbackPlan,
    capture_rollback,
    restore,
    verify_rollback,
)
from homelab_helper.engine.trust import (
    ActionRequest,
    Decision,
    TrustContext,
    decide,
    load_trust_context,
    window_is_open,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.adapters.argocd import ArgoCDAdapter
    from homelab_helper.adapters.kubernetes import K8sAdapter
    from homelab_helper.adapters.proxmox import ProxmoxAdapter
    from homelab_helper.adapters.unifi import UniFiAdapter

    ConfirmCallback = Callable[["ActionManifest", Decision], Awaitable["bool | ApprovalResult"]]

# Trust-vocabulary verbs → Proxmox API verbs.
_POWER_DISPATCH = {"start": "start", "stop": "stop", "shutdown": "shutdown", "restart": "reboot"}

# The guest kind fixes the trust domain; a manifest may not claim otherwise
# (shared with the authoring-side schema in engine/manifest.py).
_VM_KIND_DOMAIN = VM_KIND_DOMAIN


class ExecutionRefused(RuntimeError):
    """The gate (or the operator) said no — nothing was dispatched."""

    def __init__(self, message: str, decision: Decision | None = None) -> None:
        super().__init__(message)
        self.decision = decision


@dataclass(frozen=True)
class ActionManifest:
    """The validated, executable core of a ``ProposalLog.artifact``."""

    domain: TrustDomain
    action_kind: str
    blast_radius: str
    hostnames: tuple[str, ...]
    rollback_verified: bool
    rollback_strategy: str | None
    action_raw: dict[str, Any]
    node: str | None = None
    vmid: int | None = None
    vm_kind: str | None = None
    target_node: str | None = None
    online: bool | None = None
    cpu_type: str | None = None
    cores: int | None = None
    memory_mib: int | None = None
    namespace: str | None = None
    workload_kind: str | None = None
    workload_name: str | None = None
    replicas: int | None = None
    application: str | None = None
    revision: str | None = None
    prune: bool = False
    dns_hostname: str | None = None
    dns_value: str | None = None
    record_type: str | None = None
    ttl: int = 0
    controller: str | None = None

    @property
    def cell_key(self) -> str:
        return f"{self.domain.value}/{self.action_kind}/{self.blast_radius}"

    @property
    def is_workload(self) -> bool:
        return self.workload_name is not None

    @property
    def is_argocd(self) -> bool:
        return self.application is not None

    @property
    def is_dns(self) -> bool:
        return self.dns_hostname is not None

    @property
    def target_label(self) -> str:
        if self.is_argocd:
            rev = f" @ {self.revision}" if self.revision else ""
            return f"argocd app {self.application}{rev}"
        if self.is_dns:
            where = f" on {self.controller}" if self.controller else ""
            return f"dns {self.record_type} {self.dns_hostname} -> {self.dns_value}{where}"
        if self.is_workload:
            return f"{self.workload_kind}/{self.workload_name} in {self.namespace}"
        label = f"{self.vm_kind}/{self.vmid} on {self.node}"
        if self.target_node:
            return f"{label} -> {self.target_node}"
        if self.action_kind == "resize":
            parts = [f"cores={self.cores}"] if self.cores is not None else []
            if self.memory_mib is not None:
                parts.append(f"memory={self.memory_mib}MiB")
            return f"{label} {' '.join(parts)}"
        return f"{label} cpu={self.cpu_type}" if self.cpu_type else label


@dataclass(frozen=True)
class OverrideGrant:
    """One operator's "I accept this" for a single action.

    Constructed only by an interactive caller — never loaded from the DB, never
    derivable from state — so no agent and no autonomous run can reach it. It
    crosses the *soft-hard* floors (the unverified-rollback degrade, a
    non-absolute host ceiling) for exactly one action, and nothing else:
    absolute floors ignore it, and it never turns a BLOCK or PROPOSE cell into
    an executing one.
    """

    reason: str
    actor: str


@dataclass(frozen=True)
class ExecutionResult:
    receipt_id: uuid.UUID
    decision: Decision
    outcome: str
    error: str | None
    duration_ms: int
    escalation: EscalationResult | None = None
    """What this outcome did to the cell's floor — see engine/escalation.py."""
    override_used: bool = False
    """True only when the override actually changed the decided level."""
    notification: str | None = None
    """What the post-run notifier did: ``None`` when the run did not warrant one."""


def parse_manifest(proposal: ProposalLog) -> ActionManifest:
    """Validate ``proposal.artifact`` into an :class:`ActionManifest`.

    Everything here is untrusted input — an LLM may have drafted it. Raises
    :class:`ManifestError` with an operator-readable reason on any hole.
    """
    artifact = proposal.artifact or {}
    if artifact.get("kind") != "action":
        raise ManifestError(
            f'artifact kind {artifact.get("kind")!r} is not executable (expected "action")'
        )
    action = artifact.get("action")
    if not isinstance(action, dict):
        raise ManifestError('manifest has no "action" object')

    action_kind = action.get("action_kind")
    if action_kind not in ACTION_KINDS:
        allowed = ", ".join(sorted(ACTION_KINDS))
        raise ManifestError(f"action_kind {action_kind!r} is not supported (allowed: {allowed})")

    target = action.get("target")
    if not isinstance(target, dict):
        raise ManifestError('manifest action has no "target" object')
    declared = str(action.get("domain"))
    try:
        domain = TrustDomain(declared)
    except ValueError:
        raise ManifestError(f"unknown trust domain {declared!r}") from None

    rollback = artifact.get("rollback") or {}
    if not isinstance(rollback, dict):
        raise ManifestError('"rollback" must be an object when present')
    common: dict[str, Any] = {
        "domain": domain,
        "action_kind": action_kind,
        "blast_radius": proposal.blast_radius,
        "rollback_verified": bool(rollback.get("verified")),
        "rollback_strategy": rollback.get("strategy"),
        "action_raw": action,
    }

    if "vmid" in target or "node" in target:
        return _parse_guest_target(action, target, domain, common)
    if "application" in target:
        return _parse_argocd_target(action, target, domain, common)
    if "hostname" in target and "value" in target:
        return _parse_dns_target(action, target, domain, common)
    return _parse_workload_target(action, target, domain, common)


def _parse_argocd_target(
    action: dict[str, Any], target: dict[str, Any], domain: TrustDomain, common: dict[str, Any]
) -> ActionManifest:
    if common["action_kind"] not in ARGOCD_ACTION_KINDS:
        raise ManifestError(f"{common['action_kind']!r} is not an Argo CD action")
    application = target.get("application")
    if not application or not isinstance(application, str):
        raise ManifestError("target.application is required")
    if domain is not ARGOCD_DOMAIN:
        raise ManifestError(
            f"declared domain {domain.value!r} does not match an Argo CD application "
            f"(which is {ARGOCD_DOMAIN.value!r}) — refusing"
        )
    revision = target.get("revision")
    return ActionManifest(
        **common,
        hostnames=tuple(action.get("hostnames") or ()),
        application=application,
        revision=revision if isinstance(revision, str) and revision else None,
        prune=bool(target.get("prune", False)),
    )


def _parse_dns_target(
    action: dict[str, Any], target: dict[str, Any], domain: TrustDomain, common: dict[str, Any]
) -> ActionManifest:
    if common["action_kind"] not in DNS_ACTION_KINDS:
        raise ManifestError(f"{common['action_kind']!r} is not a DNS action")
    hostname, value = target.get("hostname"), target.get("value")
    if not hostname or not isinstance(hostname, str):
        raise ManifestError("target.hostname is required")
    if not value or not isinstance(value, str):
        raise ManifestError("target.value is required")
    record_type = target.get("record_type") or "A"
    if record_type not in DNS_RECORD_TYPES:
        raise ManifestError(f"target.record_type must be one of {', '.join(DNS_RECORD_TYPES)}")
    if domain is not DNS_DOMAIN:
        raise ManifestError(
            f"declared domain {domain.value!r} does not match a DNS record "
            f"(which is {DNS_DOMAIN.value!r}) — refusing"
        )
    ttl = target.get("ttl", 0)
    if not isinstance(ttl, int) or ttl < 0:
        raise ManifestError("target.ttl must be a non-negative integer")
    controller = target.get("controller")
    return ActionManifest(
        **common,
        hostnames=tuple(action.get("hostnames") or ()),
        dns_hostname=hostname,
        dns_value=value,
        record_type=record_type,
        ttl=ttl,
        controller=controller if isinstance(controller, str) and controller else None,
    )


def _bounded_int(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _parse_resize_fields(
    action_kind: str, target: dict[str, Any], vm_kind: str
) -> tuple[int | None, int | None]:
    cores, memory_mib = target.get("cores"), target.get("memory_mib")
    if action_kind != "resize":
        if cores is not None or memory_mib is not None:
            raise ManifestError("target.cores / target.memory_mib only apply to resize")
        return None, None
    if cores is None and memory_mib is None:
        raise ManifestError("resize needs target.cores and/or target.memory_mib")
    if cores is not None and not _bounded_int(cores, 1, MAX_CORES):
        raise ManifestError(f"target.cores must be an integer from 1 to {MAX_CORES}")
    floor = MIN_MEMORY_MIB[vm_kind]
    if memory_mib is not None and not _bounded_int(memory_mib, floor, MAX_MEMORY_MIB):
        raise ManifestError(
            f"target.memory_mib must be an integer from {floor} to {MAX_MEMORY_MIB} for {vm_kind}"
        )
    return cores, memory_mib


def _parse_guest_target(
    action: dict[str, Any], target: dict[str, Any], domain: TrustDomain, common: dict[str, Any]
) -> ActionManifest:
    action_kind = common["action_kind"]
    if action_kind not in GUEST_ACTION_KINDS:
        raise ManifestError(f"{action_kind!r} is not a guest action")
    node = target.get("node")
    vmid = target.get("vmid")
    vm_kind = target.get("vm_kind")
    if not node or not isinstance(node, str):
        raise ManifestError("target.node is required")
    if not isinstance(vmid, int):
        raise ManifestError("target.vmid must be an integer")
    if vm_kind not in _VM_KIND_DOMAIN:
        raise ManifestError(f'target.vm_kind must be "qemu" or "lxc", not {vm_kind!r}')
    expected_domain = _VM_KIND_DOMAIN[vm_kind]
    if domain is not expected_domain:
        raise ManifestError(
            f"declared domain {domain.value!r} does not match guest kind {vm_kind!r} "
            f"(which is {expected_domain.value!r}) — refusing"
        )
    target_node = target.get("target_node")
    if action_kind == "migrate":
        if not target_node or not isinstance(target_node, str):
            raise ManifestError("target.target_node is required for migrate")
        if target_node == node:
            raise ManifestError("target.target_node must differ from target.node")
    cpu_type = target.get("cpu_type")
    if action_kind == "cpu-type":
        if vm_kind != "qemu":
            raise ManifestError("cpu-type applies to QEMU guests only")
        if not cpu_type or not isinstance(cpu_type, str):
            raise ManifestError("target.cpu_type is required for cpu-type")
    cores, memory_mib = _parse_resize_fields(action_kind, target, vm_kind)
    online = target.get("online")
    return ActionManifest(
        **common,
        hostnames=tuple(action.get("hostnames") or (node,)),
        node=node,
        vmid=vmid,
        vm_kind=vm_kind,
        target_node=target_node if action_kind == "migrate" else None,
        online=bool(online) if online is not None else None,
        cpu_type=cpu_type if action_kind == "cpu-type" else None,
        cores=cores,
        memory_mib=memory_mib,
    )


def _parse_workload_target(
    action: dict[str, Any], target: dict[str, Any], domain: TrustDomain, common: dict[str, Any]
) -> ActionManifest:
    action_kind = common["action_kind"]
    if action_kind not in WORKLOAD_ACTION_KINDS:
        raise ManifestError(f"{action_kind!r} needs a guest target (node, vmid, vm_kind)")
    namespace = target.get("namespace")
    kind = target.get("kind")
    name = target.get("name")
    if not namespace or not isinstance(namespace, str):
        raise ManifestError("target.namespace is required")
    if kind not in WORKLOAD_KINDS:
        raise ManifestError(f"target.kind must be one of {', '.join(WORKLOAD_KINDS)}, not {kind!r}")
    if not name or not isinstance(name, str):
        raise ManifestError("target.name is required")
    if domain is not WORKLOAD_DOMAIN:
        raise ManifestError(
            f"declared domain {domain.value!r} does not match a Kubernetes workload "
            f"(which is {WORKLOAD_DOMAIN.value!r}) — refusing"
        )
    replicas = target.get("replicas")
    if action_kind == "workload-scale" and (not isinstance(replicas, int) or replicas < 0):
        raise ManifestError("target.replicas must be a non-negative integer for workload-scale")
    return ActionManifest(
        **common,
        hostnames=tuple(action.get("hostnames") or ()),
        namespace=namespace,
        workload_kind=kind,
        workload_name=name,
        replicas=replicas if action_kind == "workload-scale" else None,
    )


async def _dispatch_argocd(
    manifest: ActionManifest, argocd: ArgoCDAdapter | None
) -> tuple[str, Any]:
    if argocd is None:
        raise ArgoCDAPIError(0, "no Argo CD adapter is configured", method="POST", path="sync")
    if manifest.application is None:
        raise ManifestError("argocd target is incomplete")
    await argocd.sync_application(
        manifest.application, revision=manifest.revision, prune=manifest.prune
    )
    return f"sync{' @ ' + manifest.revision if manifest.revision else ''}", None


async def _dispatch_dns(manifest: ActionManifest, unifi: UniFiAdapter | None) -> tuple[str, Any]:
    if unifi is None:
        raise UniFiAPIError(0, "no UniFi adapter is configured", method="POST", path="static-dns")
    if manifest.dns_hostname is None or manifest.dns_value is None:
        raise ManifestError("dns target is incomplete")
    rtype = manifest.record_type or "A"
    existing = await unifi.find_dns_record(manifest.dns_hostname, rtype)
    if existing and existing.get("id"):
        await unifi.update_dns_record(
            str(existing["id"]),
            manifest.dns_hostname,
            manifest.dns_value,
            record_type=rtype,
            ttl=manifest.ttl,
        )
        return f"update {rtype} record", existing["id"]
    created = await unifi.create_dns_record(
        manifest.dns_hostname, manifest.dns_value, record_type=rtype, ttl=manifest.ttl
    )
    return f"create {rtype} record", created.get("id")


async def _dispatch_workload(manifest: ActionManifest, k8s: K8sAdapter | None) -> tuple[str, Any]:
    if k8s is None:
        raise KubeError("no Kubernetes adapter is configured")
    ns, kind, name = manifest.namespace, manifest.workload_kind, manifest.workload_name
    if ns is None or kind is None or name is None:
        raise ManifestError("workload target is incomplete")
    if manifest.action_kind == "workload-restart":
        return "rollout restart", await k8s.rollout_restart(ns, kind, name)
    if manifest.replicas is None:
        raise ManifestError("workload-scale has no replica count")
    return f"scale --replicas={manifest.replicas}", await k8s.scale_workload(
        ns, kind, name, manifest.replicas
    )


async def _dispatch_resize(
    manifest: ActionManifest, adapter: ProxmoxAdapter, node: str, vmid: int, vm_kind: str
) -> tuple[str, Any]:
    """Set cores and/or memory, refusing more than the guest's node physically has.

    A QEMU guest whose balloon floor sits above the new memory gets the floor
    lowered with it (Proxmox rejects ``balloon > memory``). QEMU without CPU or
    memory hotplug stores the change as pending until the guest's next
    stop/start; the returned verb says which happened. Containers apply live.
    """
    host = next((n for n in await adapter.list_nodes() if str(n.get("node")) == node), None)
    if host is None:
        raise ValueError(f"node {node} is not in the cluster")
    if manifest.cores is not None and host.get("maxcpu") and manifest.cores > int(host["maxcpu"]):
        raise ValueError(f"{manifest.cores} cores exceeds {node}'s {host['maxcpu']} CPUs")
    mib = 1024**2
    if (
        manifest.memory_mib is not None
        and host.get("maxmem")
        and manifest.memory_mib * mib > int(host["maxmem"])
    ):
        raise ValueError(f"{manifest.memory_mib} MiB exceeds {node}'s physical memory")
    options: dict[str, Any] = {}
    if manifest.cores is not None:
        options["cores"] = manifest.cores
    if manifest.memory_mib is not None:
        options["memory"] = manifest.memory_mib
        if vm_kind == "qemu":
            balloon = (await adapter.vm_config(node, vmid, vm_kind)).get("balloon")
            if balloon is not None and 0 < int(balloon) > manifest.memory_mib:
                options["balloon"] = manifest.memory_mib
    await adapter.set_vm_config(node, vmid, vm_kind, **options)
    change = " ".join(f"{k}={v}" for k, v in options.items())
    if vm_kind == "qemu":
        pending = await adapter.vm_config(node, vmid, vm_kind, pending=True)
        if any("pending" in (pending.get(k) or {}) for k in options):
            return f"set {change} (applies at next stop/start)", None
    return f"set {change} (live)", None


async def _dispatch_guest(manifest: ActionManifest, adapter: ProxmoxAdapter) -> tuple[str, Any]:
    node, vmid, vm_kind = manifest.node, manifest.vmid, manifest.vm_kind
    if node is None or vmid is None or vm_kind is None:
        raise ManifestError("guest target is incomplete")
    if manifest.action_kind == "cpu-type":
        if manifest.cpu_type is None:
            raise ManifestError("cpu-type has no cpu_type")
        await adapter.set_vm_config(node, vmid, vm_kind, cpu=manifest.cpu_type)
        return f"set cpu={manifest.cpu_type} (applies at next stop/start)", None
    if manifest.action_kind == "resize":
        return await _dispatch_resize(manifest, adapter, node, vmid, vm_kind)
    if manifest.action_kind == "migrate":
        if manifest.target_node is None:
            raise ManifestError("migrate has no target node")
        online = manifest.online if manifest.online is not None else True
        return f"migrate -> {manifest.target_node}", await adapter.migrate_guest(
            node, vmid, vm_kind, manifest.target_node, online=online
        )
    verb = _POWER_DISPATCH[manifest.action_kind]
    return verb, await adapter.vm_power(node, vmid, vm_kind, verb)


async def _dispatch(
    manifest: ActionManifest,
    adapter: ProxmoxAdapter,
    k8s: K8sAdapter | None,
    argocd: ArgoCDAdapter | None = None,
    unifi: UniFiAdapter | None = None,
) -> tuple[str, Any]:
    """Run the one adapter write the manifest names; returns ``(verb, task-ish detail)``."""
    if manifest.is_argocd:
        return await _dispatch_argocd(manifest, argocd)
    if manifest.is_dns:
        return await _dispatch_dns(manifest, unifi)
    if manifest.is_workload:
        return await _dispatch_workload(manifest, k8s)
    return await _dispatch_guest(manifest, adapter)


async def _log_override(
    session: AsyncSession,
    override: OverrideGrant | None,
    *,
    action: ActionRequest,
    context: TrustContext,
    decision: Decision,
    manifest: ActionManifest,
    proposal: ProposalLog,
    actor: str,
) -> bool:
    """Record an override as a distinct authority event — but only when it
    actually changed the outcome. An override that bought nothing is worth
    telling the operator about; it is not an authority change, and the audit
    spine should not fill up with gestures that did nothing.
    """
    if override is None:
        return False
    without = decide(action, replace(context, override=False))
    if without.level is decision.level:
        return False
    session.add(
        TrustHistory(
            actor=actor,
            event="override",
            domain=manifest.domain,
            proposal_id=proposal.id,
            detail={
                "cell": manifest.cell_key,
                "reason": override.reason,
                "without_override": without.level.value,
                "with_override": decision.level.value,
            },
        )
    )
    await session.flush()
    return True


async def _confirm(
    session: AsyncSession,
    confirm_cb: ConfirmCallback | None,
    manifest: ActionManifest,
    decision: Decision,
    *,
    proposal: ProposalLog,
    actor: str,
) -> ApprovalResult | None:
    """One human's "yes" for this one action, from the CLI prompt or an approval channel.

    A channel's answer (an :class:`ApprovalResult`) is written to ``TrustHistory``
    as an ``approval`` event either way, so the audit spine shows who said yes or
    no and over which path. Anything but a yes raises :class:`ExecutionRefused`.
    """
    if confirm_cb is None:
        raise ExecutionRefused(
            "decision requires operator confirmation and no confirmer is available", decision
        )
    answer = await confirm_cb(manifest, decision)
    if isinstance(answer, ApprovalResult):
        session.add(
            TrustHistory(
                actor=actor,
                event="approval",
                domain=manifest.domain,
                proposal_id=proposal.id,
                detail={
                    "cell": manifest.cell_key,
                    "approved": answer.approved,
                    "channel": answer.channel,
                    "responder": answer.responder,
                    **answer.detail,
                },
            )
        )
        await session.flush()
        if not answer.approved:
            raise ExecutionRefused(f"{answer.summary} — proposal left pending", decision)
        return answer
    if not answer:
        raise ExecutionRefused("operator declined — proposal left pending", decision)
    return None


async def execute_proposal(
    session: AsyncSession,
    proposal: ProposalLog,
    adapter: ProxmoxAdapter,
    *,
    actor: str,
    confirm_cb: ConfirmCallback | None = None,
    override: OverrideGrant | None = None,
    k8s_adapter: K8sAdapter | None = None,
    argocd_adapter: ArgoCDAdapter | None = None,
    unifi_adapter: UniFiAdapter | None = None,
    notifier: Notifier | None = None,
) -> ExecutionResult:
    """Gate, (maybe) confirm, dispatch, and receipt one pending action proposal.

    Raises :class:`ManifestError` on an invalid artifact and
    :class:`ExecutionRefused` whenever nothing may run (BLOCK/PROPOSE decision,
    missing or declined confirmation, non-pending proposal). A dispatch
    failure does *not* raise — it returns a ``failed`` result whose receipt
    carries the error.
    """
    if proposal.outcome is not ProposalOutcome.PENDING:
        raise ExecutionRefused(f"proposal is {proposal.outcome.value}, not pending — refusing")

    manifest = parse_manifest(proposal)

    def _request(rollback_verified: bool) -> ActionRequest:
        return ActionRequest(
            domain=manifest.domain,
            action_kind=manifest.action_kind,
            blast_radius=manifest.blast_radius,
            hostnames=manifest.hostnames,
            rollback_verified=rollback_verified,
            provenance=proposal.proposed_by,
        )

    # Decide pessimistically first, assuming no rollback. Verification can only
    # ever *raise* the outcome (it removes the AUTONOMOUS→CONFIRM degrade and
    # nothing else), so a BLOCK or PROPOSE here is final — and refusing now
    # means a forbidden action never touches the target at all, not even to
    # probe it.
    context = await load_trust_context(session, _request(False))
    if override is not None:
        context = replace(context, override=True)
    provisional = decide(_request(False), context)
    if provisional.level in (AutonomyLevel.BLOCK, AutonomyLevel.PROPOSE):
        raise ExecutionRefused(
            f"decision is {provisional.level.value} for cell {manifest.cell_key} — not dispatching",
            provisional,
        )

    # Authorized in some form, so it is worth asking the target whether this is
    # undoable. Read-only, and deliberately not the manifest's to assert: a
    # proposal may *request* a rollback strategy but may not certify one.
    verification = await verify_rollback(
        adapter, manifest, k8s=k8s_adapter, argocd=argocd_adapter, unifi=unifi_adapter
    )
    action = _request(verification.verified)
    decision = decide(action, context)

    # Log the override only when it actually changed the outcome. An override
    # that bought nothing is worth telling the operator about, but it is not
    # an authority change and should not clutter the audit spine.
    override_was_load_bearing = await _log_override(
        session,
        override,
        action=action,
        context=context,
        decision=decision,
        manifest=manifest,
        proposal=proposal,
        actor=actor,
    )
    approval: ApprovalResult | None = None
    if decision.level is AutonomyLevel.CONFIRM:
        approval = await _confirm(
            session, confirm_cb, manifest, decision, proposal=proposal, actor=actor
        )

    # The kill switch's checkpoint. A window can be revoked between the
    # decision and the dispatch — including by an operator watching this run
    # go wrong — so a decision that leaned on one is re-tested here, at the
    # last moment before anything changes.
    if decision.window_id is not None and not await window_is_open(session, decision.window_id):
        raise ExecutionRefused(
            f"elevation window {decision.window_id} closed before dispatch — halting",
            decision,
        )

    # Capture only now: taking a snapshot is itself a write, so it must not
    # happen while the gate is still deciding. Under a window the floor was
    # lifted without a verified rollback, so the architecture asks for a
    # best-effort snapshot anyway.
    plan = await capture_rollback(
        adapter,
        manifest,
        verification,
        best_effort=decision.window_id is not None,
    )
    rollback_state = plan.as_receipt_state()

    started = time.monotonic()
    outcome, error, upid = "succeeded", None, None
    verb = manifest.action_kind
    try:
        verb, upid = await _dispatch(manifest, adapter, k8s_adapter, argocd_adapter, unifi_adapter)
    except (ProxmoxAPIError, KubeError, ArgoCDAPIError, UniFiAPIError, OSError, ValueError) as exc:
        outcome, error = "failed", str(exc)
    duration_ms = int((time.monotonic() - started) * 1000)

    receipt = ExecutionReceipt(
        proposal_id=proposal.id,
        actor=actor,
        decision_level=decision.level,
        decision_reasons=list(decision.reasons),
        window_id=uuid.UUID(decision.window_id) if decision.window_id else None,
        action={**manifest.action_raw, "dispatched": verb},
        rollback_state=rollback_state,
        outcome=outcome,
        error=error,
        duration_ms=duration_ms,
        approval=(
            {
                "channel": approval.channel,
                "responder": approval.responder,
                "approved": approval.approved,
                **approval.detail,
            }
            if approval is not None
            else None
        ),
    )
    if upid is not None:
        receipt.action = {**receipt.action, "upid": upid}
    session.add(receipt)
    await session.flush()

    if outcome == "succeeded":
        proposal.outcome = ProposalOutcome.USER_ACCEPTED
        proposal.outcome_at = datetime.now(UTC)
        proposal.outcome_by = actor
        proposal.outcome_notes = f"executed at {decision.level.value}; receipt {receipt.id}"
        escalation = await record_clean_outcome(
            session,
            domain=manifest.domain,
            action_kind=manifest.action_kind,
            blast_radius=manifest.blast_radius,
            actor=actor,
            proposal_id=proposal.id,
        )
    else:
        escalation = await record_bad_outcome(
            session,
            domain=manifest.domain,
            action_kind=manifest.action_kind,
            blast_radius=manifest.blast_radius,
            actor=actor,
            reason=error or "dispatch failed",
            proposal_id=proposal.id,
        )
    await session.flush()

    # Only now, with the receipt and the escalation on disk, is the operator
    # told about a run they were not asked about. A lost notification changes
    # nothing above this line.
    notification = await notify_after_run(
        notifier,
        ExecutionNotice(
            receipt_id=receipt.id,
            proposal_id=proposal.id,
            title=proposal.title,
            cell=manifest.cell_key,
            target=manifest.target_label,
            level=decision.level,
            outcome=outcome,
            error=error,
            duration_ms=duration_ms,
            actor=actor,
            rollback_available=plan.verified and plan.capture_error is None,
            escalation=escalation,
        ),
    )

    return ExecutionResult(
        receipt_id=receipt.id,
        decision=decision,
        outcome=outcome,
        error=error,
        duration_ms=duration_ms,
        escalation=escalation,
        override_used=override_was_load_bearing,
        notification=notification,
    )


@dataclass(frozen=True)
class RollbackResult:
    receipt_id: uuid.UUID
    """The new receipt recording the undo, not the receipt being undone."""
    detail: str
    duration_ms: int


async def rollback_receipt(
    session: AsyncSession,
    receipt: ExecutionReceipt,
    adapter: ProxmoxAdapter,
    *,
    actor: str,
    k8s_adapter: K8sAdapter | None = None,
    argocd_adapter: ArgoCDAdapter | None = None,
    unifi_adapter: UniFiAdapter | None = None,
) -> RollbackResult:
    """Undo one executed action, using the state captured before it ran.

    Deliberately **not** gated by ``decide()``. The gradient governs what the
    framework may do on its own; this is the operator saying "put it back",
    and a safety valve that could be locked shut by the same policy that let
    the action through is not a safety valve. It is still fully recorded: the
    undo writes its own receipt, and the original is marked rolled back.

    Raises :class:`RollbackError` when the receipt cannot be undone — the
    action failed, it was already rolled back, or its captured state is too
    old to carry a restore path.
    """
    if receipt.outcome != "succeeded":
        raise RollbackError(f"receipt is {receipt.outcome}, so there is nothing to undo")
    if receipt.rolled_back_at is not None:
        raise RollbackError(
            f"receipt was already rolled back at {receipt.rolled_back_at:%Y-%m-%d %H:%M}"
        )

    plan = RollbackPlan.from_receipt_state(receipt.rollback_state or {})

    started = time.monotonic()
    detail = await restore(
        adapter, plan, k8s=k8s_adapter, argocd=argocd_adapter, unifi=unifi_adapter
    )
    duration_ms = int((time.monotonic() - started) * 1000)

    undo = ExecutionReceipt(
        proposal_id=receipt.proposal_id,
        actor=actor,
        decision_level=receipt.decision_level,
        decision_reasons=[f"operator rollback of receipt {receipt.id}"],
        window_id=receipt.window_id,
        action={"kind": "rollback", "of_receipt": str(receipt.id), "strategy": plan.strategy},
        rollback_state=plan.as_receipt_state(),
        outcome="succeeded",
        error=None,
        duration_ms=duration_ms,
    )
    session.add(undo)
    await session.flush()

    receipt.rolled_back_at = datetime.now(UTC)
    receipt.rollback_receipt_id = undo.id
    await session.flush()

    return RollbackResult(receipt_id=undo.id, detail=detail, duration_ms=duration_ms)


__all__ = [
    "ActionManifest",
    "ExecutionRefused",
    "ExecutionResult",
    "ManifestError",
    "OverrideGrant",
    "RollbackResult",
    "execute_proposal",
    "parse_manifest",
    "rollback_receipt",
]
