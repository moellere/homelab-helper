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

import re
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.db.models import PhysicalPart, Placement
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
_P_PART_RE = re.compile(r"^((?:nvme\d+n\d+|mmcblk\d+|loop\d+))p\d+$")
_DISK_PART_RE = re.compile(r"^((?:[svh]d[a-z]+|xvd[a-z]+))\d+$")


def parent_disk_name(device: str) -> str:
    """Map a partition or block device name (``sda1``, ``/dev/nvme0n1p1``) to its parent disk slot."""
    name = device.strip().removeprefix("/dev/")
    if m := _P_PART_RE.match(name):
        return m.group(1)
    if m := _DISK_PART_RE.match(name):
        return m.group(1)
    return name


def link_raid_members(
    arrays: list[dict[str, Any]],
    parts_by_slot: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Link each mdraid array's members to their backing ``PhysicalPart`` metadata by disk slot."""
    volumes: list[dict[str, Any]] = []
    for a in arrays:
        disks = (
            f"{a.get('active_disks')}/{a.get('raid_disks')}"
            if a.get("raid_disks") is not None
            else None
        )
        linked_members: list[dict[str, Any]] = []
        for m in a.get("members") or []:
            dev = str(m.get("device") or "")
            disk = parent_disk_name(dev)
            part = parts_by_slot.get(dev) or parts_by_slot.get(disk) or {}
            linked_members.append(
                {
                    "device": dev,
                    "disk": disk,
                    "slot": m.get("slot"),
                    "state": m.get("state", "active"),
                    "part_id": part.get("part_id"),
                    "serial": part.get("serial"),
                    "wwid": part.get("wwid"),
                    "model": part.get("model"),
                }
            )
        volumes.append(
            {
                "name": str(a.get("name") or "md?"),
                "level": a.get("level"),
                "state": a.get("state"),
                "disks": disks,
                "degraded": bool(a.get("degraded")),
                "sync_action": a.get("sync_action"),
                "uuid": a.get("uuid"),
                "members": linked_members,
            }
        )
    return volumes


async def resolve_host_raid_volumes(
    session: AsyncSession,
    host_id: uuid.UUID | str,
    arrays: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Link ``arrays`` to the host's open storage ``Placement`` / ``PhysicalPart`` rows.

    Also records ``attributes["raid_array"]`` on each matched ``PhysicalPart`` so
    part queries and ``helper host show`` can trace a drive to its array (#77).
    """
    hid = uuid.UUID(str(host_id)) if not isinstance(host_id, uuid.UUID) else host_id
    rows = (
        await session.execute(
            select(Placement, PhysicalPart)
            .join(PhysicalPart, PhysicalPart.id == Placement.part_id)
            .where(Placement.host_id == hid, Placement.to_date.is_(None))
        )
    ).all()
    parts_by_slot: dict[str, dict[str, Any]] = {}
    part_rows: dict[str, PhysicalPart] = {}
    for placement, part in rows:
        slot = (placement.slot or "").removeprefix("/dev/")
        if not slot:
            continue
        parts_by_slot[slot] = {
            "part_id": str(part.id),
            "serial": part.serial,
            "wwid": part.wwid,
            "model": part.model,
            "kind": part.kind.value,
        }
        part_rows[str(part.id)] = part

    volumes = link_raid_members(arrays, parts_by_slot)
    seen_part_ids: set[str] = set()
    for vol in volumes:
        for m in vol["members"]:
            pid = m.get("part_id")
            if pid and pid in part_rows:
                seen_part_ids.add(pid)
                part_obj = part_rows[pid]
                attrs = dict(part_obj.attributes or {})
                attrs["raid_array"] = {
                    "array": vol["name"],
                    "level": vol["level"],
                    "device": m["device"],
                    "slot": m["slot"],
                    "uuid": vol["uuid"],
                }
                part_obj.attributes = attrs
    for pid, part_obj in part_rows.items():
        if pid not in seen_part_ids and "raid_array" in (part_obj.attributes or {}):
            attrs = dict(part_obj.attributes or {})
            attrs.pop("raid_array", None)
            part_obj.attributes = attrs
    return volumes


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


__all__ = [
    "CATEGORIES",
    "link_raid_members",
    "parent_disk_name",
    "raid_issues",
    "reconcile_raid_health",
    "resolve_host_raid_volumes",
]
