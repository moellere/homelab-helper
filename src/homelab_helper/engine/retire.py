"""Retire hosts, merge duplicate parts, retire resolver slices — the cleanup verbs.

Three ways stale rows accumulate, all of them identity changes that no
reconcile pass owns (``docs/backlog.md``, "Host retire + part merge"):

- A host is decommissioned or reflashed under a new name. Its open
  ``Placement`` rows stay open forever, because the reconciler's rule that an
  *absent* observation never auto-resolves anything is load-bearing. Retiring
  closes them **explicitly**, records a ``DECOMMISSIONING`` intent, and
  resolves the host's open findings — the operator said so, which is exactly
  the evidence the absence rule refuses to infer.
- A drive shows up as two ``PhysicalPart`` rows because two eras reported
  different identity (a USB enclosure forging a WWN, Talos vs. Ubuntu naming
  serials differently). No heuristic links them; :func:`merge_parts` moves the
  duplicate's placement history onto the survivor and deletes it.
- A DNS resolver is renamed (``unifi`` → ``unifi:covington``) and its old
  ``ServiceEndpoint`` slice is orphaned. :func:`retire_resolver_slice` deletes
  a slice and the services left empty by it.

Every function here writes only the harness DB and is idempotent where that
makes sense (retiring twice reports ``already_retired`` and closes nothing new).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from homelab_helper.db.enums import (
    FindingStatus,
    IntentState,
    IntentTargetType,
    PowerState,
    ResolutionScope,
)
from homelab_helper.db.models import (
    Host,
    OperationalIntent,
    PhysicalPart,
    Placement,
    ReconciliationFinding,
    Service,
    ServiceEndpoint,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_MERGEABLE_FIELDS = (
    "manufacturer",
    "model",
    "serial",
    "wwid",
    "capacity_bytes",
    "speed_mts",
    "speed_mbps",
)


def _append_note(existing: str | None, line: str) -> str:
    return f"{existing}\n{line}" if existing else line


# ------------------------------------------------------------------ hosts


async def retired_host_ids(session: AsyncSession) -> set[uuid.UUID]:
    """Hosts with a DECOMMISSIONING intent — what the planners should skip."""
    rows = (
        await session.execute(
            select(OperationalIntent.target_id).where(
                OperationalIntent.target_type == IntentTargetType.HOST,
                OperationalIntent.intent == IntentState.DECOMMISSIONING,
            )
        )
    ).all()
    out: set[uuid.UUID] = set()
    for (target_id,) in rows:
        try:
            out.add(uuid.UUID(str(target_id)))
        except ValueError:
            continue
    return out


async def is_retired(session: AsyncSession, host_id: uuid.UUID) -> bool:
    return host_id in await retired_host_ids(session)


@dataclass
class RetireResult:
    hostname: str
    host_id: str
    already_retired: bool
    placements_closed: list[tuple[str | None, str]] = field(default_factory=list)
    findings_resolved: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "host_id": self.host_id,
            "already_retired": self.already_retired,
            "placements_closed": [list(p) for p in self.placements_closed],
            "findings_resolved": list(self.findings_resolved),
        }


def _affects_host(finding: ReconciliationFinding, host_id: str) -> bool:
    return any(
        a.get("target_type") == "host" and a.get("target_id") == host_id
        for a in (finding.affected or [])
    )


async def retire_host(
    session: AsyncSession,
    host: Host,
    *,
    declared_by: str,
    rationale: str | None = None,
    when: datetime | None = None,
) -> RetireResult:
    """Record the intent, close open placements, resolve the host's findings.

    Explicit, operator-attributed closure: this is the one path that may close
    a placement without an observation, and it never runs on its own.
    """
    now = when or datetime.now(UTC)
    host_id = str(host.id)
    existing = (
        (
            await session.execute(
                select(OperationalIntent).where(
                    OperationalIntent.target_type == IntentTargetType.HOST,
                    OperationalIntent.target_id == host_id,
                    OperationalIntent.intent == IntentState.DECOMMISSIONING,
                )
            )
        )
        .scalars()
        .first()
    )
    result = RetireResult(
        hostname=host.hostname, host_id=host_id, already_retired=existing is not None
    )
    if existing is None:
        session.add(
            OperationalIntent(
                target_type=IntentTargetType.HOST,
                target_id=host_id,
                intent=IntentState.DECOMMISSIONING,
                declared_at=now,
                declared_by=declared_by,
                rationale=rationale,
            )
        )

    stamp = f"closed {now.isoformat()}: host {host.hostname} retired by {declared_by}"
    open_placements = (
        await session.execute(
            select(Placement, PhysicalPart)
            .join(PhysicalPart, PhysicalPart.id == Placement.part_id)
            .where(Placement.host_id == host.id, Placement.to_date.is_(None))
        )
    ).all()
    for placement, part in open_placements:
        placement.to_date = now
        placement.notes = _append_note(placement.notes, stamp)
        result.placements_closed.append((part.serial, placement.slot))

    findings = (
        (
            await session.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.status.in_(
                        [FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    for finding in findings:
        if not _affects_host(finding, host_id):
            continue
        finding.status = FindingStatus.RESOLVED
        finding.resolved_at = now
        finding.notes = _append_note(finding.notes, f"resolved: host retired by {declared_by}")
        result.findings_resolved.append(finding.fingerprint)

    host.expected_power_state = PowerState.OFF
    host.notes = _append_note(
        host.notes,
        f"retired {now.isoformat()} by {declared_by}" + (f": {rationale}" if rationale else ""),
    )
    await session.flush()
    return result


# ------------------------------------------------------------------ parts


class PartLookupError(ValueError):
    """The reference matched no part, or more than one."""


async def find_part(session: AsyncSession, ref: str) -> PhysicalPart:
    """One part by id prefix, serial, or WWID; ambiguity is an error, never a guess."""
    needle = ref.strip()
    if not needle:
        raise PartLookupError("empty part reference")
    parts = (await session.execute(select(PhysicalPart))).scalars().all()
    lowered = needle.lower()
    matches = [
        p
        for p in parts
        if str(p.id).startswith(lowered)
        or (p.serial or "").lower() == lowered
        or (p.wwid or "").lower() == lowered
    ]
    if not matches:
        raise PartLookupError(f"no part matches {ref!r} (id prefix, serial, or wwid)")
    if len(matches) > 1:
        labels = ", ".join(
            f"{str(p.id)[:8]} ({p.kind.value} {p.serial or p.wwid or '?'})" for p in matches
        )
        raise PartLookupError(f"{ref!r} is ambiguous: {labels}")
    return matches[0]


@dataclass
class MergeResult:
    kept_id: str
    removed_id: str
    placements_moved: int = 0
    fields_filled: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kept_id": self.kept_id,
            "removed_id": self.removed_id,
            "placements_moved": self.placements_moved,
            "fields_filled": list(self.fields_filled),
        }


async def merge_parts(
    session: AsyncSession,
    duplicate: PhysicalPart,
    into: PhysicalPart,
    *,
    when: datetime | None = None,
) -> MergeResult:
    """Fold ``duplicate`` into ``into``: placements move, identity gaps fill, dup is deleted.

    The survivor's own fields always win; the duplicate only fills what the
    survivor lacks, and its identity is kept under ``attributes.merged_from``
    so the merge is auditable.
    """
    if duplicate.id == into.id:
        raise ValueError("cannot merge a part into itself")
    if duplicate.kind is not into.kind:
        raise ValueError(
            f"kind mismatch: {duplicate.kind.value} cannot merge into {into.kind.value}"
        )
    now = when or datetime.now(UTC)
    result = MergeResult(kept_id=str(into.id), removed_id=str(duplicate.id))

    placements = (
        (await session.execute(select(Placement).where(Placement.part_id == duplicate.id)))
        .scalars()
        .all()
    )
    for placement in placements:
        placement.part_id = into.id
        result.placements_moved += 1

    for name in _MERGEABLE_FIELDS:
        if getattr(into, name) is None and getattr(duplicate, name) is not None:
            setattr(into, name, getattr(duplicate, name))
            result.fields_filled.append(name)

    attrs = dict(into.attributes or {})
    for key, value in (duplicate.attributes or {}).items():
        attrs.setdefault(key, value)
    merged_from = list(attrs.get("merged_from") or [])
    merged_from.append(
        {
            "id": str(duplicate.id),
            "serial": duplicate.serial,
            "wwid": duplicate.wwid,
            "model": duplicate.model,
            "merged_at": now.isoformat(),
        }
    )
    attrs["merged_from"] = merged_from
    into.attributes = attrs  # JSON column: reassign, never mutate in place
    if duplicate.notes:
        into.notes = _append_note(into.notes, duplicate.notes)

    await session.flush()
    await session.delete(duplicate)
    await session.flush()
    return result


# ------------------------------------------------------------------ resolver slices


@dataclass
class ResolverSlice:
    scope: str
    resolver: str
    endpoints: int


async def list_resolver_slices(session: AsyncSession) -> list[ResolverSlice]:
    """Every ``(scope, resolver)`` slice with its endpoint count."""
    rows = (
        await session.execute(
            select(ServiceEndpoint.scope, ServiceEndpoint.resolver, func.count())
            .group_by(ServiceEndpoint.scope, ServiceEndpoint.resolver)
            .order_by(ServiceEndpoint.scope, ServiceEndpoint.resolver)
        )
    ).all()
    return [ResolverSlice(scope=s.value, resolver=r, endpoints=n) for s, r, n in rows]


@dataclass
class ResolverRetireResult:
    resolver: str
    scope: str | None
    endpoints_removed: int = 0
    services_removed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolver": self.resolver,
            "scope": self.scope,
            "endpoints_removed": self.endpoints_removed,
            "services_removed": list(self.services_removed),
        }


async def retire_resolver_slice(
    session: AsyncSession, resolver: str, *, scope: ResolutionScope | None = None
) -> ResolverRetireResult:
    """Delete every endpoint a resolver produced (optionally one scope) and the services it leaves empty."""
    stmt = select(ServiceEndpoint).where(ServiceEndpoint.resolver == resolver)
    if scope is not None:
        stmt = stmt.where(ServiceEndpoint.scope == scope)
    endpoints = (await session.execute(stmt)).scalars().all()
    result = ResolverRetireResult(resolver=resolver, scope=scope.value if scope else None)
    touched: set[uuid.UUID] = set()
    for ep in endpoints:
        touched.add(ep.service_id)
        await session.delete(ep)
        result.endpoints_removed += 1
    await session.flush()
    for service_id in touched:
        remaining = (
            (
                await session.execute(
                    select(ServiceEndpoint).where(ServiceEndpoint.service_id == service_id)
                )
            )
            .scalars()
            .first()
        )
        if remaining is None:
            orphan = await session.get(Service, service_id)
            if orphan is not None:
                result.services_removed.append(orphan.name)
                await session.delete(orphan)
    await session.flush()
    result.services_removed.sort()
    return result


__all__ = [
    "MergeResult",
    "PartLookupError",
    "ResolverRetireResult",
    "ResolverSlice",
    "RetireResult",
    "find_part",
    "is_retired",
    "list_resolver_slices",
    "merge_parts",
    "retire_host",
    "retire_resolver_slice",
    "retired_host_ids",
]
