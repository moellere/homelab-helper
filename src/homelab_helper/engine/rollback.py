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

from homelab_helper.adapters.proxmox import ProxmoxAPIError

if TYPE_CHECKING:
    from homelab_helper.adapters.proxmox import ProxmoxAdapter
    from homelab_helper.engine.executor import ActionManifest

PRIOR_POWER_STATE = "prior-power-state"
SNAPSHOT = "snapshot"

_RESTORABLE_STATUSES = {"running", "stopped"}
_POWER_ACTION_KINDS = {"start", "stop", "shutdown", "restart"}
_SNAPSHOT_PREFIX = "helper"


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
    """Everything :func:`restore` needs, and everything the receipt records."""

    strategy: str
    verified: bool
    evidence: str
    state: dict[str, Any]
    captured_at: str
    node: str
    vmid: int
    vm_kind: str
    capture_error: str | None = None

    def as_receipt_state(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "strategy": self.strategy,
            "verified": self.verified,
            "evidence": self.evidence,
            "captured_at": self.captured_at,
            "node": self.node,
            "vmid": self.vmid,
            "vm_kind": self.vm_kind,
            **self.state,
        }
        if self.capture_error:
            payload["capture_error"] = self.capture_error
        return payload

    @classmethod
    def from_receipt_state(cls, state: dict[str, Any]) -> RollbackPlan:
        """Rebuild a plan from a stored receipt so a later session can restore."""
        known = {
            "strategy",
            "verified",
            "evidence",
            "captured_at",
            "node",
            "vmid",
            "vm_kind",
            "capture_error",
        }
        missing = [k for k in ("node", "vmid", "vm_kind") if state.get(k) is None]
        if missing:
            raise RollbackError(
                f"receipt's rollback state is missing {', '.join(missing)} — "
                "it predates the orchestrator and cannot be restored automatically"
            )
        return cls(
            strategy=str(state.get("strategy") or PRIOR_POWER_STATE),
            verified=bool(state.get("verified")),
            evidence=str(state.get("evidence") or ""),
            state={k: v for k, v in state.items() if k not in known},
            captured_at=str(state.get("captured_at") or ""),
            node=str(state["node"]),
            vmid=int(state["vmid"]),
            vm_kind=str(state["vm_kind"]),
            capture_error=state.get("capture_error"),
        )


def select_strategy(manifest: ActionManifest) -> str:
    """The manifest may request a strategy; otherwise the action kind decides."""
    requested = (manifest.rollback_strategy or "").strip().lower()
    if requested in {PRIOR_POWER_STATE, SNAPSHOT}:
        return requested
    if requested:
        return requested  # unknown: verification will refuse it by name
    return PRIOR_POWER_STATE if manifest.action_kind in _POWER_ACTION_KINDS else SNAPSHOT


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
        current = await adapter.vm_current_status(manifest.node, manifest.vmid, manifest.vm_kind)
    except (ProxmoxAPIError, OSError) as exc:
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
        existing = await adapter.list_snapshots(manifest.node, manifest.vmid, manifest.vm_kind)
    except (ProxmoxAPIError, OSError) as exc:
        return False, f"guest does not support snapshots here: {exc}", {}
    return (
        True,
        "guest storage supports snapshots; one is taken before dispatch",
        {"existing_snapshots": len(existing)},
    )


async def verify_rollback(
    adapter: ProxmoxAdapter, manifest: ActionManifest
) -> RollbackVerification:
    """Read-only: can this action be undone? Runs *before* ``decide()``."""
    strategy = select_strategy(manifest)
    claimed = manifest.rollback_verified

    if strategy == PRIOR_POWER_STATE:
        verified, evidence, probe = await _verify_prior_power_state(adapter, manifest)
    elif strategy == SNAPSHOT:
        verified, evidence, probe = await _verify_snapshot(adapter, manifest)
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
) -> RollbackPlan:
    """Record (and for snapshots, create) what restore will need. May write.

    Only ever called after the action is authorized — taking a snapshot is
    itself a change to the target, so it must not happen while the gate is
    still deciding.
    """
    now = datetime.now(UTC)
    state: dict[str, Any] = {"prior": dict(verification.probe)} if verification.probe else {}
    capture_error: str | None = None

    if verification.strategy == SNAPSHOT and verification.verified:
        name = _snapshot_name(now)
        try:
            await adapter.create_snapshot(
                manifest.node,
                manifest.vmid,
                manifest.vm_kind,
                name,
                description=f"homelab-helper pre-{manifest.action_kind}",
            )
            state["snapshot"] = name
        except (ProxmoxAPIError, OSError) as exc:
            capture_error = f"snapshot creation failed: {exc}"

    return RollbackPlan(
        strategy=verification.strategy,
        verified=verification.verified,
        evidence=verification.evidence,
        state=state,
        captured_at=now.isoformat(),
        node=manifest.node,
        vmid=manifest.vmid,
        vm_kind=manifest.vm_kind,
        capture_error=capture_error,
    )


async def _restore_power_state(adapter: ProxmoxAdapter, plan: RollbackPlan) -> str:
    target = (plan.state.get("prior") or {}).get("status")
    if target not in _RESTORABLE_STATUSES:
        raise RollbackError(f"captured state names no restorable status (got {target!r})")

    current = await adapter.vm_current_status(plan.node, plan.vmid, plan.vm_kind)
    if current.get("status") == target:
        return f"guest is already {target}; nothing to undo"

    action = "start" if target == "running" else "stop"
    await adapter.vm_power(plan.node, plan.vmid, plan.vm_kind, action)
    return f"issued {action} to restore the guest to {target}"


async def _restore_snapshot(adapter: ProxmoxAdapter, plan: RollbackPlan) -> str:
    name = plan.state.get("snapshot")
    if not name:
        raise RollbackError("no snapshot was captured for this action")
    await adapter.rollback_snapshot(plan.node, plan.vmid, plan.vm_kind, str(name))
    return f"rolled the guest back to snapshot {name}"


async def restore(adapter: ProxmoxAdapter, plan: RollbackPlan) -> str:
    """Drive the target back to the captured state; returns what was done."""
    if plan.strategy == PRIOR_POWER_STATE:
        return await _restore_power_state(adapter, plan)
    if plan.strategy == SNAPSHOT:
        return await _restore_snapshot(adapter, plan)
    raise RollbackError(f"no restore path for strategy {plan.strategy!r}")


__all__ = [
    "PRIOR_POWER_STATE",
    "SNAPSHOT",
    "RollbackError",
    "RollbackPlan",
    "RollbackVerification",
    "capture_rollback",
    "restore",
    "select_strategy",
    "verify_rollback",
]
