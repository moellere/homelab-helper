"""Baselines and anomalies (Phase 10.3) — a subject departing from its own normal.

Every guest, node and (Phase 11.4) Kubernetes workload with hourly usage
history gets a baseline per metric —
CPU, network, disk I/O, memory — as percentile bands of its *own* last
``BASELINE_DAYS``: p10, p50, p95. A departure is the newest ``DEPARTURE_HOURS``
consecutive hourly samples all sitting outside the band by a margin that
matters:

- ``<metric>-above-baseline`` — every recent sample at or above
  ``p95 x ABOVE_FACTOR`` and above the metric's absolute floor. A service that
  started spinning, a guest suddenly chatty on the wire.
- ``cpu-below-baseline`` / ``network-below-baseline`` — every recent sample at
  or below ``p10 x BELOW_FACTOR``, and only when p10 itself clears the floor:
  a subject that is normally busy has gone quiet (a backup that silently
  stopped, a scraper that lost its target). Idle things cannot "go quiet".

Bands are percentiles, not models: the finding carries p10/p50/p95, the
recent values and the hours, so the operator argues with a number. The
whole day is one band — hour-of-day shape is a follow-up once a season of
history exists. LOW by default, MEDIUM once the departure has lasted
``SUSTAINED_HOURS``. Resolution is per category and per subject: a subject
back inside its band resolves its own finding; a subject with too little
history (or stale samples) is not judged and resolves nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.db.models import Cluster, Host, UsageSample
from homelab_helper.engine.category_findings import (
    CategoryIssue,
    CategoryResult,
    reconcile_category_findings,
)
from homelab_helper.engine.usage import HOUR, percentile

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

METRICS = ("cpu", "network", "disk-io", "memory")
CATEGORIES = (
    *(f"{m}-above-baseline" for m in METRICS),
    "cpu-below-baseline",
    "network-below-baseline",
)
BASELINE_DAYS = 28
MIN_HISTORY_HOURS = 14 * 24
HOURLY_MIN_SAMPLES = 14
_MAX_HOUR = 23
DEPARTURE_HOURS = 3
SUSTAINED_HOURS = 6
MAX_SAMPLE_AGE = timedelta(hours=6)
ABOVE_FACTOR = 1.5
BELOW_FACTOR = 0.5
FLOORS: dict[str, float] = {
    "cpu": 0.05,  # fraction of allocated cores
    "network": 100 * 1024.0,  # B/s, in + out
    "disk-io": 1024 * 1024.0,  # B/s, read + write
    "memory": 512 * 1024 * 1024.0,  # bytes above the band
}
_BELOW_METRICS = ("cpu", "network")


@dataclass(frozen=True)
class Band:
    metric: str
    p10: float
    p50: float
    p95: float
    samples: int


def _metric_value(sample: UsageSample, metric: str) -> float | None:
    pairs = {
        "network": (sample.net_in, sample.net_out),
        "disk-io": (sample.disk_read, sample.disk_write),
    }
    if metric in pairs:
        a, b = pairs[metric]
        return None if a is None and b is None else float(a or 0) + float(b or 0)
    single = {"cpu": sample.cpu, "memory": sample.mem_used}.get(metric)
    return float(single) if single is not None else None


def _fmt(metric: str, value: float) -> str:
    if metric == "cpu":
        return f"{value:.0%}"
    if metric == "memory":
        return f"{value / 1024**3:.1f} GiB"
    return f"{value / 1024:.0f} KB/s"


def _in_hour_window(hour: int, window: tuple[int, int]) -> bool:
    """True when ``hour`` (0..23) falls inside ``(start_hour, end_hour)`` inclusive."""
    start, end = window
    if start <= end:
        return start <= hour <= end
    return hour >= start or hour <= end


def expected_windows_from_env() -> dict[str, tuple[int, int]]:
    """Parse ``HOMELAB_HELPER_BASELINE_WINDOWS`` (``key=start-end,...``) into hour ranges."""
    raw = os.environ.get("HOMELAB_HELPER_BASELINE_WINDOWS") or ""
    out: dict[str, tuple[int, int]] = {}
    for entry in raw.split(","):
        item = entry.strip()
        if not item or "=" not in item:
            continue
        key, _, span = item.partition("=")
        start_s, sep, end_s = span.strip().partition("-")
        if not key.strip() or not sep:
            continue
        try:
            start_h, end_h = int(start_s), int(end_s)
        except ValueError:
            continue
        if 0 <= start_h <= _MAX_HOUR and 0 <= end_h <= _MAX_HOUR:
            out[key.strip()] = (start_h, end_h)
    return out


def band_for(
    history: Sequence[float], metric: str, *, min_samples: int = MIN_HISTORY_HOURS
) -> Band | None:
    if len(history) < min_samples:
        return None
    values = list(history)
    p10, p50, p95 = (percentile(values, q) for q in (0.10, 0.50, 0.95))
    if p10 is None or p50 is None or p95 is None:
        return None
    return Band(metric, p10, p50, p95, len(values))


def hourly_band_for(
    samples: Sequence[UsageSample],
    metric: str,
    hour: int,
    *,
    window_hours: int = 1,
    min_samples: int = HOURLY_MIN_SAMPLES,
) -> Band | None:
    """Percentile band from history samples within ``±window_hours`` of ``hour`` (UTC)."""
    matching = [
        v
        for s in samples
        if min((s.ts.hour - hour) % 24, (hour - s.ts.hour) % 24) <= window_hours
        and (v := _metric_value(s, metric)) is not None
    ]
    return band_for(matching, metric, min_samples=min_samples)


def _departure(recent: Sequence[float], band: Band) -> tuple[str, float] | None:
    """``("above" | "below", line)`` when every recent value is past the line."""
    floor = FLOORS[band.metric]
    above = max(band.p95 * ABOVE_FACTOR, band.p95 + floor)
    if all(v >= above for v in recent):
        return "above", above
    if band.metric in _BELOW_METRICS and band.p10 >= floor:
        below = band.p10 * BELOW_FACTOR
        if all(v <= below for v in recent):
            return "below", below
    return None


def _sustained(series: Sequence[float], direction: str, line: float) -> int:
    """How many trailing hours have been past the line."""
    count = 0
    for v in reversed(series):
        if (direction == "above" and v >= line) or (direction == "below" and v <= line):
            count += 1
        else:
            break
    return count


def _select_band(
    active_samples: Sequence[UsageSample],
    history: Sequence[float],
    metric: str,
    hour: int,
    *,
    hourly_bands: bool,
    expected_window: tuple[int, int] | None,
) -> Band | None:
    if expected_window is not None:
        return band_for(history, metric, min_samples=HOURLY_MIN_SAMPLES)
    if hourly_bands:
        return hourly_band_for(active_samples[:-DEPARTURE_HOURS], metric, hour) or band_for(
            history, metric
        )
    return band_for(history, metric)


def subject_issues(
    target_type: str,
    target_id: str,
    label: str,
    samples: Sequence[UsageSample],
    *,
    now: datetime,
    also_affects: tuple[tuple[str, str], ...] = (),
    hourly_bands: bool = False,
    expected_window: tuple[int, int] | None = None,
) -> tuple[list[CategoryIssue], set[str]]:
    """``(issues, categories judged)`` for one subject's hourly samples, oldest first."""
    issues: list[CategoryIssue] = []
    judged: set[str] = set()
    min_required = HOURLY_MIN_SAMPLES if expected_window is not None else MIN_HISTORY_HOURS
    if len(samples) < min_required + DEPARTURE_HOURS:
        return issues, judged
    newest = samples[-1].ts
    newest_utc = newest if newest.tzinfo else newest.replace(tzinfo=UTC)
    if now - newest_utc > MAX_SAMPLE_AGE:
        return issues, judged  # stale history is not an anomaly, and not a verdict either
    active_samples: Sequence[UsageSample] = (
        [
            s
            for s in samples
            if _in_hour_window(s.ts.hour, expected_window)
            == _in_hour_window(newest.hour, expected_window)
        ]
        if expected_window is not None
        else samples
    )
    for metric in METRICS:
        series = [v for s in active_samples if (v := _metric_value(s, metric)) is not None]
        if len(series) < min_required + DEPARTURE_HOURS:
            continue
        recent, history = series[-DEPARTURE_HOURS:], series[:-DEPARTURE_HOURS]
        band = _select_band(
            active_samples,
            history,
            metric,
            newest.hour,
            hourly_bands=hourly_bands,
            expected_window=expected_window,
        )
        if band is None:
            continue
        judged.add(f"{metric}-above-baseline")
        if metric in _BELOW_METRICS:
            judged.add(f"{metric}-below-baseline")
        hit = _departure(recent, band)
        if hit is None:
            continue
        direction, line = hit
        hours = _sustained(series, direction, line)
        severity = FindingSeverity.MEDIUM if hours >= SUSTAINED_HOURS else FindingSeverity.LOW
        latest = recent[-1]
        what = {
            "cpu": "CPU",
            "network": "network traffic",
            "disk-io": "disk I/O",
            "memory": "memory use",
        }[metric]
        if direction == "above":
            title = f"{label}: {what} at {_fmt(metric, latest)}, above its usual {_fmt(metric, band.p95)}"
            description = (
                f"{label}'s {what} has been at or above {_fmt(metric, line)} for {hours} hour(s); "
                f"over its own last {BASELINE_DAYS} days it ran {_fmt(metric, band.p10)} to "
                f"{_fmt(metric, band.p95)} (median {_fmt(metric, band.p50)}). Something changed "
                "— look at what it is doing before it becomes a capacity problem."
            )
        else:
            title = f"{label}: {what} at {_fmt(metric, latest)}, far below its usual {_fmt(metric, band.p10)}"
            description = (
                f"{label}'s {what} has been at or below {_fmt(metric, line)} for {hours} hour(s), "
                f"where its own last {BASELINE_DAYS} days never dropped under "
                f"{_fmt(metric, band.p10)} (median {_fmt(metric, band.p50)}). A job that stopped, "
                "a client that went away, or a service that is no longer being asked."
            )
        band_ev: dict[str, int | float | list[int] | bool] = {
            "p10": round(band.p10, 3),
            "p50": round(band.p50, 3),
            "p95": round(band.p95, 3),
            "samples": band.samples,
            "days": BASELINE_DAYS,
        }
        if expected_window is not None:
            band_ev["expected_window"] = list(expected_window)
        if hourly_bands:
            band_ev["hourly"] = True
        issues.append(
            CategoryIssue(
                kind=FindingKind.ANOMALY,
                category=f"{metric}-{direction}-baseline",
                target_type=target_type,
                target_id=target_id,
                severity=severity,
                title=title[:512],
                description=description,
                evidence={
                    "metric": metric,
                    "direction": direction,
                    "line": round(line, 3),
                    "recent": [round(v, 3) for v in recent],
                    "hours": hours,
                    "band": band_ev,
                    "newest_sample": newest.isoformat(),
                },
                also_affects=also_affects,
            )
        )
    return issues, judged


async def _hourly(
    session: AsyncSession, since: datetime
) -> dict[tuple[str, str], list[UsageSample]]:
    rows = (
        await session.execute(
            select(UsageSample)
            .where(
                UsageSample.subject_type.in_(["guest", "host", "workload"]),
                UsageSample.resolution == HOUR,
                UsageSample.ts >= since,
            )
            .order_by(UsageSample.subject_key, UsageSample.ts)
        )
    ).scalars()
    out: dict[tuple[str, str], list[UsageSample]] = {}
    for row in rows:
        out.setdefault((row.subject_type, row.subject_key), []).append(row)
    return out


async def evaluate(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    hourly_bands: bool = False,
    expected_windows: dict[str, tuple[int, int]] | None = None,
) -> tuple[list[CategoryIssue], set[str], set[tuple[str, str]]]:
    """``(issues, categories observed, subjects judged)`` over every subject with history."""
    moment = now or datetime.now(UTC)
    since = moment.replace(tzinfo=None) - timedelta(days=BASELINE_DAYS)
    windows = expected_windows if expected_windows is not None else expected_windows_from_env()
    hosts = {h.hostname: h for h in (await session.execute(select(Host))).scalars().all()}
    clusters = {c.name: c for c in (await session.execute(select(Cluster))).scalars().all()}
    issues: list[CategoryIssue] = []
    observed: set[str] = set()
    judged: set[tuple[str, str]] = set()
    for (subject_type, key), samples in (await _hourly(session, since)).items():
        extra: tuple[tuple[str, str], ...] = ()
        if subject_type == "host":
            host = hosts.get(key)
            target = ("host", str(host.id) if host else key)
            label = key
        elif subject_type == "workload":
            target = ("workload", key)
            label = key
        else:
            cluster_name, _, vmid = key.rpartition("/")
            target = ("guest", key)
            label = (samples[-1].label or key) + f" ({vmid})"
            extra = (("cluster", cluster_name),) if cluster_name in clusters else ()
        window = windows.get(key) or windows.get(f"{subject_type}:{key}")
        found, categories = subject_issues(
            target[0],
            target[1],
            label,
            samples,
            now=moment,
            also_affects=extra,
            hourly_bands=hourly_bands,
            expected_window=window,
        )
        if categories:
            judged.add(target)
            observed |= categories
        issues += found
    return issues, observed, judged


async def reconcile_anomaly_findings(
    session: AsyncSession, *, now: datetime | None = None
) -> tuple[CategoryResult, list[CategoryIssue]]:
    when = now or datetime.now(UTC)
    issues, observed, judged = await evaluate(session, now=when)
    result = await reconcile_category_findings(
        session,
        FindingKind.ANOMALY,
        issues,
        observed,
        when=when,
        observed_targets=judged,
    )
    return result, issues


__all__ = [
    "BASELINE_DAYS",
    "CATEGORIES",
    "DEPARTURE_HOURS",
    "FLOORS",
    "HOURLY_MIN_SAMPLES",
    "METRICS",
    "Band",
    "band_for",
    "evaluate",
    "expected_windows_from_env",
    "hourly_band_for",
    "reconcile_anomaly_findings",
    "subject_issues",
]
