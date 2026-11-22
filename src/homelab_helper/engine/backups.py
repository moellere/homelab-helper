"""Backup posture (Phase 8.2) — Proxmox backup jobs and backup storage → ``BACKUP_GAP``.

Checks, each a category resolved only when observed (``engine/category_findings``):

- ``backup-uncovered`` — a guest that no enabled backup job selects. HIGH.
- ``backup-stale`` — a selected guest whose newest backup is older than twice
  its job's interval, or that has no backup at all. HIGH.
- ``backup-verify`` — the newest backup of a guest failed verification. MEDIUM.
- ``backup-orphans`` — backups retained for vmids that are no longer guests:
  space spent on data nothing will restore. LOW, one finding per storage.
- ``backup-capacity`` — a backup storage at 80% (MEDIUM) or 90% (HIGH).

Templates are exempt from coverage and staleness (they do not change between
backups). A job's interval comes from its calendar-event schedule:
weekday-restricted schedules repeat every ``7 / days`` days, ``*/N`` hour steps
every N hours, everything else daily; anything unparseable is treated as daily,
which can only make the staleness check stricter.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from sqlalchemy import select

from homelab_helper.db.enums import (
    Confidence,
    FindingKind,
    FindingSeverity,
    FindingStatus,
    IntentTargetType,
    PrivilegeLevel,
)
from homelab_helper.db.models import DiscoveryRun, Observation, Probe, ReconciliationFinding
from homelab_helper.engine.category_findings import (
    CategoryIssue,
    CategoryResult,
    category_of,
    reconcile_category_findings,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

CATEGORIES = (
    "backup-uncovered",
    "backup-stale",
    "backup-verify",
    "backup-orphans",
    "backup-capacity",
)
EXTERNAL_CATEGORIES = (
    "backup-offsite-stale",
    "backup-target-stale",
    "backup-target-uncovered",
)
CAPACITY_MEDIUM = 0.80
CAPACITY_HIGH = 0.90
DEFAULT_OFFSITE_MAX_AGE = timedelta(hours=48)
RESTORE_DRILL_KEY = "backup.restore_drill"
TARGET_LAST_RUN_KEY = "backup.target.last_run"
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_GIB = 1024**3
_BAD_SENSOR_STATES = frozenset({"failed", "error", "stale", "problem", "unavailable"})
_OFFSITE_KEYWORDS = ("offsite", "off_site", "replication")
_BACKUP_SENSOR_TOKENS = ("status", "last", "age", "ok", "state", "stale", "push", "replication")


def _issue(**kw: Any) -> CategoryIssue:
    return CategoryIssue(kind=FindingKind.BACKUP_GAP, **kw)


def _parse_iso(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def schedule_interval(schedule: str | None) -> timedelta:
    """How often a Proxmox calendar-event schedule fires (see module docstring)."""
    text = (schedule or "").lower()
    days: set[str] = set()
    for token in re.findall(r"[a-z]{3}(?:\.\.[a-z]{3})?", text):
        if ".." in token:
            a, b = token.split("..")
            if a in _DAYS and b in _DAYS:
                i, j = _DAYS.index(a), _DAYS.index(b)
                days |= set(_DAYS[i : j + 1] if i <= j else _DAYS[i:] + _DAYS[: j + 1])
        elif token in _DAYS:
            days.add(token)
    if days:
        return timedelta(days=7 / len(days))
    step = re.search(r"\*/(\d+)\s*:", text) or re.search(r"^\s*0?/(\d+)\s*:", text)
    if step and int(step.group(1)) > 0:
        return timedelta(hours=int(step.group(1)))
    return timedelta(days=1)


def _selected(job: dict[str, Any], guest: dict[str, Any]) -> bool:
    if not int(job.get("enabled", 1) or 0):
        return False
    if job.get("node") and job["node"] != guest.get("node"):
        return False
    vmid = str(guest["vmid"])
    if job.get("all"):
        return vmid not in {
            x.strip() for x in str(job.get("exclude") or "").split(",") if x.strip()
        }
    if job.get("pool"):
        return bool(guest.get("pool")) and guest.get("pool") == job["pool"]
    return vmid in {x.strip() for x in str(job.get("vmid") or "").split(",") if x.strip()}


def _label(guest: dict[str, Any]) -> str:
    return f"{guest.get('name') or 'guest'} ({guest['vmid']})"


def backup_issues(
    guests: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    backups: list[dict[str, Any]],
    *,
    now: datetime,
    verified_drills: dict[str, datetime] | None = None,
) -> list[CategoryIssue]:
    """``guests``: cluster-resources VM rows. ``backups``: backup content rows from every backup storage."""
    newest: dict[int, dict[str, Any]] = {}
    for b in backups:
        vmid = int(b.get("vmid") or 0)
        if vmid and (vmid not in newest or b.get("ctime", 0) > newest[vmid].get("ctime", 0)):
            newest[vmid] = b
    drills = verified_drills or {}
    issues: list[CategoryIssue] = []
    for g in guests:
        if g.get("template"):
            continue
        vmid = int(g["vmid"])
        selecting = [j for j in jobs if _selected(j, g)]
        if not selecting:
            issues.append(
                _issue(
                    category="backup-uncovered",
                    target_type="guest",
                    target_id=str(vmid),
                    severity=FindingSeverity.HIGH,
                    title=f"{_label(g)} is not in any enabled backup job",
                    description=f"No enabled backup job selects {_label(g)} on {g.get('node')}.",
                    evidence={"vmid": vmid, "node": g.get("node")},
                )
            )
            continue
        interval = min(schedule_interval(j.get("schedule")) for j in selecting)
        latest = newest.get(vmid)
        if latest is None:
            issues.append(
                _issue(
                    category="backup-stale",
                    target_type="guest",
                    target_id=str(vmid),
                    severity=FindingSeverity.HIGH,
                    title=f"{_label(g)} has no backup",
                    description=f"{_label(g)} is selected by a backup job but no backup of it exists.",
                    evidence={"vmid": vmid},
                )
            )
            continue
        backup_dt = datetime.fromtimestamp(int(latest.get("ctime", 0)), tz=UTC)
        age = now - backup_dt
        if age > 2 * interval:
            issues.append(
                _issue(
                    category="backup-stale",
                    target_type="guest",
                    target_id=str(vmid),
                    severity=FindingSeverity.HIGH,
                    title=f"{_label(g)}: last backup {age.days}d {age.seconds // 3600}h ago",
                    description=(
                        f"Newest backup of {_label(g)} is {latest.get('volid')}, "
                        f"older than twice its job interval ({interval})."
                    ),
                    evidence={"vmid": vmid, "age_hours": int(age.total_seconds() // 3600)},
                )
            )
        state = (latest.get("verification") or {}).get("state")
        drill_dt = drills.get(str(vmid))
        drill_clears = drill_dt is not None and drill_dt >= backup_dt
        if state == "failed" and not drill_clears:
            issues.append(
                _issue(
                    category="backup-verify",
                    target_type="guest",
                    target_id=str(vmid),
                    severity=FindingSeverity.MEDIUM,
                    title=f"{_label(g)}: newest backup failed verification",
                    description=f"{latest.get('volid')} failed its verify job; the previous backup is the newest known-good copy.",
                    evidence={"vmid": vmid, "volid": latest.get("volid")},
                )
            )
    return issues


def _is_offsite_backup_entity(entity_id: str, domain: str | None) -> bool:
    if domain not in ("sensor", "binary_sensor"):
        return False
    low = entity_id.lower()
    if any(k in low for k in _OFFSITE_KEYWORDS):
        return True
    return "backup" in low and any(tok in low for tok in _BACKUP_SENSOR_TOKENS)


def hass_backup_issues(
    service_name: str,
    states: list[dict[str, Any]],
    *,
    now: datetime,
    max_age: timedelta = DEFAULT_OFFSITE_MAX_AGE,
) -> tuple[list[CategoryIssue], set[str]]:
    """Evaluate Home Assistant off-site replication and backup sensors → ``(issues, observed)``."""
    issues: list[CategoryIssue] = []
    observed: set[str] = set()
    for s in states:
        entity_id = str(s.get("entity_id") or "")
        domain = s.get("domain") or (entity_id.split(".", 1)[0] if "." in entity_id else None)
        if not _is_offsite_backup_entity(entity_id, domain):
            continue
        observed.add("backup-offsite-stale")
        state = str(s.get("state") or "").strip()
        state_low = state.lower()
        name = str(s.get("name") or entity_id)
        target_id = f"{service_name}/{entity_id}"
        binary_bad = domain == "binary_sensor" and "ok" in entity_id.lower() and state_low == "off"
        if state_low in _BAD_SENSOR_STATES or binary_bad:
            issues.append(
                _issue(
                    category="backup-offsite-stale",
                    target_type="service",
                    target_id=target_id,
                    severity=FindingSeverity.HIGH,
                    title=f"{name} ({service_name}): off-site backup state is {state!r}",
                    description=(
                        f"Home Assistant entity {entity_id} on {service_name} reports {state!r}. "
                        "Verify the off-site replication job and storage target."
                    ),
                    evidence={"service": service_name, "entity_id": entity_id, "state": state},
                )
            )
            continue
        ts = _parse_iso(state)
        if ts is not None:
            age = now - ts
            if age > max_age:
                hours = int(age.total_seconds() // 3600)
                issues.append(
                    _issue(
                        category="backup-offsite-stale",
                        target_type="service",
                        target_id=target_id,
                        severity=FindingSeverity.HIGH,
                        title=f"{name} ({service_name}): last off-site push {age.days}d {age.seconds // 3600}h ago",
                        description=(
                            f"Home Assistant entity {entity_id} on {service_name} last ran at "
                            f"{ts.isoformat()} ({hours}h ago, threshold {int(max_age.total_seconds() // 3600)}h)."
                        ),
                        evidence={
                            "service": service_name,
                            "entity_id": entity_id,
                            "last_run": ts.isoformat(),
                            "age_hours": hours,
                        },
                    )
                )
    return issues, observed


def load_backup_targets(raw: str | None = None) -> list[dict[str, Any]]:
    """Load non-Proxmox backup targets from ``HOMELAB_HELPER_BACKUP_TARGETS`` (YAML path or ``id=hours,...``)."""
    spec = raw if raw is not None else (os.environ.get("HOMELAB_HELPER_BACKUP_TARGETS") or "")
    spec = spec.strip()
    if not spec:
        return []
    path = Path(spec)
    if "=" not in spec and path.is_file():
        loaded = yaml.safe_load(path.read_text()) or {}
        rows = loaded.get("targets", loaded) if isinstance(loaded, dict) else loaded
        if isinstance(rows, list):
            return [dict(r) for r in rows if isinstance(r, dict) and r.get("id")]
        return []
    targets: list[dict[str, Any]] = []
    for item in spec.split(","):
        part = item.strip()
        if not part:
            continue
        lhs, sep, rhs = part.partition("=")
        tid, _, label = lhs.strip().partition(":")
        if not tid:
            continue
        try:
            hours = float(rhs.strip()) if sep and rhs.strip() else 48.0
        except ValueError:
            hours = 48.0
        targets.append({"id": tid.strip(), "label": (label or tid).strip(), "max_age_hours": hours})
    return targets


def declared_target_issues(
    targets: list[dict[str, Any]],
    *,
    now: datetime,
    observed_runs: dict[str, datetime] | None = None,
    verified_drills: dict[str, datetime] | None = None,
) -> tuple[list[CategoryIssue], set[str]]:
    """Evaluate operator-declared non-Proxmox backup targets → ``(issues, observed)``."""
    if not targets:
        return [], set()
    runs = observed_runs or {}
    drills = verified_drills or {}
    observed: set[str] = {"backup-target-uncovered", "backup-target-stale"}
    issues: list[CategoryIssue] = []
    for t in targets:
        tid = str(t.get("id") or "").strip()
        if not tid:
            continue
        label = str(t.get("label") or tid)
        max_hours = float(t.get("max_age_hours") or 48.0)
        max_age = timedelta(hours=max_hours)
        last_run = runs.get(tid) or _parse_iso(t.get("last_backup"))
        if last_run is None:
            issues.append(
                _issue(
                    category="backup-target-uncovered",
                    target_type="backup-target",
                    target_id=tid,
                    severity=FindingSeverity.HIGH,
                    title=f"Backup target {label} has no recorded backup",
                    description=(
                        f"Declared backup target {label!r} ({tid}) has no recorded backup run."
                    ),
                    evidence={"target_id": tid, "max_age_hours": max_hours},
                )
            )
            continue
        age = now - last_run
        if age > max_age:
            age_hours = int(age.total_seconds() // 3600)
            issues.append(
                _issue(
                    category="backup-target-stale",
                    target_type="backup-target",
                    target_id=tid,
                    severity=FindingSeverity.HIGH,
                    title=f"Backup target {label}: last backup {age.days}d {age.seconds // 3600}h ago",
                    description=(
                        f"Declared backup target {label!r} ({tid}) last backed up at "
                        f"{last_run.isoformat()} ({age_hours}h ago, threshold {int(max_hours)}h)."
                    ),
                    evidence={
                        "target_id": tid,
                        "last_backup": last_run.isoformat(),
                        "age_hours": age_hours,
                        "max_age_hours": max_hours,
                    },
                )
            )
        if t.get("require_verify"):
            observed.add("backup-verify")
            verified_at = drills.get(tid) or _parse_iso(t.get("verified_at"))
            if verified_at is None or verified_at < last_run:
                issues.append(
                    _issue(
                        category="backup-verify",
                        target_type="backup-target",
                        target_id=tid,
                        severity=FindingSeverity.MEDIUM,
                        title=f"Backup target {label} has not been verified by a restore drill",
                        description=(
                            f"Declared backup target {label!r} ({tid}) requires verification, "
                            "and no restore drill has been recorded since its latest backup."
                        ),
                        evidence={"target_id": tid, "last_backup": last_run.isoformat()},
                    )
                )
    return issues, observed


async def restore_drill_timestamps(session: AsyncSession) -> dict[str, datetime]:
    """Latest recorded restore-drill verification timestamp per target id."""
    rows = (
        await session.execute(
            select(Observation)
            .where(Observation.key == RESTORE_DRILL_KEY)
            .order_by(Observation.recorded_at.asc())
        )
    ).scalars()
    out: dict[str, datetime] = {}
    for row in rows:
        val = row.value if isinstance(row.value, dict) else {}
        ts = _parse_iso(val.get("verified_at")) or (
            row.recorded_at
            if row.recorded_at.tzinfo is not None
            else row.recorded_at.replace(tzinfo=UTC)
        )
        out[str(row.target_id)] = ts
    return out


async def backup_target_timestamps(session: AsyncSession) -> dict[str, datetime]:
    """Latest recorded backup run timestamp per non-Proxmox backup target id."""
    rows = (
        await session.execute(
            select(Observation)
            .where(Observation.key == TARGET_LAST_RUN_KEY)
            .order_by(Observation.recorded_at.asc())
        )
    ).scalars()
    out: dict[str, datetime] = {}
    for row in rows:
        val = row.value if isinstance(row.value, dict) else {}
        ts = _parse_iso(val.get("last_backup")) or (
            row.recorded_at
            if row.recorded_at.tzinfo is not None
            else row.recorded_at.replace(tzinfo=UTC)
        )
        out[str(row.target_id)] = ts
    return out


async def _ensure_restore_probe(session: AsyncSession) -> Probe:
    probe = (
        await session.execute(select(Probe).where(Probe.name == RESTORE_DRILL_KEY))
    ).scalar_one_or_none()
    if probe is not None:
        return probe
    probe = Probe(
        name=RESTORE_DRILL_KEY,
        version="0.1.0",
        module_path="homelab_helper.engine.backups",
        required_privilege=PrivilegeLevel.NONE,
        produces_keys=[RESTORE_DRILL_KEY],
        description="Auditable backup restore-drill verification records.",
    )
    session.add(probe)
    await session.flush()
    return probe


async def record_restore_drill(
    session: AsyncSession,
    target_id: str,
    *,
    verified_at: datetime | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record an auditable restore-drill verification and resolve any open ``backup-verify`` finding for ``target_id``."""
    moment = verified_at or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    tid = str(target_id).strip()
    probe = await _ensure_restore_probe(session)
    run = DiscoveryRun(
        probe_id=probe.id,
        probe_name=RESTORE_DRILL_KEY,
        probe_version="0.1.0",
        privilege_level=PrivilegeLevel.NONE,
        started_at=moment,
        ended_at=moment,
        success=True,
        observation_count=1,
        triggered_by="manual",
    )
    session.add(run)
    await session.flush()
    obs = Observation(
        run_id=run.id,
        target_type=IntentTargetType.SERVICE,
        target_id=tid,
        key=RESTORE_DRILL_KEY,
        value={
            "target_id": tid,
            "verified_at": moment.isoformat(),
            "notes": notes,
        },
        confidence=Confidence.VERIFIED,
        recorded_at=moment,
    )
    session.add(obs)
    resolved: list[str] = []
    open_findings = (
        await session.execute(
            select(ReconciliationFinding).where(
                ReconciliationFinding.kind == FindingKind.BACKUP_GAP,
                ReconciliationFinding.status.in_([FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED]),
            )
        )
    ).scalars()
    for f in open_findings:
        if category_of(f) != "backup-verify":
            continue
        if any(a.get("target_id") == tid for a in (f.affected or [])):
            f.status = FindingStatus.RESOLVED
            f.resolved_at = moment
            resolved.append(f.fingerprint)
    await session.flush()
    return {
        "target_id": tid,
        "verified_at": moment.isoformat(),
        "notes": notes,
        "resolved_findings": resolved,
    }


def orphan_issues(
    storage: str, guests: list[dict[str, Any]], backups: list[dict[str, Any]]
) -> list[CategoryIssue]:
    live = {int(g["vmid"]) for g in guests}
    by_vmid: dict[int, list[dict[str, Any]]] = {}
    for b in backups:
        vmid = int(b.get("vmid") or 0)
        if vmid and vmid not in live:
            by_vmid.setdefault(vmid, []).append(b)
    if not by_vmid:
        return []
    size = sum(int(b.get("size") or 0) for rows in by_vmid.values() for b in rows)
    listing = ", ".join(
        f"{vmid} ({rows[0].get('notes') or '?'}, {len(rows)} backup(s))"
        for vmid, rows in sorted(by_vmid.items())
    )
    return [
        _issue(
            category="backup-orphans",
            target_type="storage",
            target_id=storage,
            severity=FindingSeverity.LOW,
            title=f"{storage}: backups kept for {len(by_vmid)} guest(s) that no longer exist",
            description=(
                f"{listing}; {size / _GIB:.1f} GiB logical (deduplicated on disk). Prune "
                "retention keeps a group's newest backups no matter how old, so these never "
                "age out — remove the groups if the guests are gone for good."
            ),
            evidence={"vmids": sorted(by_vmid), "bytes": size},
        )
    ]


def capacity_issues(storages: list[dict[str, Any]]) -> list[CategoryIssue]:
    issues: list[CategoryIssue] = []
    for s in storages:
        total, used = int(s.get("maxdisk") or 0), int(s.get("disk") or 0)
        if not total:
            continue
        ratio = used / total
        if ratio < CAPACITY_MEDIUM:
            continue
        issues.append(
            _issue(
                category="backup-capacity",
                target_type="storage",
                target_id=str(s["storage"]),
                severity=FindingSeverity.HIGH if ratio >= CAPACITY_HIGH else FindingSeverity.MEDIUM,
                title=f"Backup storage {s['storage']} is {ratio:.0%} full",
                description=f"{used / _GIB:.0f} of {total / _GIB:.0f} GiB used.",
                evidence={"used": used, "total": total},
            )
        )
    return issues


async def reconcile_backup_findings(
    session: AsyncSession,
    issues: list[CategoryIssue],
    observed: set[str],
    *,
    when: datetime | None = None,
) -> CategoryResult:
    return await reconcile_category_findings(
        session, FindingKind.BACKUP_GAP, issues, observed, when=when
    )


__all__ = [
    "CATEGORIES",
    "DEFAULT_OFFSITE_MAX_AGE",
    "EXTERNAL_CATEGORIES",
    "RESTORE_DRILL_KEY",
    "TARGET_LAST_RUN_KEY",
    "backup_issues",
    "backup_target_timestamps",
    "capacity_issues",
    "declared_target_issues",
    "hass_backup_issues",
    "load_backup_targets",
    "orphan_issues",
    "reconcile_backup_findings",
    "record_restore_drill",
    "restore_drill_timestamps",
    "schedule_interval",
]
