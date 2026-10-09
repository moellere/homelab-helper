"""Storage efficiency (Phase 8.5) — space that is committed but not earning.

Five deterministic checks, each a *category* the pass either observed or did
not (``engine/category_findings.py``, invariant 1):

- ``storage-headroom`` — a pool whose used bytes are trending up, with a
  projected date it runs out. Needs 8.3 history: a single reading cannot have
  a slope. 8.2's ``backup-capacity`` owns the point-in-time ratio on backup
  storages; this answers the different question of *when*, for every pool, so
  a pool can legitimately raise both.
- ``storage-snapshot-stale`` — guest snapshots older than
  :data:`STALE_SNAPSHOT_DAYS`. A snapshot pins the blocks its parent has since
  overwritten, so an old one costs space that looks like the guest's. The
  harness's own ``helper-`` snapshots are called out separately: those are its
  litter, left behind by a rollback capture nobody undid.
- ``storage-detached-disk`` — ``unusedN`` entries in a guest config: a disk
  still on the pool, attached to nothing, invisible in the guest's own view.
- ``storage-template-clutter`` — ISO images and container templates no guest
  config references.
- ``storage-released-pv`` — Kubernetes PersistentVolumes in ``Released``: the
  claim is gone, the volume is retained, and nothing can bind it again.

Every check is a pure function over already-fetched source data, so the pass
is testable without a hypervisor and the projections are reproducible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.engine.category_findings import CategoryIssue

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_GIB = 1024**3
_TIB = 1024**4

STALE_SNAPSHOT_DAYS = 30
HEADROOM_HIGH_DAYS = 30
HEADROOM_MEDIUM_DAYS = 90
MIN_HEADROOM_SAMPLES = 7
"""Fewer points than this is not a trend, and no projection is made."""
MIN_GROWTH_BYTES_PER_DAY = 1 * _GIB // 100
"""Noise floor: ~10 MiB/day of churn is not a pool filling up."""

HARNESS_SNAPSHOT_PREFIX = "helper-"
_NAMES_SHOWN = 6
"""A finding names this many volumes and counts the rest."""
_DISK_KEY = re.compile(r"^(scsi|virtio|sata|ide|mp|rootfs)\d*$")
_UNUSED_KEY = re.compile(r"^unused\d+$")
_VOLUME_ID = re.compile(r"^(?P<storage>[^:]+):(?P<rest>.+)$")


def _issue(**kw: Any) -> CategoryIssue:
    return CategoryIssue(kind=FindingKind.STORAGE_EFFICIENCY, **kw)


def _human(nbytes: float) -> str:
    return f"{nbytes / _TIB:.1f} TiB" if nbytes >= _TIB else f"{nbytes / _GIB:.0f} GiB"


# --------------------------------------------------------------- headroom


@dataclass(frozen=True)
class Projection:
    """Where a pool is heading, from a least-squares fit on its history."""

    bytes_per_day: float
    days_to_full: float | None
    used: int
    total: int
    samples: int

    @property
    def ratio(self) -> float:
        return self.used / self.total if self.total else 0.0


def project_headroom(points: Sequence[tuple[datetime, int]], total: int) -> Projection | None:
    """Least-squares slope over ``(timestamp, used)``; ``None`` without a trend.

    Deliberately linear: a pool filling from writes grows about linearly, and
    a straight line is explainable in a finding ("68 GiB/day"). Anything
    cleverer would be harder to justify to the operator reading it.
    """
    if len(points) < MIN_HEADROOM_SAMPLES or not total:
        return None
    base = points[0][0]
    xs = [(ts - base).total_seconds() / 86400 for ts, _ in points]
    ys = [float(used) for _, used in points]
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denominator
    used = int(ys[-1])
    if slope < MIN_GROWTH_BYTES_PER_DAY:
        return Projection(slope, None, used, total, n)
    remaining = total - used
    return Projection(slope, max(0.0, remaining / slope), used, total, n)


def headroom_issues(
    pools: Iterable[tuple[str, Projection | None]],
) -> list[CategoryIssue]:
    """One issue per pool projected to fill inside the medium horizon."""
    issues: list[CategoryIssue] = []
    for name, projection in pools:
        if projection is None or projection.days_to_full is None:
            continue
        days = projection.days_to_full
        if days > HEADROOM_MEDIUM_DAYS:
            continue
        severity = FindingSeverity.HIGH if days <= HEADROOM_HIGH_DAYS else FindingSeverity.MEDIUM
        full_on = datetime.now(UTC) + timedelta(days=days)
        issues.append(
            _issue(
                category="storage-headroom",
                target_type="storage",
                target_id=name,
                severity=severity,
                title=f"{name} fills in about {days:.0f} day(s)",
                description=(
                    f"{_human(projection.used)} of {_human(projection.total)} used "
                    f"({projection.ratio:.0%}), growing {_human(projection.bytes_per_day)}/day "
                    f"over {projection.samples} sample(s) — full around "
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


# --------------------------------------------------------------- snapshots


def snapshot_issues(
    guests: Iterable[dict[str, Any]], *, now: datetime | None = None
) -> list[CategoryIssue]:
    """Guests carrying snapshots older than :data:`STALE_SNAPSHOT_DAYS`.

    ``guests`` entries are ``{vmid, name, node, kind, snapshots: [...]}`` as
    the Proxmox snapshot listing returns them (``current`` is the live state,
    not a snapshot, and is skipped).
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=STALE_SNAPSHOT_DAYS)
    issues: list[CategoryIssue] = []
    for guest in guests:
        stale: list[tuple[str, datetime]] = []
        for snap in guest.get("snapshots") or []:
            name = str(snap.get("name") or "")
            if name == "current" or not snap.get("snaptime"):
                continue
            taken = datetime.fromtimestamp(int(snap["snaptime"]), tz=UTC)
            if taken < cutoff:
                stale.append((name, taken))
        if not stale:
            continue
        stale.sort(key=lambda pair: pair[1])
        oldest_name, oldest_at = stale[0]
        ours = [name for name, _ in stale if name.startswith(HARNESS_SNAPSHOT_PREFIX)]
        age = (moment - oldest_at).days
        label = guest.get("name") or guest["vmid"]
        detail = (
            f"{len(stale)} snapshot(s) older than {STALE_SNAPSHOT_DAYS} days; oldest "
            f"{oldest_name} from {oldest_at:%Y-%m-%d} ({age} days). A snapshot pins the "
            "blocks the guest has since overwritten, so it costs space the guest's own "
            "usage does not show."
        )
        if ours:
            detail += (
                f" {len(ours)} of these are the harness's own rollback captures "
                f"({', '.join(sorted(ours)[:3])}) — left behind by an execution nobody undid, "
                "and safe to delete once the action is known good."
            )
        issues.append(
            _issue(
                category="storage-snapshot-stale",
                target_type="guest",
                target_id=f"{guest['node']}/{guest['vmid']}",
                severity=FindingSeverity.MEDIUM if ours else FindingSeverity.LOW,
                title=f"{label} has {len(stale)} stale snapshot(s)",
                description=detail,
                evidence={
                    "snapshots": [name for name, _ in stale],
                    "harness_snapshots": sorted(ours),
                    "oldest_days": age,
                },
            )
        )
    return issues


# ----------------------------------------------------------- detached disks


def detached_disk_issues(guests: Iterable[dict[str, Any]]) -> list[CategoryIssue]:
    """``unusedN`` entries: a disk on the pool, attached to nothing."""
    issues: list[CategoryIssue] = []
    for guest in guests:
        config = guest.get("config") or {}
        unused = {k: str(v) for k, v in config.items() if _UNUSED_KEY.match(k)}
        if not unused:
            continue
        label = guest.get("name") or guest["vmid"]
        issues.append(
            _issue(
                category="storage-detached-disk",
                target_type="guest",
                target_id=f"{guest['node']}/{guest['vmid']}",
                severity=FindingSeverity.LOW,
                title=f"{label} has {len(unused)} detached disk(s)",
                description=(
                    ", ".join(f"{k} = {v}" for k, v in sorted(unused.items()))
                    + ". These still occupy the pool but are attached to no controller, so "
                    "neither the guest nor its own disk usage accounts for them."
                ),
                evidence={"unused": unused},
            )
        )
    return issues


# ------------------------------------------------------- template / ISO clutter


def _referenced_volumes(guests: Iterable[dict[str, Any]]) -> set[str]:
    """Every volume id any guest config mentions, disks and mounted ISOs alike."""
    seen: set[str] = set()
    for guest in guests:
        for key, value in (guest.get("config") or {}).items():
            if not isinstance(value, str):
                continue
            if not (_DISK_KEY.match(key) or _UNUSED_KEY.match(key) or key == "ostemplate"):
                continue
            first = value.split(",", 1)[0]
            if _VOLUME_ID.match(first):
                seen.add(first)
    return seen


def template_clutter_issues(
    volumes: Iterable[dict[str, Any]], guests: Iterable[dict[str, Any]]
) -> list[CategoryIssue]:
    """ISO images and container templates nothing references, grouped per storage."""
    referenced = _referenced_volumes(guests)
    by_storage: dict[str, list[dict[str, Any]]] = {}
    for volume in volumes:
        volid = str(volume.get("volid") or "")
        if not volid or volid in referenced:
            continue
        match = _VOLUME_ID.match(volid)
        if match:
            by_storage.setdefault(match.group("storage"), []).append(volume)

    issues: list[CategoryIssue] = []
    for storage, rows in sorted(by_storage.items()):
        size = sum(int(r.get("size") or 0) for r in rows)
        names = sorted(str(r["volid"]).split("/", 1)[-1] for r in rows)
        issues.append(
            _issue(
                category="storage-template-clutter",
                target_type="storage",
                target_id=storage,
                severity=FindingSeverity.LOW,
                title=f"{storage}: {len(rows)} unreferenced image(s), {_human(size)}",
                description=(
                    ", ".join(names[:_NAMES_SHOWN])
                    + (
                        f" and {len(names) - _NAMES_SHOWN} more"
                        if len(names) > _NAMES_SHOWN
                        else ""
                    )
                    + ". No guest config mentions these; an installer ISO is usually "
                    "disposable once the guest is built."
                ),
                evidence={"volumes": names, "bytes": size},
            )
        )
    return issues


# ------------------------------------------------------------- released PVs


def released_pv_issues(volumes: Iterable[dict[str, Any]]) -> list[CategoryIssue]:
    """PersistentVolumes whose claim is gone but which were retained."""
    issues: list[CategoryIssue] = []
    for pv in volumes:
        status = (pv.get("status") or {}).get("phase")
        if status != "Released":
            continue
        meta = pv.get("metadata") or {}
        spec = pv.get("spec") or {}
        name = str(meta.get("name") or "?")
        claim = spec.get("claimRef") or {}
        capacity = str((spec.get("capacity") or {}).get("storage") or "?")
        issues.append(
            _issue(
                category="storage-released-pv",
                target_type="pv",
                target_id=name,
                severity=FindingSeverity.LOW,
                title=f"PersistentVolume {name} is Released ({capacity})",
                description=(
                    f"Its claim {claim.get('namespace', '?')}/{claim.get('name', '?')} is gone "
                    f"and the reclaim policy is "
                    f"{spec.get('persistentVolumeReclaimPolicy') or 'unknown'}, so the volume is "
                    "kept and cannot be bound again. Delete it, or recreate the claim to adopt it."
                ),
                evidence={
                    "capacity": capacity,
                    "claim": f"{claim.get('namespace', '?')}/{claim.get('name', '?')}",
                    "reclaim_policy": spec.get("persistentVolumeReclaimPolicy"),
                },
            )
        )
    return issues


__all__ = [
    "HEADROOM_HIGH_DAYS",
    "HEADROOM_MEDIUM_DAYS",
    "MIN_HEADROOM_SAMPLES",
    "STALE_SNAPSHOT_DAYS",
    "Projection",
    "detached_disk_issues",
    "headroom_issues",
    "project_headroom",
    "released_pv_issues",
    "snapshot_issues",
    "template_clutter_issues",
]
