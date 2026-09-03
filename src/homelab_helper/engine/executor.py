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
- Rollback state (the guest's prior power state) is captured **before**
  dispatch and lands in the receipt either way.
- Every dispatch — success or failure — writes exactly one
  ``ExecutionReceipt``. Success also closes the proposal (USER_ACCEPTED);
  failure leaves it PENDING so it can be retried.
- Every dispatch feeds the cell's trust record (``engine/escalation.py``):
  success extends the clean streak, failure demotes the cell to PROPOSE and
  flags probation. The feedback is one-way — escalation writes levels that a
  *later* ``decide()`` reads; it never influences the decision in flight.

The only write surface today is Proxmox guest power
(start | stop | shutdown | restart), AC2's ``containers/restart/single-host``
cell being the canonical first cell.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homelab_helper.adapters.proxmox import ProxmoxAPIError
from homelab_helper.db.enums import AutonomyLevel, ProposalOutcome, TrustDomain
from homelab_helper.db.models import ExecutionReceipt, ProposalLog
from homelab_helper.engine.escalation import (
    EscalationResult,
    record_bad_outcome,
    record_clean_outcome,
)
from homelab_helper.engine.trust import ActionRequest, Decision, decide, load_trust_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.adapters.proxmox import ProxmoxAdapter

    ConfirmCallback = Callable[["ActionManifest", Decision], Awaitable[bool]]

# Trust-vocabulary verbs → Proxmox API verbs.
_POWER_DISPATCH = {"start": "start", "stop": "stop", "shutdown": "shutdown", "restart": "reboot"}

# The guest kind fixes the trust domain; a manifest may not claim otherwise.
_VM_KIND_DOMAIN = {"lxc": TrustDomain.CONTAINERS, "qemu": TrustDomain.HYPERVISOR}


class ManifestError(ValueError):
    """The proposal's artifact is not a valid executable action manifest."""


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
    node: str
    vmid: int
    vm_kind: str
    rollback_verified: bool
    rollback_strategy: str | None
    action_raw: dict[str, Any]

    @property
    def cell_key(self) -> str:
        return f"{self.domain.value}/{self.action_kind}/{self.blast_radius}"

    @property
    def target_label(self) -> str:
        return f"{self.vm_kind}/{self.vmid} on {self.node}"


@dataclass(frozen=True)
class ExecutionResult:
    receipt_id: uuid.UUID
    decision: Decision
    outcome: str
    error: str | None
    duration_ms: int
    escalation: EscalationResult | None = None
    """What this outcome did to the cell's floor — see engine/escalation.py."""


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
    if action_kind not in _POWER_DISPATCH:
        allowed = ", ".join(sorted(_POWER_DISPATCH))
        raise ManifestError(f"action_kind {action_kind!r} is not supported (allowed: {allowed})")

    target = action.get("target")
    if not isinstance(target, dict):
        raise ManifestError('manifest action has no "target" object')
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
    declared = str(action.get("domain"))
    try:
        domain = TrustDomain(declared)
    except ValueError:
        raise ManifestError(f"unknown trust domain {declared!r}") from None
    if domain is not expected_domain:
        raise ManifestError(
            f"declared domain {domain.value!r} does not match guest kind {vm_kind!r} "
            f"(which is {expected_domain.value!r}) — refusing"
        )

    hostnames = tuple(action.get("hostnames") or (node,))
    rollback = artifact.get("rollback") or {}
    if not isinstance(rollback, dict):
        raise ManifestError('"rollback" must be an object when present')

    return ActionManifest(
        domain=domain,
        action_kind=action_kind,
        blast_radius=proposal.blast_radius,
        hostnames=hostnames,
        node=node,
        vmid=vmid,
        vm_kind=vm_kind,
        rollback_verified=bool(rollback.get("verified")),
        rollback_strategy=rollback.get("strategy"),
        action_raw=action,
    )


async def _capture_rollback_state(
    adapter: ProxmoxAdapter, manifest: ActionManifest
) -> dict[str, Any]:
    """Prior power state, captured before dispatch — best-effort but recorded."""
    state: dict[str, Any] = {
        "strategy": manifest.rollback_strategy or "prior-power-state",
        "verified": manifest.rollback_verified,
        "captured_at": datetime.now(UTC).isoformat(),
    }
    try:
        prior = await adapter.vm_current_status(manifest.node, manifest.vmid, manifest.vm_kind)
        state["prior"] = {
            "status": prior.get("status"),
            "name": prior.get("name"),
            "uptime_s": prior.get("uptime"),
        }
    except (ProxmoxAPIError, OSError) as exc:
        state["capture_error"] = str(exc)
    return state


async def execute_proposal(
    session: AsyncSession,
    proposal: ProposalLog,
    adapter: ProxmoxAdapter,
    *,
    actor: str,
    confirm_cb: ConfirmCallback | None = None,
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
    action = ActionRequest(
        domain=manifest.domain,
        action_kind=manifest.action_kind,
        blast_radius=manifest.blast_radius,
        hostnames=manifest.hostnames,
        rollback_verified=manifest.rollback_verified,
        provenance=proposal.proposed_by,
    )
    context = await load_trust_context(session, action)
    decision = decide(action, context)

    if decision.level in (AutonomyLevel.BLOCK, AutonomyLevel.PROPOSE):
        raise ExecutionRefused(
            f"decision is {decision.level.value} for cell {manifest.cell_key} — not dispatching",
            decision,
        )
    if decision.level is AutonomyLevel.CONFIRM:
        if confirm_cb is None:
            raise ExecutionRefused(
                "decision requires operator confirmation and no confirmer is available",
                decision,
            )
        if not await confirm_cb(manifest, decision):
            raise ExecutionRefused("operator declined — proposal left pending", decision)

    rollback_state = await _capture_rollback_state(adapter, manifest)

    started = time.monotonic()
    outcome, error, upid = "succeeded", None, None
    try:
        upid = await adapter.vm_power(
            manifest.node, manifest.vmid, manifest.vm_kind, _POWER_DISPATCH[manifest.action_kind]
        )
    except (ProxmoxAPIError, OSError) as exc:
        outcome, error = "failed", str(exc)
    duration_ms = int((time.monotonic() - started) * 1000)

    receipt = ExecutionReceipt(
        proposal_id=proposal.id,
        actor=actor,
        decision_level=decision.level,
        decision_reasons=list(decision.reasons),
        window_id=uuid.UUID(decision.window_id) if decision.window_id else None,
        action={**manifest.action_raw, "dispatched": _POWER_DISPATCH[manifest.action_kind]},
        rollback_state=rollback_state,
        outcome=outcome,
        error=error,
        duration_ms=duration_ms,
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

    return ExecutionResult(
        receipt_id=receipt.id,
        decision=decision,
        outcome=outcome,
        error=error,
        duration_ms=duration_ms,
        escalation=escalation,
    )


__all__ = [
    "ActionManifest",
    "ExecutionRefused",
    "ExecutionResult",
    "ManifestError",
    "execute_proposal",
    "parse_manifest",
]
