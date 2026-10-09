"""RAID health (Phase 9.5) — ``host.raid`` arrays → ``storage-health`` findings.

One finding per array and condition, keyed ``(storage-health, raid-array,
<host_id>/<mdN>, <category>)``:

- ``raid-inactive`` — the array is assembled but not running. HIGH: nothing on
  it is reachable.
- ``raid-rebuilding`` — a recovery, resync or reshape is in flight (or queued).
  MEDIUM: redundancy is reduced until it finishes; nothing to do but let it.
- ``raid-degraded`` — slots are missing or a member is faulty and *no* rebuild
  is running. HIGH: one more failure can lose data; replace the member.

A scrub (``check`` / ``repair``) is routine and raises nothing. Every finding
also names its host, so resolution is scoped per host (invariant 1): a host
whose raid probe has never reported cannot resolve another host's findings,
and an array that disappears from a host that *did* report is resolved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.engine.category_findings import (
    CategoryIssue,
    CategoryResult,
    reconcile_category_findings,
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

CATEGORIES = ("raid-inactive", "raid-rebuilding", "raid-degraded")
_REBUILDS = {"recovery", "resync", "reshape"}


def raid_issues(host_id: str, hostname: str, arrays: list[dict[str, Any]]) -> list[CategoryIssue]:
    issues: list[CategoryIssue] = []
    for a in arrays:
        name = str(a.get("name") or "md?")
        label = f"{hostname}:{name}"
        disks = (
            f"{a.get('active_disks')}/{a.get('raid_disks')}"
            if a.get("raid_disks") is not None
            else "?"
        )
        faulty = [m["device"] for m in a.get("members") or [] if m.get("state") == "faulty"]
        evidence = {
            "array": name,
            "level": a.get("level"),
            "state": a.get("state"),
            "disks": disks,
            "sync_action": a.get("sync_action"),
            "sync_progress": a.get("sync_progress"),
            "faulty": faulty,
            "uuid": a.get("uuid"),
        }
        target_id = f"{host_id}/{name}"

        def _issue(
            category: str,
            severity: FindingSeverity,
            title: str,
            description: str,
            *,
            _target: str = target_id,
            _evidence: dict[str, Any] = evidence,
        ) -> CategoryIssue:
            return CategoryIssue(
                kind=FindingKind.STORAGE_HEALTH,
                category=category,
                target_type="raid-array",
                target_id=_target,
                severity=severity,
                title=title,
                description=description,
                evidence=_evidence,
                also_affects=(("host", host_id),),
            )

        level = a.get("level") or "md"
        if str(a.get("state") or "").startswith("inactive"):
            issues.append(
                _issue(
                    "raid-inactive",
                    FindingSeverity.HIGH,
                    f"{label} ({level}) is inactive",
                    (
                        f"{label} is assembled but not running; nothing on it is reachable. "
                        "Check `mdadm --detail` and the member devices."
                    ),
                )
            )
            continue
        action = a.get("sync_action")
        if action in _REBUILDS:
            progress = a.get("sync_progress")
            done = f" — {progress:.1f}% done" if isinstance(progress, int | float) else ""
            issues.append(
                _issue(
                    "raid-rebuilding",
                    FindingSeverity.MEDIUM,
                    f"{label} ({level}) is rebuilding ({action}{done})",
                    (
                        f"{label} is running a {action} with {disks} disks active{done}. "
                        "Redundancy is reduced until it completes; avoid touching its members."
                    ),
                )
            )
        elif a.get("degraded") or faulty:
            missing = f"; faulty: {', '.join(faulty)}" if faulty else ""
            issues.append(
                _issue(
                    "raid-degraded",
                    FindingSeverity.HIGH,
                    f"{label} ({level}) is degraded — {disks} disks{missing}",
                    (
                        f"{label} runs with {disks} disks active and no rebuild in progress{missing}. "
                        "One more failure can lose data: replace or re-add the missing member."
                    ),
                )
            )
    return issues


async def reconcile_raid_health(
    session: AsyncSession,
    host_id: str,
    hostname: str,
    arrays: list[dict[str, Any]],
    *,
    when: datetime | None = None,
) -> CategoryResult:
    return await reconcile_category_findings(
        session,
        FindingKind.STORAGE_HEALTH,
        raid_issues(host_id, hostname, arrays),
        set(CATEGORIES),
        when=when,
        observed_targets={("host", host_id)},
    )


__all__ = ["CATEGORIES", "raid_issues", "reconcile_raid_health"]
