"""Usage history (Phase 8.3) — hypervisor round-robin data → bounded rollups.

Proxmox already keeps usage for every node and guest in RRD files: 30-minute
points for a month and 6-hour points for a year, cluster-wide, so a guest's
history follows it across migrations. This module turns those points into
``UsageSample`` rollups:

- ``hour`` buckets from the ``month`` timeframe (mean of the AVERAGE points,
  peak of the MAX points);
- ``day`` buckets from the ``year`` timeframe.

The first run therefore backfills about thirty days of hourly and a year of
daily history; later runs re-upsert the overlapping buckets (idempotent on
``subject_type, subject_key, resolution, ts``), so a missed run loses nothing
the source still holds. ``prune_usage`` deletes rows older than the per-
resolution horizon, which is what keeps the table bounded.

``summarize`` is the read side the planners use: nearest-rank p95 and peak of
CPU and memory over a window, plus the latest allocation, and how many buckets
backed the answer — a caller can refuse to recommend on thin history.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from homelab_helper.db.models import UsageSample

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

HOUR = "hour"
DAY = "day"
BUCKET_SECONDS = {HOUR: 3600, DAY: 86400}
SOURCE_TIMEFRAME = {HOUR: "month", DAY: "year"}
DEFAULT_RETENTION = {HOUR: timedelta(days=45), DAY: timedelta(days=730)}
RETENTION_ENV = {HOUR: "HOMELAB_HELPER_USAGE_HOURLY_DAYS", DAY: "HOMELAB_HELPER_USAGE_DAILY_DAYS"}
P95 = 0.95

# Source field → column, per subject type. Unmapped numeric fields listed in
# EXTRA keep their bucket mean in ``extra``.
NODE_FIELDS = {
    "cpu": "cpu",
    "maxcpu": "cpus",
    "memused": "mem_used",
    "memtotal": "mem_total",
    "rootused": "disk_used",
    "roottotal": "disk_total",
    "netin": "net_in",
    "netout": "net_out",
}
GUEST_FIELDS = {
    "cpu": "cpu",
    "maxcpu": "cpus",
    "mem": "mem_used",
    "maxmem": "mem_total",
    "disk": "disk_used",
    "maxdisk": "disk_total",
    "netin": "net_in",
    "netout": "net_out",
    "diskread": "disk_read",
    "diskwrite": "disk_write",
}
STORAGE_FIELDS = {"used": "disk_used", "total": "disk_total"}
"""A pool's RRD carries only these two; 8.5 projects time-to-full from them."""
NODE_EXTRA = ("iowait", "loadavg", "pressurecpusome", "pressurememorysome", "pressureiosome")
_INT_COLUMNS = {"mem_used", "mem_total", "disk_used", "disk_total"}


def retention(resolution: str) -> timedelta:
    raw = os.environ.get(RETENTION_ENV[resolution])
    return timedelta(days=int(raw)) if raw and raw.isdigit() else DEFAULT_RETENTION[resolution]


@dataclass
class Bucket:
    ts: datetime
    values: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, float] = field(default_factory=dict)


def _bucket_start(epoch: float, seconds: int) -> datetime:
    return datetime.fromtimestamp(int(epoch) // seconds * seconds, tz=UTC).replace(tzinfo=None)


def _naive_utc(ts: datetime) -> datetime:
    """Rows come back naive from SQLite and aware from Postgres; buckets are naive UTC."""
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo is not None else ts


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def rollup(
    average: list[dict[str, Any]],
    peak: list[dict[str, Any]],
    *,
    resolution: str,
    fields: dict[str, str],
    extra: tuple[str, ...] = (),
) -> list[Bucket]:
    """Bucket RRD points: means of AVERAGE points, peaks of MAX points.

    A point without the subject's leading field (``cpu`` for nodes and guests,
    ``used`` for a storage pool) carries no data — the guest was stopped or did
    not exist yet — and is skipped, so empty buckets are never written.
    """
    seconds = BUCKET_SECONDS[resolution]
    presence = next(iter(fields))
    grouped: dict[datetime, list[dict[str, Any]]] = {}
    for p in average:
        if p.get(presence) is None:
            continue
        grouped.setdefault(_bucket_start(p["time"], seconds), []).append(p)
    peaks: dict[datetime, list[dict[str, Any]]] = {}
    for p in peak:
        if p.get(presence) is None:
            continue
        peaks.setdefault(_bucket_start(p["time"], seconds), []).append(p)

    mem_key = next((k for k, v in fields.items() if v == "mem_used"), None)
    out: list[Bucket] = []
    for ts in sorted(grouped):
        points = grouped[ts]
        b = Bucket(ts=ts)
        for src, col in fields.items():
            value = _mean([float(p[src]) for p in points if p.get(src) is not None])
            if value is None:
                continue
            b.values[col] = round(value) if col in _INT_COLUMNS else value
        for key in extra:
            value = _mean([float(p[key]) for p in points if p.get(key) is not None])
            if value is not None:
                b.extra[key] = value
        top = peaks.get(ts) or points
        cpu_peaks = [float(p["cpu"]) for p in top if p.get("cpu") is not None]
        mem_peaks = (
            [float(p[mem_key]) for p in top if p.get(mem_key) is not None] if mem_key else []
        )
        if "cpu" in fields:
            b.values["cpu_max"] = max(cpu_peaks) if cpu_peaks else b.values.get("cpu")
        if mem_peaks:
            b.values["mem_used_max"] = round(max(mem_peaks))
        out.append(b)
    return out


@dataclass
class UsageWrite:
    inserted: int = 0
    updated: int = 0


async def record_usage(
    session: AsyncSession,
    *,
    subject_type: str,
    subject_key: str,
    label: str | None,
    resolution: str,
    buckets: list[Bucket],
) -> UsageWrite:
    """Upsert buckets for one subject and resolution."""
    result = UsageWrite()
    if not buckets:
        return result
    existing = {
        _naive_utc(row.ts): row
        for row in (
            await session.execute(
                select(UsageSample).where(
                    UsageSample.subject_type == subject_type,
                    UsageSample.subject_key == subject_key,
                    UsageSample.resolution == resolution,
                    UsageSample.ts >= buckets[0].ts,
                    UsageSample.ts <= buckets[-1].ts,
                )
            )
        )
        .scalars()
        .all()
    }
    for b in buckets:
        row = existing.get(b.ts)
        if row is None:
            row = UsageSample(
                subject_type=subject_type,
                subject_key=subject_key,
                resolution=resolution,
                ts=b.ts,
            )
            session.add(row)
            result.inserted += 1
        else:
            result.updated += 1
        row.label = label
        for col, value in b.values.items():
            setattr(row, col, value)
        row.extra = dict(b.extra)
    await session.flush()
    return result


async def prune_usage(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Delete rows older than each resolution's horizon; returns rows removed."""
    current = (now or datetime.now(UTC)).replace(tzinfo=None)
    removed: dict[str, int] = {}
    for resolution in (HOUR, DAY):
        cutoff = current - retention(resolution)
        res = await session.execute(
            delete(UsageSample).where(UsageSample.resolution == resolution, UsageSample.ts < cutoff)
        )
        removed[resolution] = int(getattr(res, "rowcount", 0) or 0)
    await session.flush()
    return removed


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; ``None`` for no data."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


async def pool_history(
    session: AsyncSession,
    storage: str,
    *,
    window: timedelta = timedelta(days=30),
    resolution: str = DAY,
    now: datetime | None = None,
) -> list[UsageSample]:
    """A storage pool's samples over ``window``, oldest first (for 8.5's slope).

    Daily by default: an hourly series makes the fit chase backup churn, while
    a day's mean is what actually moves a pool.
    """
    since = (now or datetime.now(UTC)).replace(tzinfo=None) - window
    return list(
        (
            await session.execute(
                select(UsageSample)
                .where(
                    UsageSample.subject_type == "storage",
                    UsageSample.subject_key == storage,
                    UsageSample.resolution == resolution,
                    UsageSample.ts >= since,
                )
                .order_by(UsageSample.ts)
            )
        )
        .scalars()
        .all()
    )


def _same_allocation(a: UsageSample, b: UsageSample) -> bool:
    return a.cpus == b.cpus and a.mem_total == b.mem_total


async def summarize(
    session: AsyncSession,
    *,
    subject_type: str,
    subject_key: str,
    window: timedelta = timedelta(days=30),
    resolution: str = HOUR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """p95 and peak of CPU and memory over ``window``, with the latest allocation.

    The figures cover only the buckets since the allocation last changed: a
    guest resized mid-window is judged by what it did with its *current* cores
    and memory, not by a history that mixes two sizes. ``allocation_changed_at``
    names that moment when it falls inside the window; ``samples_in_window``
    is the count before the cut.
    """
    since = (now or datetime.now(UTC)).replace(tzinfo=None) - window
    rows = (
        (
            await session.execute(
                select(UsageSample)
                .where(
                    UsageSample.subject_type == subject_type,
                    UsageSample.subject_key == subject_key,
                    UsageSample.resolution == resolution,
                    UsageSample.ts >= since,
                )
                .order_by(UsageSample.ts)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {"subject": subject_key, "samples": 0}
    in_window = len(rows)
    latest = rows[-1]
    changed_at: datetime | None = None
    cut = len(rows) - 1
    while cut > 0 and _same_allocation(rows[cut - 1], latest):
        cut -= 1
    if cut > 0:
        changed_at = rows[cut].ts
        rows = rows[cut:]
    cpu = [r.cpu for r in rows if r.cpu is not None]
    cpu_peak = [r.cpu_max for r in rows if r.cpu_max is not None]
    mem = [float(v) for r in rows if (v := r.mem_used_max or r.mem_used)]
    net = [(r.net_in or 0.0) + (r.net_out or 0.0) for r in rows if r.net_in is not None]
    return {
        "subject": subject_key,
        "label": latest.label,
        "resolution": resolution,
        "samples": len(rows),
        "samples_in_window": in_window,
        "allocation_changed_at": changed_at.isoformat() if changed_at else None,
        "first": rows[0].ts.isoformat(),
        "last": latest.ts.isoformat(),
        "cpus": latest.cpus,
        "cpu_p95": percentile(cpu, P95),
        "cpu_peak": max(cpu_peak) if cpu_peak else None,
        "mem_total": latest.mem_total,
        "mem_p95": percentile(mem, P95),
        "mem_peak": max(mem) if mem else None,
        "net_mean": sum(net) / len(net) if net else None,
    }


__all__ = [
    "DAY",
    "GUEST_FIELDS",
    "HOUR",
    "NODE_EXTRA",
    "NODE_FIELDS",
    "SOURCE_TIMEFRAME",
    "STORAGE_FIELDS",
    "Bucket",
    "UsageWrite",
    "percentile",
    "pool_history",
    "prune_usage",
    "record_usage",
    "retention",
    "rollup",
    "summarize",
]
