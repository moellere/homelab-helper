"""Auto-escalation — the moving floor's slow rise and instant fall (Phase 6).

The policy is quoted verbatim from ``docs/architecture.md`` ("The promotable
unit is a narrow cell"):

    Auto-escalation — applies *only* to reversible + low-blast cells
    (``blast ∈ {metadata-only, single-host}``): after N clean approvals the
    cell's default level rises one step. One bad outcome instantly demotes
    the cell to PROPOSE and flags it on probation. Trust accrues slowly,
    evaporates instantly.

Consequences that are easy to get wrong, so they are stated here and tested:

- **The ladder is one rung at a time.** PROPOSE → CONFIRM → AUTONOMOUS, never
  a double promotion, and the streak resets at each rung — five clean
  approvals buy one step, not a level per approval.
- **Eligibility is a property of the cell, not of the run.** An ineligible
  cell (wide blast radius, or an action kind not on the reversible list)
  accrues its streak for the operator to see but never promotes; only an
  explicit ``grant_cell`` moves it.
- **Demotion is universal.** It applies to explicitly granted cells too — a
  grant is not a shield. Recovery is an explicit operator re-grant, which is
  also the only thing that clears probation.
- **A rejected proposal is not a bad outcome.** It breaks the streak (the
  framework's judgment in that cell is not yet reliable) but never demotes:
  disagreeing with a recommendation is not the same as an action going wrong.
- **A blocked cell banks nothing.** On probation the streak stays at zero, and
  any other block (domain ceiling, top of ladder, ineligible) holds it at the
  threshold. Without this, a blocked cell would quietly accumulate credit and
  cash it the moment the block lifted.

Nothing here is on the authorization path — ``decide()`` reads the levels this
module writes, but this module never decides whether an action may run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from homelab_helper.db.enums import AutonomyLevel, TrustDomain
from homelab_helper.db.models import CellTrust, Domain, TrustHistory
from homelab_helper.engine.trust import AUTONOMY_ORDER

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

PROMOTION_STREAK = 5
"""Clean approvals that buy one rung. Owner-locked; not runtime-tunable."""

LOW_BLAST_RADII = frozenset({"metadata-only", "single-host"})
"""The only blast radii auto-escalation will touch."""

REVERSIBLE_ACTION_KINDS = frozenset({"start", "stop", "shutdown", "restart"})
"""Deliberately narrow: an action kind earns a place here only once its
inverse is a real, tested write path. Everything else needs a grant."""

_LADDER: tuple[AutonomyLevel, ...] = (
    AutonomyLevel.PROPOSE,
    AutonomyLevel.CONFIRM,
    AutonomyLevel.AUTONOMOUS,
)


@dataclass(frozen=True)
class EscalationResult:
    """What the outcome did to the cell — for receipts, CLI, and tests."""

    event: str
    """streak | auto-promote | demote | streak-reset | noop"""
    previous_level: AutonomyLevel
    level: AutonomyLevel
    clean_streak: int
    on_probation: bool
    reason: str

    @property
    def promoted(self) -> bool:
        return self.event == "auto-promote"

    @property
    def demoted(self) -> bool:
        return self.event == "demote"


def is_promotable(action_kind: str, blast_radius: str) -> bool:
    """Eligibility for auto-escalation: reversible action *and* low blast."""
    return action_kind in REVERSIBLE_ACTION_KINDS and blast_radius in LOW_BLAST_RADII


def next_level(level: AutonomyLevel) -> AutonomyLevel | None:
    """The next rung up, or None at the top (or off-ladder, e.g. BLOCK)."""
    if level not in _LADDER:
        return None
    index = _LADDER.index(level)
    return _LADDER[index + 1] if index + 1 < len(_LADDER) else None


async def _cell_for(
    session: AsyncSession,
    domain: TrustDomain,
    action_kind: str,
    blast_radius: str,
) -> CellTrust:
    """The cell row, created at the domain default if this is its first outcome."""
    cell = (
        await session.execute(
            select(CellTrust).where(
                CellTrust.domain == domain,
                CellTrust.action_kind == action_kind,
                CellTrust.blast_radius == blast_radius,
            )
        )
    ).scalar_one_or_none()
    if cell is not None:
        return cell

    domain_row = await session.get(Domain, domain)
    cell = CellTrust(
        domain=domain,
        action_kind=action_kind,
        blast_radius=blast_radius,
        level=domain_row.default_level if domain_row else AutonomyLevel.PROPOSE,
        clean_streak=0,
        on_probation=False,
    )
    session.add(cell)
    await session.flush()
    return cell


def _promotion_block(
    cell: CellTrust, domain_row: Domain | None, target: AutonomyLevel | None
) -> str | None:
    """Why this cell may not rise right now — None means it may."""
    if not is_promotable(cell.action_kind, cell.blast_radius):
        return (
            f"cell {cell.action_kind}/{cell.blast_radius} is not auto-promotable "
            "(needs a reversible action kind and a low blast radius); explicit grant only"
        )
    if domain_row is not None and domain_row.is_absolute:
        return f"domain {cell.domain.value} is absolute — no runtime promotion"
    if target is None:
        return "already at the top of the ladder"
    if domain_row is not None and AUTONOMY_ORDER[target] > AUTONOMY_ORDER[domain_row.max_level]:
        return (
            f"{target.value} exceeds domain {cell.domain.value} max_level "
            f"{domain_row.max_level.value}"
        )
    return None


async def record_clean_outcome(
    session: AsyncSession,
    *,
    domain: TrustDomain,
    action_kind: str,
    blast_radius: str,
    actor: str,
    proposal_id: uuid.UUID | None = None,
) -> EscalationResult:
    """One clean approval: extend the streak, and promote if it bought a rung."""
    cell = await _cell_for(session, domain, action_kind, blast_radius)
    previous = cell.level

    if cell.on_probation:
        # A cell on probation banks nothing: the clock does not start until an
        # explicit grant clears the flag. Otherwise probation would cost only
        # the wait, and the cell would cash a banked streak the instant it
        # lifted — the opposite of "trust accrues slowly".
        cell.clean_streak = 0
        await session.flush()
        return EscalationResult(
            event="noop",
            previous_level=previous,
            level=cell.level,
            clean_streak=0,
            on_probation=True,
            reason="cell is on probation — the streak stays at zero until an explicit grant",
        )

    cell.clean_streak += 1

    if cell.clean_streak < PROMOTION_STREAK:
        await session.flush()
        return EscalationResult(
            event="streak",
            previous_level=previous,
            level=cell.level,
            clean_streak=cell.clean_streak,
            on_probation=cell.on_probation,
            reason=f"{cell.clean_streak}/{PROMOTION_STREAK} clean approvals toward the next rung",
        )

    domain_row = await session.get(Domain, domain)
    target = next_level(cell.level)
    blocked = _promotion_block(cell, domain_row, target)
    if blocked is not None or target is None:
        # Hold at the threshold rather than banking indefinitely: if the block
        # ever lifts (a policy edit, a widened eligibility list), the cell owes
        # one more clean run, not a decade of backdated credit.
        cell.clean_streak = PROMOTION_STREAK
        await session.flush()
        return EscalationResult(
            event="noop",
            previous_level=previous,
            level=cell.level,
            clean_streak=cell.clean_streak,
            on_probation=cell.on_probation,
            reason=blocked or "already at the top of the ladder",
        )

    cell.level = target
    cell.clean_streak = 0
    cell.granted_by = None
    await session.flush()
    session.add(
        TrustHistory(
            actor=actor,
            event="auto-promote",
            domain=domain,
            cell_trust_id=cell.id,
            proposal_id=proposal_id,
            detail={
                "action_kind": action_kind,
                "blast_radius": blast_radius,
                "from": previous.value,
                "to": target.value,
                "clean_streak": PROMOTION_STREAK,
            },
        )
    )
    await session.flush()
    return EscalationResult(
        event="auto-promote",
        previous_level=previous,
        level=cell.level,
        clean_streak=0,
        on_probation=False,
        reason=(
            f"{PROMOTION_STREAK} clean approvals: {previous.value} → {target.value} (streak reset)"
        ),
    )


async def record_bad_outcome(
    session: AsyncSession,
    *,
    domain: TrustDomain,
    action_kind: str,
    blast_radius: str,
    actor: str,
    reason: str,
    proposal_id: uuid.UUID | None = None,
) -> EscalationResult:
    """One bad outcome: straight to PROPOSE, on probation, streak zeroed."""
    cell = await _cell_for(session, domain, action_kind, blast_radius)
    previous = cell.level
    cell.level = AutonomyLevel.PROPOSE
    cell.clean_streak = 0
    cell.on_probation = True
    cell.granted_by = None
    await session.flush()
    session.add(
        TrustHistory(
            actor=actor,
            event="demote",
            domain=domain,
            cell_trust_id=cell.id,
            proposal_id=proposal_id,
            detail={
                "action_kind": action_kind,
                "blast_radius": blast_radius,
                "from": previous.value,
                "to": AutonomyLevel.PROPOSE.value,
                "cause": reason,
            },
        )
    )
    await session.flush()
    return EscalationResult(
        event="demote",
        previous_level=previous,
        level=cell.level,
        clean_streak=0,
        on_probation=True,
        reason=f"bad outcome ({reason}): demoted to propose and flagged on probation",
    )


async def record_rejection(
    session: AsyncSession,
    *,
    domain: TrustDomain,
    action_kind: str,
    blast_radius: str,
) -> EscalationResult:
    """A rejected proposal: streak broken, level untouched, nothing logged.

    Rejection means the operator disagreed with a recommendation — evidence
    that this cell has not earned a rung, but not evidence that an action went
    wrong. The audit spine stays reserved for authority changes.
    """
    cell = await _cell_for(session, domain, action_kind, blast_radius)
    previous_streak = cell.clean_streak
    cell.clean_streak = 0
    await session.flush()
    return EscalationResult(
        event="streak-reset",
        previous_level=cell.level,
        level=cell.level,
        clean_streak=0,
        on_probation=cell.on_probation,
        reason=f"proposal rejected: streak reset (was {previous_streak})",
    )


__all__ = [
    "LOW_BLAST_RADII",
    "PROMOTION_STREAK",
    "REVERSIBLE_ACTION_KINDS",
    "EscalationResult",
    "is_promotable",
    "next_level",
    "record_bad_outcome",
    "record_clean_outcome",
    "record_rejection",
]
