"""Retention for probe history: observations and the runs that produced them.

Every probe run appends rows and nothing removed them, so each reconcile and
each schedule pass read a history that only grew. ``prune_history`` runs with
the hourly usage job and deletes what is older than the horizon and no longer
load-bearing:

- an **observation** older than ``HOMELAB_HELPER_OBSERVATION_DAYS`` (default
  120) goes, unless it is the newest for its target, key and confidence tier.
  The reconciler's winner (confidence first, then recency) and an assertion's
  latest reading are always among the rows kept, so a host that stopped
  reporting keeps its last known facts however old they are. The horizon stays
  above the 90 days of ``host.smart`` history disk health reads.
- a **discovery run** older than ``HOMELAB_HELPER_RUN_DAYS`` (default 120)
  goes once no observation or placement refers to it, unless it is the newest
  for its host and probe: the schedule reads that one for cadence.

Either variable set to ``0`` turns that half off. Findings are not pruned:
they are the record of what was wrong and when, and a resolved one is what a
recurrence reopens.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, exists, func, select

from homelab_helper.db.models import DiscoveryRun, Observation, Placement
from homelab_helper.engine.disk_health import HISTORY_WINDOW

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

OBSERVATION_DAYS_ENV = "HOMELAB_HELPER_OBSERVATION_DAYS"
RUN_DAYS_ENV = "HOMELAB_HELPER_RUN_DAYS"
DEFAULT_DAYS = 120


def horizon(env_var: str, *, floor: timedelta = timedelta(0)) -> timedelta | None:
    """The configured horizon, never below ``floor``; ``None`` when set to 0 (off)."""
    raw = os.environ.get(env_var)
    days = int(raw) if raw and raw.isdigit() else DEFAULT_DAYS
    if days == 0:
        return None
    return max(timedelta(days=days), floor)


async def prune_observations(session: AsyncSession, cutoff: datetime) -> int:
    newest_first = (
        func.row_number()
        .over(
            partition_by=(
                Observation.target_type,
                Observation.target_id,
                Observation.key,
                Observation.confidence,
            ),
            order_by=(Observation.recorded_at.desc(), Observation.id.desc()),
        )
        .label("rank")
    )
    ranked = select(Observation.id, Observation.recorded_at, newest_first).subquery()
    stale = select(ranked.c.id).where(ranked.c.rank > 1, ranked.c.recorded_at < cutoff)
    res = await session.execute(delete(Observation).where(Observation.id.in_(stale)))
    return int(getattr(res, "rowcount", 0) or 0)


async def prune_runs(session: AsyncSession, cutoff: datetime) -> int:
    newest_first = (
        func.row_number()
        .over(
            partition_by=(DiscoveryRun.host_id, DiscoveryRun.probe_name),
            order_by=(DiscoveryRun.started_at.desc(), DiscoveryRun.id.desc()),
        )
        .label("rank")
    )
    ranked = select(DiscoveryRun.id, DiscoveryRun.started_at, newest_first).subquery()
    stale = select(ranked.c.id).where(ranked.c.rank > 1, ranked.c.started_at < cutoff)
    res = await session.execute(
        delete(DiscoveryRun).where(
            DiscoveryRun.id.in_(stale),
            ~exists().where(Observation.run_id == DiscoveryRun.id),
            ~exists().where(Placement.discovery_run_id == DiscoveryRun.id),
        )
    )
    return int(getattr(res, "rowcount", 0) or 0)


async def prune_history(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Delete observations and runs past their horizon; returns rows removed."""
    current = now or datetime.now(UTC)
    removed = {"observations": 0, "runs": 0}
    keep = horizon(OBSERVATION_DAYS_ENV, floor=HISTORY_WINDOW + timedelta(days=1))
    if keep is not None:
        removed["observations"] = await prune_observations(session, current - keep)
    keep = horizon(RUN_DAYS_ENV)
    if keep is not None:
        removed["runs"] = await prune_runs(session, current - keep)
    await session.flush()
    return removed


__all__ = [
    "DEFAULT_DAYS",
    "OBSERVATION_DAYS_ENV",
    "RUN_DAYS_ENV",
    "horizon",
    "prune_history",
    "prune_observations",
    "prune_runs",
]
