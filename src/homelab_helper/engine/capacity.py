"""Capacity trends (Phase 10.2) — when something runs out, not only that it is high.

8.5 projects Proxmox storage pools to full from the usage history. This module
does the same for everything else the history covers and turns the result
into ``capacity`` findings:

- ``filesystem-full`` — a node's root filesystem or a NAS filesystem already
  past ``FULL_WARN`` of its size. MEDIUM; HIGH past ``FULL_HIGH``. Judged on the
  newest sample, no trend needed.
- ``filesystem-headroom`` — a filesystem whose daily growth reaches full
  within ``HEADROOM_MEDIUM_DAYS`` (MEDIUM) or ``HEADROOM_HIGH_DAYS`` (HIGH),
  from the same least-squares fit 8.5 uses.
- ``memory-headroom`` — a node whose daily mean memory use trends across
  ``MEMORY_LINE`` of its RAM inside the horizon.
- ``cpu-headroom`` — a node whose daily mean CPU trends across ``CPU_LINE``
  inside the horizon.

Filesystem samples for a NAS come from ``record_filesystem_samples``, which
the OpenMediaVault discoverers call once per run (one daily bucket, upserted),
so the NAS accrues the history the fit needs. Nodes and pools already have
theirs from 8.3. Every rule is a stated line or horizon on the finding; a
subject with fewer than ``MIN_HEADROOM_SAMPLES`` daily samples gets no trend
verdict and is not counted as observed for it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.db.models import Host, UsageSample
from homelab_helper.engine.category_findings import (
    CategoryIssue,
    CategoryResult,
    reconcile_category_findings,
)
from homelab_helper.engine.storage import (
    HEADROOM_HIGH_DAYS,
    HEADROOM_MEDIUM_DAYS,
    MIN_HEADROOM_SAMPLES,
    Projection,
    project_headroom,
)
from homelab_helper.engine.usage import DAY, Bucket, UsageWrite, record_usage

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

CATEGORIES = ("filesystem-full", "filesystem-headroom", "memory-headroom", "cpu-headroom")
FULL_WARN = 0.85
FULL_HIGH = 0.95
MEMORY_LINE = 0.90
CPU_LINE = 0.80
TREND_WINDOW = timedelta(days=60)
_GIB = 1024**3
_RATIO_SCALE = 10**11
"""CPU is a fraction; the fit wants integers and has a bytes-per-day floor, so ratios are
scaled until a slope of 0.001/day clears it and 0.0001/day does not."""


def _human(nbytes: float) -> str:
    return f"{nbytes / _GIB:.1f} GiB"


def _issue(category: str, target_type: str, target_id: str, **kw: Any) -> CategoryIssue:
    return CategoryIssue(
        kind=FindingKind.CAPACITY,
        category=category,
        target_type=target_type,
        target_id=target_id,
        **kw,
    )


def _severity_for(days: float) -> FindingSeverity:
    return FindingSeverity.HIGH if days <= HEADROOM_HIGH_DAYS else FindingSeverity.MEDIUM


def filesystem_issues(
    label: str,
    target_type: str,
    target_id: str,
    points: Sequence[tuple[datetime, int]],
    total: int,
    *,
    now: datetime,
) -> list[CategoryIssue]:
    """``filesystem-full`` on the newest sample, ``filesystem-headroom`` on the fit."""
    issues: list[CategoryIssue] = []
    if not points or not total:
        return issues
    used = points[-1][1]
    ratio = used / total
    if ratio >= FULL_WARN:
        issues.append(
            _issue(
                "filesystem-full",
                target_type,
                target_id,
                severity=FindingSeverity.HIGH if ratio >= FULL_HIGH else FindingSeverity.MEDIUM,
                title=f"{label} is {ratio:.0%} full",
                description=(
                    f"{label} holds {_human(used)} of {_human(total)}. Past {FULL_WARN:.0%} a "
                    "filesystem starts hurting (fragmentation, failed writes under burst); "
                    "free space or grow it."
                ),
                evidence={"used": used, "total": total, "ratio": round(ratio, 3)},
            )
        )
    projection = project_headroom(points, total)
    if projection is not None and projection.days_to_full is not None:
        days = projection.days_to_full
        if days <= HEADROOM_MEDIUM_DAYS:
            full_on = now + timedelta(days=days)
            issues.append(
                _issue(
                    "filesystem-headroom",
                    target_type,
                    target_id,
                    severity=_severity_for(days),
                    title=f"{label} fills in about {days:.0f} day(s)",
                    description=(
                        f"{_human(projection.used)} of {_human(projection.total)} used "
                        f"({projection.ratio:.0%}), growing {_human(projection.bytes_per_day)}/day "
                        f"over {projection.samples} daily sample(s) — full around "
                        f"{full_on:%Y-%m-%d} at this rate."
                    ),
                    evidence={
                        "used": projection.used,
                        "total": projection.total,
                        "bytes_per_day": round(projection.bytes_per_day),
                        "days_to_full": round(days, 1),
                        "samples": projection.samples,
                    },
                )
            )
    return issues


def _line_issue(
    category: str,
    label: str,
    what: str,
    target_id: str,
    projection: Projection | None,
    line: float,
    unit: str,
    *,
    now: datetime,
) -> CategoryIssue | None:
    if projection is None or projection.days_to_full is None:
        return None
    days = projection.days_to_full
    if days > HEADROOM_MEDIUM_DAYS:
        return None
    crosses = now + timedelta(days=days)
    current = projection.used / projection.total * line if projection.total else 0.0
    return _issue(
        category,
        "host",
        target_id,
        severity=_severity_for(days),
        title=f"{label}: {what} crosses {line:.0%} in about {days:.0f} day(s)",
        description=(
            f"{label}'s daily mean {what} is {current:.0%} of {unit} and rising over "
            f"{projection.samples} daily sample(s); at this rate it crosses {line:.0%} around "
            f"{crosses:%Y-%m-%d}. Plan the move, the DIMM or the retirement before then."
        ),
        evidence={
            "line": line,
            "current_ratio": round(current, 3),
            "days_to_line": round(days, 1),
            "samples": projection.samples,
        },
    )


def node_issues(
    label: str,
    target_id: str,
    samples: Sequence[UsageSample],
    *,
    now: datetime,
) -> list[CategoryIssue]:
    """Memory and CPU trend findings for one node's daily samples."""
    issues: list[CategoryIssue] = []
    mem = [(s.ts, int(s.mem_used)) for s in samples if s.mem_used is not None and s.mem_total]
    total = next((int(s.mem_total) for s in reversed(samples) if s.mem_total), 0)
    if mem and total:
        line_bytes = int(total * MEMORY_LINE)
        issue = _line_issue(
            "memory-headroom",
            label,
            "memory use",
            target_id,
            project_headroom(mem, line_bytes),
            MEMORY_LINE,
            "its RAM",
            now=now,
        )
        if issue is not None:
            issues.append(issue)
    cpu = [(s.ts, int(float(s.cpu) * _RATIO_SCALE)) for s in samples if s.cpu is not None]
    if cpu:
        issue = _line_issue(
            "cpu-headroom",
            label,
            "CPU",
            target_id,
            project_headroom(cpu, int(CPU_LINE * _RATIO_SCALE)),
            CPU_LINE,
            "its cores",
            now=now,
        )
        if issue is not None:
            issues.append(issue)
    return issues


async def record_filesystem_samples(
    session: AsyncSession,
    appliance: str,
    filesystems: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> UsageWrite:
    """One daily bucket per mounted filesystem, upserted — the NAS's trend history."""
    moment = (now or datetime.now(UTC)).replace(tzinfo=None)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    total = UsageWrite()
    for fs in filesystems:
        mount = fs.get("mountpoint")
        size, used = fs.get("size_bytes"), fs.get("used_bytes")
        if not mount or not isinstance(size, int) or not isinstance(used, int) or size <= 0:
            continue
        bucket = Bucket(ts=day, values={"disk_used": used, "disk_total": size})
        write = await record_usage(
            session,
            subject_type="filesystem",
            subject_key=f"{appliance}:{mount}",
            label=str(fs.get("label") or fs.get("device") or mount),
            resolution=DAY,
            buckets=[bucket],
        )
        total.inserted += write.inserted
        total.updated += write.updated
    return total


async def _daily(
    session: AsyncSession, subject_type: str, since: datetime
) -> dict[str, list[UsageSample]]:
    rows = (
        await session.execute(
            select(UsageSample)
            .where(
                UsageSample.subject_type == subject_type,
                UsageSample.resolution == DAY,
                UsageSample.ts >= since,
            )
            .order_by(UsageSample.subject_key, UsageSample.ts)
        )
    ).scalars()
    out: dict[str, list[UsageSample]] = {}
    for row in rows:
        out.setdefault(row.subject_key, []).append(row)
    return out


async def evaluate(
    session: AsyncSession, *, now: datetime | None = None
) -> tuple[list[CategoryIssue], set[str]]:
    """``(issues, observed categories)`` over nodes and NAS filesystems with history."""
    moment = now or datetime.now(UTC)
    since = moment.replace(tzinfo=None) - TREND_WINDOW
    host_ids = {
        h.hostname: str(h.id) for h in (await session.execute(select(Host))).scalars().all()
    }
    issues: list[CategoryIssue] = []
    observed: set[str] = set()
    for hostname, samples in (await _daily(session, "host", since)).items():
        target_id = host_ids.get(hostname, hostname)
        if len(samples) < MIN_HEADROOM_SAMPLES:
            continue
        observed |= {"memory-headroom", "cpu-headroom"}
        issues += node_issues(hostname, target_id, samples, now=moment)
        disk = [(s.ts, int(s.disk_used)) for s in samples if s.disk_used is not None]
        total = next((int(s.disk_total) for s in reversed(samples) if s.disk_total), 0)
        if disk and total:
            observed |= {"filesystem-full", "filesystem-headroom"}
            issues += filesystem_issues(f"{hostname}:/", "host", target_id, disk, total, now=moment)
    for key, samples in (await _daily(session, "filesystem", since)).items():
        if len(samples) < MIN_HEADROOM_SAMPLES:
            continue
        disk = [(s.ts, int(s.disk_used)) for s in samples if s.disk_used is not None]
        total = next((int(s.disk_total) for s in reversed(samples) if s.disk_total), 0)
        if disk and total:
            observed |= {"filesystem-full", "filesystem-headroom"}
            issues += filesystem_issues(key, "filesystem", key, disk, total, now=moment)
    return issues, observed


async def reconcile_capacity_findings(
    session: AsyncSession, *, now: datetime | None = None
) -> tuple[CategoryResult, list[CategoryIssue]]:
    when = now or datetime.now(UTC)
    issues, observed = await evaluate(session, now=when)
    result = await reconcile_category_findings(
        session, FindingKind.CAPACITY, issues, observed, when=when
    )
    return result, issues


__all__ = [
    "CATEGORIES",
    "CPU_LINE",
    "FULL_HIGH",
    "FULL_WARN",
    "MEMORY_LINE",
    "evaluate",
    "filesystem_issues",
    "node_issues",
    "reconcile_capacity_findings",
    "record_filesystem_samples",
]
