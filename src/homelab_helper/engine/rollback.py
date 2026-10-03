"""Snapshot/rollback orchestration — the verified-rollback floor made real.

Phase 6 PR D (P6-AC4). Until now ``rollback_verified`` came off the manifest:
the proposal *claimed* it was reversible and the gate believed it. That is the
one input to ``decide()`` an untrusted (possibly LLM-drafted) artifact could
set in its own favour, turning the AUTONOMOUS-degrades-to-CONFIRM floor into
an honour system. This module replaces the claim with a finding.

Three phases, deliberately ordered around the authorization gate:

1. :func:`verify_rollback` — **read-only, before ``decide()``.** Can this
   action actually be undone? Answers by probing the target (is the prior
   power state readable? does this guest's storage support snapshots?), never
   by reading the manifest's own say-so. The result feeds
   ``ActionRequest.rollback_verified``.
2. :func:`capture_rollback` — **after authorization, before dispatch.** Now
   that the action is allowed to run, record what restore needs, and take the
   snapshot if that is the strategy. Capture may write; verification may not.
3. :func:`restore` — drive the target back to the captured state.

The manifest still *chooses* a strategy; it just cannot certify one. A
manifest asking for a strategy that does not apply, or one whose probe fails,
comes back unverified with the reason attached — which degrades AUTONOMOUS to
CONFIRM rather than failing the action outright. Its claim is recorded next to
the finding, so a manifest that asserted a reversibility it did not have is
visible in the receipt afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homelab_helper.adapters.argocd import ArgoCDAPIError
from homelab_helper.adapters.kubernetes import KubeError
from homelab_helper.adapters.proxmox import ProxmoxAPIError
from homelab_helper.adapters.unifi import UniFiAPIError

if TYPE_CHECKING:
    from homelab_helper.adapters.argocd import ArgoCDAdapter
    from homelab_helper.adapters.kubernetes import K8sAdapter
    from homelab_helper.adapters.proxmox import ProxmoxAdapter
    from homelab_helper.adapters.unifi import UniFiAdapter
    from homelab_helper.engine.executor import ActionManifest

PRIOR_POWER_STATE = "prior-power-state"
SNAPSHOT = "snapshot"
PRIOR_NODE = "prior-node"
PRIOR_REPLICAS = "prior-replicas"
ROLLOUT_UNDO = "rollout-undo"
PRIOR_CONFIG = "prior-config"
ARGOCD_HISTORY = "argocd-history"
PRIOR_DNS_RECORD = "prior-dns-record"
STRATEGIES: frozenset[str] = frozenset(
    {
        PRIOR_POWER_STATE,
        SNAPSHOT,
        PRIOR_NODE,
        PRIOR_REPLICAS,
        ROLLOUT_UNDO,
        PRIOR_CONFIG,
        ARGOCD_HISTORY,
        PRIOR_DNS_RECORD,
    }
)

_RESTORABLE_STATUSES = {"running", "stopped"}
_POWER_ACTION_KINDS = {"start", "stop", "shutdown", "restart"}
_DEFAULT_STRATEGY = {
    "migrate": PRIOR_NODE,
    "cpu-type": PRIOR_CONFIG,
    "argocd-sync": ARGOCD_HISTORY,
    "dns-record": PRIOR_DNS_RECORD,
    "workload-scale": PRIOR_REPLICAS,
    "workload-restart": ROLLOUT_UNDO,
}
_SNAPSHOT_PREFIX = "helper"
_TARGET_KEYS = (
    "node",
    "vmid",
    "vm_kind",
    "namespace",
    "workload_kind",
    "workload_name",
    "application",
    "dns_hostname",
    "record_type",
    "controller",
)


class RollbackError(RuntimeError):
    """A restore that could not be carried out."""


@dataclass(frozen=True)
class RollbackVerification:
    """The read-only finding that gates autonomy."""

    verified: bool
    strategy: str
    evidence: str
    claimed: bool = False
    """What the manifest asserted — kept only so a false claim stays visible."""
    probe: dict[str, Any] = field(default_factory=dict)

    @property
    def claim_was_false(self) -> bool:
        return self.claimed and not self.verified


@dataclass(frozen=True)
class RollbackPlan:
    """Everything :func:`restore` needs, and everything the receipt records.

    A guest plan carries ``node``/``vmid``/``vm_kind``; a workload plan carries
    ``namespace``/``workload_kind``/``workload_name``. Receipts store whichever
    set applies at the top level, so pre-Phase-7 receipts still rebuild.
    """

    strategy: str
    verified: bool
    evidence: str
    state: dict[str, Any]
    captured_at: str
    node: str | None = None
    vmid: int | None = None
    vm_kind: str | None = None
    namespace: str | None = None
    workload_kind: str | None = None
    workload_name: str | None = None
    application: str | None = None
    dns_hostname: str | None = None
    record_type: str | None = None
    controller: str | None = None
    capture_error: str | None = None

    @property
    def is_workload(self) -> bool:
        return self.workload_name is not None

    def as_receipt_state(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "strategy": self.strategy,
            "verified": self.verified,
            "evidence": self.evidence,
            "captured_at": self.captured_at,
            **self.state,
        }
        for key in _TARGET_KEYS:
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.capture_error:
            payload["capture_error"] = self.capture_error
        return payload

    @classmethod
    def from_receipt_state(cls, state: dict[str, Any]) -> RollbackPlan:
        """Rebuild a plan from a stored receipt so a later session can restore."""
        known = {"strategy", "verified", "evidence", "captured_at", "capture_error", *_TARGET_KEYS}
        strategy = str(state.get("strategy") or PRIOR_POWER_STATE)
        if strategy in (PRIOR_REPLICAS, ROLLOUT_UNDO):
            required: tuple[str, ...] = ("namespace", "workload_kind", "workload_name")
        elif strategy == ARGOCD_HISTORY:
            required = ("application",)
        elif strategy == PRIOR_DNS_RECORD:
            required = ("dns_hostname", "record_type")
        else:
            required = ("node", "vmid", "vm_kind")
        missing = [k for k in required if state.get(k) is None]
        if missing:
            raise RollbackError(
                f"receipt's rollback state is missing {', '.join(missing)} — "
                "it predates the orchestrator and cannot be restored automatically"
            )
        return cls(
            strategy=strategy,
            verified=bool(state.get("verified")),
            evidence=str(state.get("evidence") or ""),
            state={k: v for k, v in state.items() if k not in known},
            captured_at=str(state.get("captured_at") or ""),
            node=state.get("node"),
            vmid=int(state["vmid"]) if state.get("vmid") is not None else None,
            vm_kind=state.get("vm_kind"),
            namespace=state.get("namespace"),
            workload_kind=state.get("workload_kind"),
            workload_name=state.get("workload_name"),
            application=state.get("application"),
            dns_hostname=state.get("dns_hostname"),
            record_type=state.get("record_type"),
            controller=state.get("controller"),
            capture_error=state.get("capture_error"),
        )


def select_strategy(manifest: ActionManifest) -> str:
    """The manifest may request a strategy; otherwise the action kind decides."""
    requested = (manifest.rollback_strategy or "").strip().lower()
    if requested in STRATEGIES:
        return requested
    if requested:
        return requested  # unknown: verification will refuse it by name
    if manifest.action_kind in _POWER_ACTION_KINDS:
        return PRIOR_POWER_STATE
    return _DEFAULT_STRATEGY.get(manifest.action_kind, SNAPSHOT)


def _guest(manifest: ActionManifest) -> tuple[str, int, str]:
    if manifest.node is None or manifest.vmid is None or manifest.vm_kind is None:
        raise RollbackError("this strategy applies to a Proxmox guest, and the manifest names none")
    return manifest.node, manifest.vmid, manifest.vm_kind


def _workload(manifest: ActionManifest) -> tuple[str, str, str]:
    if (
        manifest.namespace is None
        or manifest.workload_kind is None
        or manifest.workload_name is None
    ):
        raise RollbackError(
            "this strategy applies to a Kubernetes workload, and the manifest names none"
        )
    return manifest.namespace, manifest.workload_kind, manifest.workload_name


async def _verify_prior_power_state(
    adapter: ProxmoxAdapter, manifest: ActionManifest
) -> tuple[bool, str, dict[str, Any]]:
    if manifest.action_kind not in _POWER_ACTION_KINDS:
        return (
            False,
            f"prior-power-state cannot undo a {manifest.action_kind} action",
            {},
        )
    try:
        current = await adapter.vm_current_status(*_guest(manifest))
    except (ProxmoxAPIError, OSError, RollbackError) as exc:
        return False, f"could not read the guest's current state: {exc}", {}

    status = current.get("status")
    if status not in _RESTORABLE_STATUSES:
        return (
            False,
            f"guest reports status {status!r}, which names no state to restore",
            {"status": status},
        )
    return (
        True,
        f"guest is {status}; the inverse power action restores it",
        {"status": status, "name": current.get("name"), "uptime_s": current.get("uptime")},
    )


async def _verify_snapshot(
    adapter: ProxmoxAdapter, manifest: ActionManifest
) -> tuple[bool, str, dict[str, Any]]:
    try:
        existing = await adapter.list_snapshots(*_guest(manifest))
    except (ProxmoxAPIError, OSError, RollbackError) as exc:
        return False, f"guest does not support snapshots here: {exc}", {}
    return (
        True,
        "guest storage supports snapshots; one is taken before dispatch",
        {"existing_snapshots": len(existing)},
    )


async def _verify_prior_node(
    adapter: ProxmoxAdapter, manifest: ActionManifest
) -> tuple[bool, str, dict[str, Any]]:
    if manifest.action_kind != "migrate" or not manifest.target_node:
        return False, "prior-node only undoes a migrate action", {}
    try:
        node, vmid, vm_kind = _guest(manifest)
        status = await adapter.cluster_status()
        current = await adapter.vm_current_status(node, vmid, vm_kind)
    except (ProxmoxAPIError, OSError, RollbackError) as exc:
        return False, f"could not read cluster or guest state: {exc}", {}
    online = {n.get("name") for n in status.get("nodes") or [] if n.get("online")}
    if node not in online or manifest.target_node not in online:
        return (
            False,
            f"migrating back needs both {node} and {manifest.target_node} online",
            {"online_nodes": sorted(n for n in online if n)},
        )
    return (
        True,
        f"both nodes are online; the guest migrates back to {node} to undo",
        {
            "prior_node": node,
            "target_node": manifest.target_node,
            "online": bool(manifest.online if manifest.online is not None else True),
            "status": current.get("status"),
        },
    )


async def _verify_prior_config(
    adapter: ProxmoxAdapter, manifest: ActionManifest
) -> tuple[bool, str, dict[str, Any]]:
    if manifest.action_kind != "cpu-type":
        return False, "prior-config only undoes a cpu-type action", {}
    try:
        config = await adapter.vm_config(*_guest(manifest))
    except (ProxmoxAPIError, OSError, RollbackError) as exc:
        return False, f"could not read the guest's configuration: {exc}", {}
    current = config.get("cpu")
    return (
        True,
        f"guest cpu type is {current!r}; setting it back restores the config",
        {"cpu": current},
    )


async def _verify_prior_replicas(
    k8s: K8sAdapter | None, manifest: ActionManifest
) -> tuple[bool, str, dict[str, Any]]:
    if k8s is None:
        return False, "no Kubernetes adapter is configured", {}
    if manifest.action_kind != "workload-scale":
        return False, "prior-replicas only undoes a workload-scale action", {}
    try:
        current = await k8s.get_workload(*_workload(manifest))
    except (KubeError, OSError, ValueError, RollbackError) as exc:
        return False, f"could not read the workload: {exc}", {}
    if not isinstance(current.get("replicas"), int):
        return False, "workload reports no replica count to restore", current
    return True, f"workload has {current['replicas']} replicas; scaling back restores it", current


async def _verify_rollout_undo(
    k8s: K8sAdapter | None, manifest: ActionManifest
) -> tuple[bool, str, dict[str, Any]]:
    if k8s is None:
        return False, "no Kubernetes adapter is configured", {}
    if manifest.action_kind != "workload-restart":
        return False, "rollout-undo only undoes a workload-restart action", {}
    try:
        revisions = await k8s.rollout_history(*_workload(manifest))
    except (KubeError, OSError, ValueError, RollbackError) as exc:
        return False, f"could not read the rollout history: {exc}", {}
    if not revisions:
        return False, "workload has no rollout revisions to return to", {}
    return (
        True,
        f"rollout history has {len(revisions)} revision(s); undo returns to the current one",
        {"revision": revisions[-1]},
    )


async def _verify_argocd_history(
    argocd: ArgoCDAdapter | None, manifest: ActionManifest
) -> tuple[bool, str, dict[str, Any]]:
    if argocd is None:
        return False, "no Argo CD adapter is configured", {}
    if manifest.action_kind != "argocd-sync" or not manifest.application:
        return False, "argocd-history only undoes an argocd-sync action", {}
    try:
        app = await argocd.get_application(manifest.application)
    except (ArgoCDAPIError, OSError) as exc:
        return False, f"could not read the application: {exc}", {}
    history = app.get("history") or []
    if app.get("auto_sync"):
        return (
            False,
            "application has automated sync, and Argo CD refuses rollbacks while it is on; "
            "undoing a sync there is a git revert",
            {"auto_sync": True, "history_id": history[-1]["id"] if history else None},
        )
    if not history:
        return False, "application has no sync history to roll back to", {}
    last = history[-1]
    return (
        True,
        f"application is at history id {last['id']} ({str(last.get('revision'))[:12]}); "
        "a rollback returns to it",
        {
            "history_id": last["id"],
            "revision": last.get("revision"),
            "sync_status": app.get("sync_status"),
        },
    )


async def _verify_prior_dns_record(
    unifi: UniFiAdapter | None, manifest: ActionManifest
) -> tuple[bool, str, dict[str, Any]]:
    if unifi is None:
        return False, "no UniFi adapter is configured", {}
    if manifest.action_kind != "dns-record" or not manifest.dns_hostname:
        return False, "prior-dns-record only undoes a dns-record action", {}
    try:
        existing = await unifi.find_dns_record(manifest.dns_hostname, manifest.record_type or "A")
    except (UniFiAPIError, OSError) as exc:
        return False, f"could not read the controller's static DNS: {exc}", {}
    if existing is None:
        return (
            True,
            "no record exists yet; undo deletes the one this action creates",
            {"existing": None},
        )
    return (
        True,
        f"record exists ({existing.get('value')}); undo puts that value back",
        {"existing": existing},
    )


async def verify_rollback(
    adapter: ProxmoxAdapter,
    manifest: ActionManifest,
    *,
    k8s: K8sAdapter | None = None,
    argocd: ArgoCDAdapter | None = None,
    unifi: UniFiAdapter | None = None,
) -> RollbackVerification:
    """Read-only: can this action be undone? Runs *before* ``decide()``."""
    strategy = select_strategy(manifest)
    claimed = manifest.rollback_verified

    if strategy == PRIOR_POWER_STATE:
        verified, evidence, probe = await _verify_prior_power_state(adapter, manifest)
    elif strategy == SNAPSHOT:
        verified, evidence, probe = await _verify_snapshot(adapter, manifest)
    elif strategy == PRIOR_NODE:
        verified, evidence, probe = await _verify_prior_node(adapter, manifest)
    elif strategy == PRIOR_CONFIG:
        verified, evidence, probe = await _verify_prior_config(adapter, manifest)
    elif strategy == PRIOR_REPLICAS:
        verified, evidence, probe = await _verify_prior_replicas(k8s, manifest)
    elif strategy == ROLLOUT_UNDO:
        verified, evidence, probe = await _verify_rollout_undo(k8s, manifest)
    elif strategy == ARGOCD_HISTORY:
        verified, evidence, probe = await _verify_argocd_history(argocd, manifest)
    elif strategy == PRIOR_DNS_RECORD:
        verified, evidence, probe = await _verify_prior_dns_record(unifi, manifest)
    else:
        verified, evidence, probe = (
            False,
            f"unknown rollback strategy {strategy!r}",
            {},
        )

    if claimed and not verified:
        evidence = f"manifest claimed a verified rollback, but {evidence}"
    return RollbackVerification(
        verified=verified,
        strategy=strategy,
        evidence=evidence,
        claimed=claimed,
        probe=probe,
    )


def _snapshot_name(now: datetime) -> str:
    return f"{_SNAPSHOT_PREFIX}-{now.strftime('%Y%m%d-%H%M%S')}"


async def capture_rollback(
    adapter: ProxmoxAdapter,
    manifest: ActionManifest,
    verification: RollbackVerification,
    *,
    best_effort: bool = False,
) -> RollbackPlan:
    """Record (and for snapshots, create) what restore will need. May write.

    Only ever called after the action is authorized — taking a snapshot is
    itself a change to the target, so it must not happen while the gate is
    still deciding.

    ``best_effort`` is the elevation-window case: the floor was lifted without
    a verified rollback, so the architecture asks for a snapshot anyway. It is
    attempted even when verification failed, and a failure to take it is
    recorded rather than raised — the action was already authorized, and a
    missing snapshot must not turn into an unlogged half-execution.
    """
    now = datetime.now(UTC)
    state: dict[str, Any] = {"prior": dict(verification.probe)} if verification.probe else {}
    capture_error: str | None = None

    snapshot_wanted = (verification.strategy == SNAPSHOT and verification.verified) or (
        best_effort and not verification.verified
    )
    if snapshot_wanted and manifest.node is not None:
        name = _snapshot_name(now)
        try:
            await adapter.create_snapshot(
                *_guest(manifest),
                name,
                description=f"homelab-helper pre-{manifest.action_kind}",
            )
            state["snapshot"] = name
            if best_effort and not verification.verified:
                state["best_effort_snapshot"] = True
        except (ProxmoxAPIError, OSError, RollbackError) as exc:
            capture_error = f"snapshot creation failed: {exc}"
    elif snapshot_wanted:
        capture_error = "best-effort snapshot requested, but the target is not a Proxmox guest"

    return RollbackPlan(
        strategy=verification.strategy,
        verified=verification.verified,
        evidence=verification.evidence,
        state=state,
        captured_at=now.isoformat(),
        node=manifest.node,
        vmid=manifest.vmid,
        vm_kind=manifest.vm_kind,
        namespace=manifest.namespace,
        workload_kind=manifest.workload_kind,
        workload_name=manifest.workload_name,
        application=manifest.application,
        dns_hostname=manifest.dns_hostname,
        record_type=manifest.record_type,
        controller=manifest.controller,
        capture_error=capture_error,
    )


def _plan_guest(plan: RollbackPlan) -> tuple[str, int, str]:
    if plan.node is None or plan.vmid is None or plan.vm_kind is None:
        raise RollbackError("captured state names no Proxmox guest")
    return plan.node, plan.vmid, plan.vm_kind


def _plan_workload(plan: RollbackPlan) -> tuple[str, str, str]:
    if plan.namespace is None or plan.workload_kind is None or plan.workload_name is None:
        raise RollbackError("captured state names no Kubernetes workload")
    return plan.namespace, plan.workload_kind, plan.workload_name


async def _restore_power_state(adapter: ProxmoxAdapter, plan: RollbackPlan) -> str:
    target = (plan.state.get("prior") or {}).get("status")
    if target not in _RESTORABLE_STATUSES:
        raise RollbackError(f"captured state names no restorable status (got {target!r})")

    node, vmid, vm_kind = _plan_guest(plan)
    current = await adapter.vm_current_status(node, vmid, vm_kind)
    if current.get("status") == target:
        return f"guest is already {target}; nothing to undo"

    action = "start" if target == "running" else "stop"
    await adapter.vm_power(node, vmid, vm_kind, action)
    return f"issued {action} to restore the guest to {target}"


async def _restore_snapshot(adapter: ProxmoxAdapter, plan: RollbackPlan) -> str:
    name = plan.state.get("snapshot")
    if not name:
        raise RollbackError("no snapshot was captured for this action")
    await adapter.rollback_snapshot(*_plan_guest(plan), str(name))
    return f"rolled the guest back to snapshot {name}"


async def _restore_prior_node(adapter: ProxmoxAdapter, plan: RollbackPlan) -> str:
    prior = (plan.state.get("prior") or {}).get("prior_node")
    target = (plan.state.get("prior") or {}).get("target_node")
    online = bool((plan.state.get("prior") or {}).get("online", True))
    if not prior or not target:
        raise RollbackError("captured state names no source node to migrate back to")
    _, vmid, vm_kind = _plan_guest(plan)
    await adapter.migrate_guest(str(target), vmid, vm_kind, str(prior), online=online)
    return f"issued migrate from {target} back to {prior}"


async def _restore_prior_config(adapter: ProxmoxAdapter, plan: RollbackPlan) -> str:
    prior = plan.state.get("prior") or {}
    if "cpu" not in prior:
        raise RollbackError("captured state names no prior cpu type")
    node, vmid, vm_kind = _plan_guest(plan)
    cpu = prior["cpu"]
    if cpu is None:
        await adapter.set_vm_config(node, vmid, vm_kind, delete="cpu")
        return "removed the cpu type so the guest returns to the Proxmox default"
    await adapter.set_vm_config(node, vmid, vm_kind, cpu=str(cpu))
    return f"set cpu={cpu} back (applies at next stop/start)"


async def _restore_prior_replicas(k8s: K8sAdapter | None, plan: RollbackPlan) -> str:
    if k8s is None:
        raise RollbackError("no Kubernetes adapter is configured")
    replicas = (plan.state.get("prior") or {}).get("replicas")
    if not isinstance(replicas, int):
        raise RollbackError("captured state names no prior replica count")
    await k8s.scale_workload(*_plan_workload(plan), replicas)
    return f"scaled the workload back to {replicas} replicas"


async def _restore_rollout_undo(k8s: K8sAdapter | None, plan: RollbackPlan) -> str:
    if k8s is None:
        raise RollbackError("no Kubernetes adapter is configured")
    revision = (plan.state.get("prior") or {}).get("revision")
    await k8s.rollout_undo(
        *_plan_workload(plan), to_revision=revision if isinstance(revision, int) else None
    )
    return "rolled the workload back to its pre-restart revision"


async def _restore_argocd_history(argocd: ArgoCDAdapter | None, plan: RollbackPlan) -> str:
    if argocd is None:
        raise RollbackError("no Argo CD adapter is configured")
    history_id = (plan.state.get("prior") or {}).get("history_id")
    if not isinstance(history_id, int) or plan.application is None:
        raise RollbackError("captured state names no sync-history entry")
    await argocd.rollback_application(plan.application, history_id)
    return f"rolled the application back to history id {history_id}"


async def _restore_prior_dns_record(unifi: UniFiAdapter | None, plan: RollbackPlan) -> str:
    if unifi is None:
        raise RollbackError("no UniFi adapter is configured")
    if plan.dns_hostname is None:
        raise RollbackError("captured state names no DNS record")
    rtype = plan.record_type or "A"
    prior = (plan.state.get("prior") or {}).get("existing")
    current = await unifi.find_dns_record(plan.dns_hostname, rtype)
    if prior is None:
        if current and current.get("id"):
            await unifi.delete_dns_record(str(current["id"]))
            return f"deleted the {rtype} record for {plan.dns_hostname} that the action created"
        return f"no {rtype} record for {plan.dns_hostname} exists; nothing to undo"
    if not current or not current.get("id"):
        restored = await unifi.create_dns_record(
            plan.dns_hostname,
            str(prior.get("value")),
            record_type=rtype,
            ttl=int(prior.get("ttl") or 0),
        )
        return f"re-created the prior {rtype} record ({restored.get('value')})"
    await unifi.update_dns_record(
        str(current["id"]),
        plan.dns_hostname,
        str(prior.get("value")),
        record_type=rtype,
        ttl=int(prior.get("ttl") or 0),
        enabled=bool(prior.get("enabled", True)),
    )
    return f"restored the prior {rtype} value {prior.get('value')} for {plan.dns_hostname}"


async def restore(
    adapter: ProxmoxAdapter,
    plan: RollbackPlan,
    *,
    k8s: K8sAdapter | None = None,
    argocd: ArgoCDAdapter | None = None,
    unifi: UniFiAdapter | None = None,
) -> str:
    """Drive the target back to the captured state; returns what was done."""
    guest = {
        PRIOR_POWER_STATE: _restore_power_state,
        SNAPSHOT: _restore_snapshot,
        PRIOR_NODE: _restore_prior_node,
        PRIOR_CONFIG: _restore_prior_config,
    }
    if plan.strategy in guest:
        return await guest[plan.strategy](adapter, plan)
    if plan.strategy == PRIOR_REPLICAS:
        return await _restore_prior_replicas(k8s, plan)
    if plan.strategy == ROLLOUT_UNDO:
        return await _restore_rollout_undo(k8s, plan)
    if plan.strategy == ARGOCD_HISTORY:
        return await _restore_argocd_history(argocd, plan)
    if plan.strategy == PRIOR_DNS_RECORD:
        return await _restore_prior_dns_record(unifi, plan)
    raise RollbackError(f"no restore path for strategy {plan.strategy!r}")


__all__ = [
    "ARGOCD_HISTORY",
    "PRIOR_CONFIG",
    "PRIOR_DNS_RECORD",
    "PRIOR_NODE",
    "PRIOR_POWER_STATE",
    "PRIOR_REPLICAS",
    "ROLLOUT_UNDO",
    "SNAPSHOT",
    "STRATEGIES",
    "RollbackError",
    "RollbackPlan",
    "RollbackVerification",
    "capture_rollback",
    "restore",
    "select_strategy",
    "verify_rollback",
]
