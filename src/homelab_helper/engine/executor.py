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

The only write surface today is Proxmox guest power
(start | stop | shutdown | restart), AC2's ``containers/restart/single-host``
cell being the canonical first cell.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homelab_helper.adapters.proxmox import ProxmoxAPIError
from homelab_helper.db.enums import AutonomyLevel, ProposalOutcome, TrustDomain
from homelab_helper.db.models import ExecutionReceipt, ProposalLog, TrustHistory
from homelab_helper.engine.escalation import (
    EscalationResult,
    record_bad_outcome,
    record_clean_outcome,
)
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


async def execute_proposal(
    session: AsyncSession,
    proposal: ProposalLog,
    adapter: ProxmoxAdapter,
    *,
    actor: str,
    confirm_cb: ConfirmCallback | None = None,
    override: OverrideGrant | None = None,
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
    verification = await verify_rollback(adapter, manifest)
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
    if decision.level is AutonomyLevel.CONFIRM:
        if confirm_cb is None:
            raise ExecutionRefused(
                "decision requires operator confirmation and no confirmer is available",
                decision,
            )
        if not await confirm_cb(manifest, decision):
            raise ExecutionRefused("operator declined — proposal left pending", decision)

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
        override_used=override_was_load_bearing,
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
    detail = await restore(adapter, plan)
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
