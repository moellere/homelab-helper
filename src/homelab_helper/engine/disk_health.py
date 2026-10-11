"""Disk health (Phase 10.1) — ``host.smart`` drives → ``storage-health`` findings.

The ``host.smart`` probe has recorded per-drive status, power-on hours,
temperature, reallocated and pending sectors, CRC errors and NVMe wear since
Phase 1, and nothing read it: a failing disk was invisible. This module
judges the newest observation against thresholds and against the drive's own
recent history (the earlier ``host.smart.devices`` observations for the same
host), so a counter that *moves* outranks a counter that merely *exists*.
Thresholds are module constants, deliberately conservative; a count that has
not changed inside the window is history, not news.

One finding per drive and condition, keyed
``(storage-health, disk, <host_id>/<serial-or-name>, <category>)``:

- ``disk-failing`` — SMART overall status FAILED. HIGH: back it up now.
- ``disk-reallocating`` — reallocated or pending sectors are non-zero. MEDIUM;
  HIGH when the count grew inside the window or passed ``REALLOC_HIGH``.
- ``disk-link-errors`` — CRC errors grew inside the window. MEDIUM: usually a
  cable or port, not the drive; a static count from years ago raises nothing.
- ``disk-hot`` — above the temperature a drive of that kind tolerates, judged
  on the window's peak so one warm reading does not flap. MEDIUM, HIGH above
  the upper line. Spinning drives have lower lines than flash.
- ``disk-worn`` — NVMe/SSD percentage used past ``WEAR_WARN``, or a wear rate
  over the window that reaches 100% within ``WEAR_HORIZON``. MEDIUM, HIGH past
  ``WEAR_HIGH``.
- ``disk-aged`` — a spinning drive past ``AGED_HOURS`` of power-on time. LOW:
  plan the replacement; nothing is wrong yet.

Every finding names its host, so resolution is scoped per host (invariant 1):
a host whose smart probe never reported resolves nothing, and a drive that
leaves a host which *did* report is resolved — a replaced drive is a new
identity with a clean slate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.engine.category_findings import (
    CategoryIssue,
    CategoryResult,
    reconcile_category_findings,
)
from homelab_helper.engine.trust import as_utc

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

CATEGORIES = (
    "disk-failing",
    "disk-reallocating",
    "disk-link-errors",
    "disk-hot",
    "disk-worn",
    "disk-aged",
)
HISTORY_WINDOW = timedelta(days=90)
REALLOC_HIGH = 50
HOT_HDD = (50, 55)
"""(warn, high) °C for spinning drives."""
HOT_SSD = (65, 70)
"""(warn, high) °C for flash (SATA SSD and NVMe)."""
WEAR_WARN = 80
WEAR_HIGH = 95
WEAR_HORIZON = timedelta(days=180)
WEAR_MIN_SPAN = timedelta(days=14)
"""A wear rate needs at least this much history before it projects anything."""
AGED_HOURS = 50_000

History = list[tuple[datetime, list[dict[str, Any]]]]
"""Earlier ``host.smart.devices`` observations for the host, oldest first."""


def drive_identity(dev: dict[str, Any]) -> str:
    serial = dev.get("serial")
    return str(serial) if serial else str(dev.get("name") or "?")


def _label(hostname: str, dev: dict[str, Any]) -> str:
    model = dev.get("model") or "unknown model"
    return f"{hostname}:{dev.get('name') or '?'} ({model})"


def _series(history: History, identity: str, field: str) -> list[tuple[datetime, float]]:
    """``(when, value)`` for one drive's numeric field across the history."""
    out: list[tuple[datetime, float]] = []
    for when, devices in history:
        for dev in devices:
            if drive_identity(dev) == identity and isinstance(dev.get(field), int | float):
                out.append((as_utc(when), float(dev[field])))
    return out


def _rotational(dev: dict[str, Any]) -> bool:
    rate = dev.get("rotation_rate")
    return isinstance(rate, int | float) and rate > 0


def _issue(
    host_id: str, identity: str, category: str, severity: FindingSeverity, **kw: Any
) -> CategoryIssue:
    return CategoryIssue(
        kind=FindingKind.STORAGE_HEALTH,
        category=category,
        target_type="disk",
        target_id=f"{host_id}/{identity}",
        severity=severity,
        also_affects=(("host", host_id),),
        **kw,
    )


def _sector_issue(
    host_id: str, label: str, identity: str, dev: dict[str, Any], history: History
) -> CategoryIssue | None:
    realloc = dev.get("reallocated_sectors") or 0
    pending = dev.get("pending_sectors") or 0
    if not (isinstance(realloc, int | float) and isinstance(pending, int | float)):
        return None
    if realloc <= 0 and pending <= 0:
        return None
    earliest = {
        f: s[0][1]
        for f in ("reallocated_sectors", "pending_sectors")
        if (s := _series(history, identity, f))
    }
    grew = any(float(dev.get(f) or 0) > earliest[f] for f in earliest if dev.get(f) is not None)
    high = grew or realloc >= REALLOC_HIGH
    movement = "and the count is growing" if grew else "and the count is steady in the window"
    return _issue(
        host_id,
        identity,
        "disk-reallocating",
        FindingSeverity.HIGH if high else FindingSeverity.MEDIUM,
        title=f"{label}: {int(realloc)} reallocated, {int(pending)} pending sector(s)",
        description=(
            f"{label} reports {int(realloc)} reallocated and {int(pending)} pending sector(s) "
            f"{movement}. "
            + (
                "A growing count is a drive on its way out: back it up and plan the replacement."
                if high
                else "Watch it; a count that starts growing becomes a HIGH finding."
            )
        ),
        evidence={
            "reallocated_sectors": realloc,
            "pending_sectors": pending,
            "window_start": dict(earliest),
            "growing": grew,
        },
    )


def _crc_issue(
    host_id: str, label: str, identity: str, dev: dict[str, Any], history: History
) -> CategoryIssue | None:
    crc = dev.get("crc_errors")
    series = _series(history, identity, "crc_errors")
    if not isinstance(crc, int | float) or not series or crc <= series[0][1]:
        return None
    delta = int(crc - series[0][1])
    return _issue(
        host_id,
        identity,
        "disk-link-errors",
        FindingSeverity.MEDIUM,
        title=f"{label}: {delta} new CRC error(s) in the window",
        description=(
            f"{label}'s UDMA CRC error count rose from {int(series[0][1])} to {int(crc)} "
            f"since {series[0][0].date().isoformat()}. CRC errors are the link, not the "
            "platters: reseat or replace the cable, check the port, then watch whether it stops."
        ),
        evidence={"crc_errors": crc, "window_start": series[0][1], "delta": delta},
    )


def _hot_issue(
    host_id: str, label: str, identity: str, dev: dict[str, Any], history: History
) -> CategoryIssue | None:
    temp = dev.get("temperature_c")
    if not isinstance(temp, int | float):
        return None
    peak = max([temp, *(v for _, v in _series(history, identity, "temperature_c"))])
    warn, high = HOT_HDD if _rotational(dev) else HOT_SSD
    if peak < warn:
        return None
    severity = FindingSeverity.HIGH if peak >= high else FindingSeverity.MEDIUM
    kind = "spinning drive" if _rotational(dev) else "flash drive"
    return _issue(
        host_id,
        identity,
        "disk-hot",
        severity,
        title=f"{label}: {int(peak)} °C peak ({int(temp)} °C now)",
        description=(
            f"{label} peaked at {int(peak)} °C in the window and reads {int(temp)} °C now; "
            f"a {kind} should stay under {warn} °C. Check airflow and the drive's neighbours."
        ),
        evidence={"temperature_c": temp, "peak_c": peak, "warn_c": warn, "high_c": high},
    )


def _wear_issue(
    host_id: str,
    label: str,
    identity: str,
    dev: dict[str, Any],
    history: History,
    now: datetime,
) -> CategoryIssue | None:
    used = dev.get("percentage_used")
    if not isinstance(used, int | float):
        return None
    series = _series(history, identity, "percentage_used")
    projected_days: int | None = None
    if series and now - series[0][0] >= WEAR_MIN_SPAN and used > series[0][1]:
        rate = (used - series[0][1]) / (now - series[0][0]).days  # points per day
        projected_days = int((100 - used) / rate) if rate > 0 else None
    near = projected_days is not None and projected_days <= WEAR_HORIZON.days
    if used < WEAR_WARN and not near:
        return None
    severity = FindingSeverity.HIGH if used >= WEAR_HIGH else FindingSeverity.MEDIUM
    horizon = (
        f" At its rate over the window it reaches 100% in about {projected_days} days."
        if projected_days is not None
        else ""
    )
    return _issue(
        host_id,
        identity,
        "disk-worn",
        severity,
        title=f"{label}: {int(used)}% of rated endurance used",
        description=(
            f"{label} reports {int(used)}% of its rated write endurance used.{horizon} "
            "Plan the replacement before it reads 100%; a worn drive goes read-only or vanishes."
        ),
        evidence={"percentage_used": used, "projected_days_to_100": projected_days},
    )


def _aged_issue(
    host_id: str, label: str, identity: str, dev: dict[str, Any]
) -> CategoryIssue | None:
    hours = dev.get("power_on_hours")
    if not _rotational(dev) or not isinstance(hours, int | float) or hours < AGED_HOURS:
        return None
    years = hours / 8766
    return _issue(
        host_id,
        identity,
        "disk-aged",
        FindingSeverity.LOW,
        title=f"{label}: {int(hours)} power-on hours ({years:.1f} years)",
        description=(
            f"{label} has spun for {int(hours)} hours, about {years:.1f} years. Nothing is wrong "
            "with it yet; drives this old fail without much warning, so have the replacement "
            "and the restore path ready."
        ),
        evidence={"power_on_hours": hours, "years": round(years, 1)},
    )


def disk_issues(
    host_id: str,
    hostname: str,
    devices: list[dict[str, Any]],
    history: History | None = None,
    *,
    now: datetime | None = None,
) -> list[CategoryIssue]:
    """Rules for one host's newest ``host.smart.devices`` against its history."""
    moment = as_utc(now or datetime.now(UTC))
    past = [(as_utc(w), d) for w, d in (history or []) if moment - as_utc(w) <= HISTORY_WINDOW]
    issues: list[CategoryIssue] = []
    for dev in devices:
        identity = drive_identity(dev)
        label = _label(hostname, dev)
        if dev.get("health_passed") is False:
            issues.append(
                _issue(
                    host_id,
                    identity,
                    "disk-failing",
                    FindingSeverity.HIGH,
                    title=f"{label}: SMART overall status FAILED",
                    description=(
                        f"{label} reports its SMART overall health as FAILED — the drive's own "
                        "firmware expects it to fail. Back it up now and replace it."
                    ),
                    evidence={"health_passed": False, "serial": dev.get("serial")},
                )
            )
        for issue in (
            _sector_issue(host_id, label, identity, dev, past),
            _crc_issue(host_id, label, identity, dev, past),
            _hot_issue(host_id, label, identity, dev, past),
            _wear_issue(host_id, label, identity, dev, past, moment),
            _aged_issue(host_id, label, identity, dev),
        ):
            if issue is not None:
                issues.append(issue)
    return issues


async def reconcile_disk_health(
    session: AsyncSession,
    host_id: str,
    hostname: str,
    devices: list[dict[str, Any]],
    history: History | None = None,
    *,
    when: datetime | None = None,
) -> CategoryResult:
    return await reconcile_category_findings(
        session,
        FindingKind.STORAGE_HEALTH,
        disk_issues(host_id, hostname, devices, history, now=when),
        set(CATEGORIES),
        when=when,
        observed_targets={("host", host_id)},
    )


__all__ = ["CATEGORIES", "HISTORY_WINDOW", "disk_issues", "drive_identity", "reconcile_disk_health"]
