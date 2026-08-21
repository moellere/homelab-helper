"""Trust gradient — ``decide()`` and its supporting machinery (Phase 6).

The single locked invariant, from ``docs/architecture.md``:

    The LLM is on the untrusted side of the boundary, so an LLM is never in
    the path that authorizes execution.

:func:`decide` is a **pure function** over plain dataclasses — no session, no
network, no LLM, nothing nondeterministic. ``load_trust_context`` does the DB
reads; ``decide`` does the judging; the split keeps the policy exhaustively
unit-testable. A regression test asserts this module never (transitively)
imports ``homelab_helper.llm``.

The decision walks three tiers of floor hardness, clamping downward:

1. **Cell level** (moves): the ``CellTrust`` row for
   ``(domain, action_kind, blast_radius)``, defaulting to the domain's
   ``default_level`` — which defaults to PROPOSE. L1 is this function with
   every cell at PROPOSE.
2. **Soft-hard floors** (crossable by an open elevation window):
   the domain's ``max_level``, each affected host's ``TrustBoundary``
   ceiling, and the verified-rollback requirement (AUTONOMOUS without a
   verified rollback degrades to CONFIRM).
3. **Absolute floors** (crossable only by editing policy config):
   ``Domain.is_absolute`` and ``TrustBoundary.absolute``. A window is never
   consulted for these.

Every clamp appends a human-readable reason, so a receipt can show *why* an
action ran at the level it did — the decision is explainable without a model.
"""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import AutonomyLevel, TrustDomain
from homelab_helper.db.models import (
    CellTrust,
    Domain,
    ElevationWindow,
    Host,
    TrustBoundary,
    TrustHistory,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

OPERATOR_ENV_VAR = "HOMELAB_HELPER_OPERATOR"

AUTONOMY_ORDER: dict[AutonomyLevel, int] = {
    AutonomyLevel.BLOCK: 0,
    AutonomyLevel.PROPOSE: 1,
    AutonomyLevel.CONFIRM: 2,
    AutonomyLevel.AUTONOMOUS: 3,
}

# Seed policy: every domain defaults to PROPOSE (AC1's floor). SECRETS is
# absolute with a PROPOSE ceiling — nothing in that domain ever executes
# without a policy-config edit, and no window or override applies.
_DOMAIN_SEEDS: dict[TrustDomain, tuple[AutonomyLevel, bool]] = {
    TrustDomain.INVENTORY_METADATA: (AutonomyLevel.AUTONOMOUS, False),
    TrustDomain.CONTAINERS: (AutonomyLevel.AUTONOMOUS, False),
    TrustDomain.DNS: (AutonomyLevel.AUTONOMOUS, False),
    TrustDomain.NETWORK_FABRIC: (AutonomyLevel.AUTONOMOUS, False),
    TrustDomain.STORAGE: (AutonomyLevel.AUTONOMOUS, False),
    TrustDomain.HYPERVISOR: (AutonomyLevel.AUTONOMOUS, False),
    TrustDomain.HOST_OS: (AutonomyLevel.AUTONOMOUS, False),
    TrustDomain.SECRETS: (AutonomyLevel.PROPOSE, True),
}


def operator_identity() -> str:
    """The owner identity actions and grants are attributed to.

    ``HOMELAB_HELPER_OPERATOR`` wins; otherwise the OS user. The trust model
    is deliberately local: whoever can run this CLI against this DB is the
    owner. Anything stronger is future work, documented as such.
    """
    return os.environ.get(OPERATOR_ENV_VAR) or getpass.getuser()


@dataclass(frozen=True)
class ActionRequest:
    """One proposed action, as the gradient sees it (mirrors ProposalLog)."""

    domain: TrustDomain
    action_kind: str
    blast_radius: str
    hostnames: tuple[str, ...] = ()
    rollback_verified: bool = False
    provenance: str = "framework"

    @property
    def cell_key(self) -> str:
        return f"{self.domain.value}/{self.action_kind}/{self.blast_radius}"


@dataclass(frozen=True)
class BoundaryView:
    hostname: str
    ceiling: AutonomyLevel
    absolute: bool


@dataclass(frozen=True)
class WindowView:
    window_id: str
    scope: dict[str, Any]
    expires_at: datetime
    revoked_at: datetime | None

    def is_open(self, at: datetime) -> bool:
        return self.revoked_at is None and at < self.expires_at

    def covers(self, action: ActionRequest) -> bool:
        """Scope match: by domain, by any affected host, or by exact cell."""
        if action.domain.value in (self.scope.get("domains") or []):
            return True
        scope_hosts = set(self.scope.get("hosts") or [])
        if scope_hosts.intersection(action.hostnames):
            return True
        return action.cell_key in (self.scope.get("cells") or [])


@dataclass(frozen=True)
class TrustContext:
    """Everything decide() may consider — loaded from the DB, then frozen."""

    cell_level: AutonomyLevel
    cell_granted: bool
    cell_on_probation: bool
    domain_default: AutonomyLevel
    domain_max: AutonomyLevel
    domain_absolute: bool
    boundaries: tuple[BoundaryView, ...] = ()
    windows: tuple[WindowView, ...] = ()
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class Decision:
    level: AutonomyLevel
    reasons: tuple[str, ...]
    window_id: str | None = None


def _clamp(
    level: AutonomyLevel, ceiling: AutonomyLevel, reasons: list[str], why: str
) -> AutonomyLevel:
    if AUTONOMY_ORDER[level] > AUTONOMY_ORDER[ceiling]:
        reasons.append(f"clamped {level.value} → {ceiling.value}: {why}")
        return ceiling
    return level


def _matching_window(action: ActionRequest, context: TrustContext) -> WindowView | None:
    """The first open window covering this action — absolute domains get none."""
    if context.domain_absolute:
        return None
    for window in context.windows:
        if window.is_open(context.now) and window.covers(action):
            return window
    return None


def _apply_boundaries(
    level: AutonomyLevel,
    boundaries: tuple[BoundaryView, ...],
    open_window: WindowView | None,
    reasons: list[str],
) -> AutonomyLevel:
    """Host ceilings: absolute ones always clamp; others lift under a window."""
    for boundary in boundaries:
        if boundary.absolute:
            level = _clamp(
                level,
                boundary.ceiling,
                reasons,
                f"absolute boundary on {boundary.hostname} (window-proof)",
            )
        elif open_window is not None:
            reasons.append(
                f"boundary ceiling on {boundary.hostname} lifted by window {open_window.window_id}"
            )
        else:
            level = _clamp(
                level, boundary.ceiling, reasons, f"boundary ceiling on {boundary.hostname}"
            )
    return level


def decide(action: ActionRequest, context: TrustContext) -> Decision:
    """(action, context) → BLOCK | PROPOSE | CONFIRM | AUTONOMOUS. Pure."""
    reasons: list[str] = []

    level = context.cell_level
    if context.cell_granted:
        reasons.append(f"cell {action.cell_key} level: {level.value}")
    else:
        reasons.append(f"no grant for cell {action.cell_key}; domain default {level.value}")
    if context.cell_on_probation:
        level = _clamp(level, AutonomyLevel.CONFIRM, reasons, "cell is on probation")

    level = _clamp(
        level,
        context.domain_max,
        reasons,
        f"domain {action.domain.value} max_level"
        + (" (absolute)" if context.domain_absolute else ""),
    )

    open_window = _matching_window(action, context)
    level = _apply_boundaries(level, context.boundaries, open_window, reasons)

    if level == AutonomyLevel.AUTONOMOUS and not action.rollback_verified:
        if open_window is not None and not context.domain_absolute:
            reasons.append(
                f"no verified rollback, but window {open_window.window_id} is open — "
                "best-effort snapshot required before execution"
            )
        else:
            level = AutonomyLevel.CONFIRM
            reasons.append("no verified rollback: autonomous degrades to confirm")

    if level == AutonomyLevel.BLOCK:
        reasons.append("action is blocked by policy")

    return Decision(
        level=level,
        reasons=tuple(reasons),
        window_id=open_window.window_id if open_window is not None else None,
    )


# ------------------------------------------------------------------ DB side


async def seed_domains(session: AsyncSession) -> int:
    """Idempotently seed the domain taxonomy; returns rows created."""
    existing = {d.name for d in (await session.execute(select(Domain))).scalars().all()}
    created = 0
    for name, (max_level, absolute) in _DOMAIN_SEEDS.items():
        if name in existing:
            continue
        session.add(
            Domain(
                name=name,
                default_level=AutonomyLevel.PROPOSE,
                max_level=max_level,
                is_absolute=absolute,
            )
        )
        created += 1
    await session.flush()
    return created


async def load_trust_context(session: AsyncSession, action: ActionRequest) -> TrustContext:
    """Assemble the frozen context decide() judges against."""
    domain = await session.get(Domain, action.domain)
    domain_default = domain.default_level if domain else AutonomyLevel.PROPOSE
    domain_max = domain.max_level if domain else AutonomyLevel.PROPOSE
    domain_absolute = domain.is_absolute if domain else False

    cell = (
        await session.execute(
            select(CellTrust).where(
                CellTrust.domain == action.domain,
                CellTrust.action_kind == action.action_kind,
                CellTrust.blast_radius == action.blast_radius,
            )
        )
    ).scalar_one_or_none()

    boundaries: list[BoundaryView] = []
    if action.hostnames:
        hosts = (
            (await session.execute(select(Host).where(Host.hostname.in_(action.hostnames))))
            .scalars()
            .all()
        )
        host_ids = {h.id: h.hostname for h in hosts}
        rows = (
            (
                await session.execute(
                    select(TrustBoundary).where(TrustBoundary.host_id.in_(host_ids))
                )
            )
            .scalars()
            .all()
        )
        boundaries = [
            BoundaryView(
                hostname=host_ids[b.host_id],
                ceiling=b.max_agent_authority,
                absolute=b.absolute,
            )
            for b in rows
            if b.host_id in host_ids
        ]

    windows = [
        WindowView(
            window_id=str(w.id),
            scope=w.scope or {},
            expires_at=w.expires_at,
            revoked_at=w.revoked_at,
        )
        for w in (await session.execute(select(ElevationWindow))).scalars().all()
    ]

    return TrustContext(
        cell_level=cell.level if cell else domain_default,
        cell_granted=cell is not None,
        cell_on_probation=bool(cell.on_probation) if cell else False,
        domain_default=domain_default,
        domain_max=domain_max,
        domain_absolute=domain_absolute,
        boundaries=tuple(boundaries),
        windows=tuple(windows),
    )


class GrantError(ValueError):
    """A grant the policy refuses (above the domain ceiling, absolute domain)."""


async def grant_cell(
    session: AsyncSession,
    domain: TrustDomain,
    action_kind: str,
    blast_radius: str,
    level: AutonomyLevel,
    *,
    actor: str,
) -> CellTrust:
    """Explicit operator grant. Refuses to exceed the domain's ceiling."""
    domain_row = await session.get(Domain, domain)
    if domain_row is not None and AUTONOMY_ORDER[level] > AUTONOMY_ORDER[domain_row.max_level]:
        raise GrantError(
            f"{level.value} exceeds domain {domain.value} max_level "
            f"{domain_row.max_level.value}"
            + (" (absolute — edit policy config to change)" if domain_row.is_absolute else "")
        )
    cell = (
        await session.execute(
            select(CellTrust).where(
                CellTrust.domain == domain,
                CellTrust.action_kind == action_kind,
                CellTrust.blast_radius == blast_radius,
            )
        )
    ).scalar_one_or_none()
    if cell is None:
        cell = CellTrust(domain=domain, action_kind=action_kind, blast_radius=blast_radius)
        session.add(cell)
    cell.level = level
    cell.granted_by = actor
    cell.on_probation = False
    await session.flush()
    session.add(
        TrustHistory(
            actor=actor,
            event="grant",
            domain=domain,
            cell_trust_id=cell.id,
            detail={
                "action_kind": action_kind,
                "blast_radius": blast_radius,
                "level": level.value,
            },
        )
    )
    await session.flush()
    return cell


__all__ = [
    "AUTONOMY_ORDER",
    "OPERATOR_ENV_VAR",
    "ActionRequest",
    "BoundaryView",
    "Decision",
    "GrantError",
    "TrustContext",
    "WindowView",
    "decide",
    "grant_cell",
    "load_trust_context",
    "operator_identity",
    "seed_domains",
]
