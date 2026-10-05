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

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.engine.category_findings import (
    CategoryIssue,
    CategoryResult,
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
CAPACITY_MEDIUM = 0.80
CAPACITY_HIGH = 0.90
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_GIB = 1024**3


def _issue(**kw: Any) -> CategoryIssue:
    return CategoryIssue(kind=FindingKind.BACKUP_GAP, **kw)


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
) -> list[CategoryIssue]:
    """``guests``: cluster-resources VM rows. ``backups``: backup content rows from every backup storage."""
    newest: dict[int, dict[str, Any]] = {}
    for b in backups:
        vmid = int(b.get("vmid") or 0)
        if vmid and (vmid not in newest or b.get("ctime", 0) > newest[vmid].get("ctime", 0)):
            newest[vmid] = b
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
        age = now - datetime.fromtimestamp(int(latest.get("ctime", 0)), tz=UTC)
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
        if state == "failed":
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
    "backup_issues",
    "capacity_issues",
    "orphan_issues",
    "reconcile_backup_findings",
    "schedule_interval",
]
