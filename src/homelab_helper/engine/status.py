"""Status snapshot — one read-only rollup of the harness for dashboards (Phase 9.7a).

``snapshot()`` answers "is anything wrong, and is anything waiting on me?" from
the database alone: open findings by severity and kind, action proposals
awaiting an operator, the newest discovery and assertion runs, trust cells by
level, the last day's receipts. ``helper status`` prints it and the HTTP
endpoint (``status_api``) serves it to a Homepage widget or a Home Assistant
REST sensor. Nothing here touches infrastructure or authority, and the result
only gains keys.

``health`` is a three-step traffic light: ``critical`` when a critical or high
finding is open or a receipt failed in the last day, ``attention`` when a
medium finding is open, a proposal awaits approval, or discovery has not run
within ``stale_after``, else ``ok``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from homelab_helper import __version__
from homelab_helper.db.enums import FindingSeverity, FindingStatus, ProposalOutcome
from homelab_helper.db.models import (
    AssertionRun,
    CellTrust,
    Cluster,
    DiscoveryRun,
    ExecutionReceipt,
    Host,
    ProposalLog,
    ReconciliationFinding,
    Service,
    VirtualMachine,
)
from homelab_helper.engine.trust import as_utc, open_windows

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

OPEN_STATUSES = frozenset({FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED})
SEVERITY_ORDER = [s.value for s in FindingSeverity]
DEFAULT_STALE_AFTER = timedelta(hours=12)
RECENT = timedelta(hours=24)
MAX_TITLES = 10


def _iso(dt: datetime | None) -> str | None:
    return as_utc(dt).isoformat() if dt is not None else None


def _age_seconds(dt: datetime | None, now: datetime) -> int | None:
    return int((now - as_utc(dt)).total_seconds()) if dt is not None else None


def _health(
    by_severity: dict[str, int],
    pending: int,
    failed_receipts: int,
    discovery_age: int | None,
    stale_after: timedelta,
) -> str:
    if by_severity.get("critical") or by_severity.get("high") or failed_receipts:
        return "critical"
    stale = discovery_age is None or discovery_age > stale_after.total_seconds()
    if by_severity.get("medium") or pending or stale:
        return "attention"
    return "ok"


def _headline(open_count: int, highest: str | None, pending: int, discovery_age: int | None) -> str:
    parts = [f"{open_count} open finding(s)" + (f", highest {highest}" if highest else "")]
    if pending:
        parts.append(f"{pending} proposal(s) awaiting approval")
    if discovery_age is None:
        parts.append("discovery has never run")
    else:
        parts.append(f"last discovery {discovery_age // 3600}h {discovery_age % 3600 // 60}m ago")
    return " · ".join(parts)


async def _count(session: AsyncSession, model: Any) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _findings(session: AsyncSession) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.status.in_(list(OPEN_STATUSES))
                )
            )
        )
        .scalars()
        .all()
    )
    by_severity = dict.fromkeys(SEVERITY_ORDER, 0)
    by_kind: dict[str, int] = {}
    for f in rows:
        by_severity[f.severity.value] += 1
        by_kind[f.kind.value] = by_kind.get(f.kind.value, 0) + 1
    highest = next((s for s in SEVERITY_ORDER if by_severity[s]), None)
    return {
        "open": len(rows),
        "highest": highest,
        "critical_or_high": by_severity["critical"] + by_severity["high"],
        "by_severity": by_severity,
        "by_kind": dict(sorted(by_kind.items())),
    }


async def _proposals(session: AsyncSession) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(ProposalLog)
                .where(ProposalLog.outcome == ProposalOutcome.PENDING)
                .order_by(ProposalLog.proposed_at)
            )
        )
        .scalars()
        .all()
    )
    actions = [p for p in rows if (p.artifact or {}).get("kind") == "action"]
    return {
        "pending": len(actions),
        "titles": [p.title for p in actions[:MAX_TITLES]],
        "oldest_at": _iso(actions[0].proposed_at) if actions else None,
    }


async def _discovery(session: AsyncSession, now: datetime) -> dict[str, Any]:
    last = (await session.execute(select(func.max(DiscoveryRun.started_at)))).scalar_one_or_none()
    since = now - RECENT
    recent = (
        (await session.execute(select(DiscoveryRun).where(DiscoveryRun.started_at >= since)))
        .scalars()
        .all()
    )
    per_probe = (
        await session.execute(
            select(DiscoveryRun.probe_name, func.max(DiscoveryRun.started_at)).group_by(
                DiscoveryRun.probe_name
            )
        )
    ).all()
    return {
        "last_run_at": _iso(last),
        "age_seconds": _age_seconds(last, now),
        "runs_24h": len(recent),
        "failed_24h": sum(1 for r in recent if r.success is False),
        "probes": {name: _iso(at) for name, at in sorted(per_probe)},
    }


async def _assertions(session: AsyncSession, now: datetime) -> dict[str, Any]:
    last = (await session.execute(select(func.max(AssertionRun.ran_at)))).scalar_one_or_none()
    return {"last_run_at": _iso(last), "age_seconds": _age_seconds(last, now)}


async def _trust(session: AsyncSession, now: datetime) -> dict[str, Any]:
    cells = (await session.execute(select(CellTrust))).scalars().all()
    by_level: dict[str, int] = {}
    for c in cells:
        by_level[c.level.value] = by_level.get(c.level.value, 0) + 1
    return {
        "cells": len(cells),
        "cells_by_level": dict(sorted(by_level.items())),
        "open_windows": len(await open_windows(session, at=now)),
    }


async def _receipts(session: AsyncSession, now: datetime) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(ExecutionReceipt).where(ExecutionReceipt.executed_at >= now - RECENT)
            )
        )
        .scalars()
        .all()
    )
    return {
        "succeeded": sum(1 for r in rows if r.outcome == "succeeded"),
        "failed": sum(1 for r in rows if r.outcome != "succeeded"),
        "last_at": _iso(max((r.executed_at for r in rows), default=None)),
    }


async def snapshot(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> dict[str, Any]:
    """The whole rollup; see the module docstring for ``health``."""
    moment = as_utc(now or datetime.now(UTC))
    findings = await _findings(session)
    proposals = await _proposals(session)
    discovery = await _discovery(session, moment)
    receipts = await _receipts(session, moment)
    return {
        "generated_at": moment.isoformat(),
        "version": __version__,
        "health": _health(
            findings["by_severity"],
            proposals["pending"],
            receipts["failed"],
            discovery["age_seconds"],
            stale_after,
        ),
        "headline": _headline(
            findings["open"], findings["highest"], proposals["pending"], discovery["age_seconds"]
        ),
        "findings": findings,
        "proposals": proposals,
        "discovery": discovery,
        "assertions": await _assertions(session, moment),
        "trust": await _trust(session, moment),
        "receipts_24h": receipts,
        "inventory": {
            "hosts": await _count(session, Host),
            "clusters": await _count(session, Cluster),
            "virtual_machines": await _count(session, VirtualMachine),
            "services": await _count(session, Service),
        },
    }


__all__ = ["DEFAULT_STALE_AFTER", "OPEN_STATUSES", "snapshot"]
