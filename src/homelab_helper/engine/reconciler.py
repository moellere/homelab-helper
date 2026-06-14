"""Reconciler — observation → inventory.

This is the reconciler — what the architecture calls "the most important
component." It covers two kinds of projection today:

**Host-row projection** (rule registry, capability bag + typed columns):

- ``host.identity.*`` → the ``Host`` capability bag (kernel, OS, machine-id, …).
- ``host.cpu.*`` → ``Host.arch`` (typed column, via a value transform) and the
  capability bag (model, topology, cache, flags).
- ``host.memory.*`` → the capability bag (RAM/swap/hugepage totals from
  ``/proc/meminfo``).

**Part-lineage projection** (first-class ``PhysicalPart`` + ``Placement`` rows):

- ``host.memory.dimms`` → DIMM ``PhysicalPart`` upsert by serial, append-only
  ``Placement`` open/close. The observation value is a list of populated-slot
  dicts; the latest observation IS the current DIMM topology. Cross-host moves
  close the prior placement on the source host. DIMMs missing a serial are
  counted and skipped (a future finding-generation slice will flag them).

Future slices add ``host.storage.*`` and ``host.network.*`` projection,
SSD/NIC ``PhysicalPart`` lineage in the same pattern, the NetBox write path,
then findings.

Precedence rule today: **latest observation per (target, key) wins.** The
Observation table is append-only, so the freshest row reflects the most
recent probe run. Multi-source precedence (kernel beats management-plane)
arrives when the second source does.

Idempotency: re-running the reconciler over the same observations is a
no-op — the returned ``changes`` dict is empty, no part/placement rows
are touched, and freshness markers (``discovery_last_run`` / ``last_verified``)
bump silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import Architecture, IntentTargetType, PartKind
from homelab_helper.db.models import Host, Observation, PhysicalPart, Placement

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession


# Key the dmidecode-driven DIMM probe must emit. Value contract: list of dicts,
# one per populated slot, each with ``slot`` plus the optional PhysicalPart
# fields (``serial``, ``manufacturer``, ``part_number``, ``size_bytes``,
# ``speed_mts``, ``type``).
_DIMMS_KEY = "host.memory.dimms"


def normalize_arch(raw: Any) -> Architecture:
    """Map a raw observed architecture string onto the ``Architecture`` enum.

    Handles the common ``uname``/``lscpu`` spellings; anything unrecognized
    falls to ``OTHER`` rather than raising — an odd arch is a finding, not a
    crash.
    """
    s = str(raw).strip().lower()
    if s in ("x86_64", "amd64", "x64"):
        return Architecture.AMD64
    if s in ("aarch64", "arm64"):
        return Architecture.ARM64
    if s == "arm" or s.startswith("armv"):
        return Architecture.ARM
    return Architecture.OTHER


@dataclass(frozen=True)
class HostProjectionRule:
    """How one observation key projects onto Host state.

    Exactly one of ``attr`` or ``capability`` is set. ``attr`` writes a typed
    column on Host; ``capability`` writes a key into the JSON ``capabilities``
    bag. ``transform``, when set, maps the raw observation value before it's
    written/compared — used where the observed value (e.g. ``"x86_64"``) needs
    coercing onto a typed column's domain (e.g. ``Architecture.AMD64``).
    """

    key: str
    attr: str | None = None
    capability: str | None = None
    transform: Callable[[Any], Any] | None = None

    def __post_init__(self) -> None:
        if (self.attr is None) == (self.capability is None):
            raise ValueError(f"rule {self.key!r} must set exactly one of attr/capability")


# Observed hostname goes into capabilities, not Host.hostname — operator-set
# hostname is the identity of record. Divergence will become a finding once
# the finding-generation slice lands.
_IDENTITY_RULES: tuple[HostProjectionRule, ...] = (
    HostProjectionRule(key="host.identity.hostname", capability="observed_hostname"),
    HostProjectionRule(key="host.identity.kernel", capability="kernel"),
    HostProjectionRule(key="host.identity.machine_id", capability="machine_id"),
    HostProjectionRule(key="host.identity.os_id", capability="os_id"),
    HostProjectionRule(key="host.identity.os_pretty_name", capability="os_pretty_name"),
    HostProjectionRule(key="host.identity.boot_time_unix", capability="boot_time_unix"),
)

# CPU: architecture lands on the typed Host.arch column (via normalize_arch);
# everything else fills the capability bag under cpu_-prefixed keys.
_CPU_RULES: tuple[HostProjectionRule, ...] = (
    HostProjectionRule(key="host.cpu.architecture", attr="arch", transform=normalize_arch),
    HostProjectionRule(key="host.cpu.model", capability="cpu_model"),
    HostProjectionRule(key="host.cpu.vendor", capability="cpu_vendor"),
    HostProjectionRule(key="host.cpu.sockets", capability="cpu_sockets"),
    HostProjectionRule(key="host.cpu.cores", capability="cpu_cores"),
    HostProjectionRule(key="host.cpu.threads", capability="cpu_threads"),
    HostProjectionRule(key="host.cpu.threads_per_core", capability="cpu_threads_per_core"),
    HostProjectionRule(key="host.cpu.base_freq_mhz", capability="cpu_base_freq_mhz"),
    HostProjectionRule(key="host.cpu.max_freq_mhz", capability="cpu_max_freq_mhz"),
    HostProjectionRule(key="host.cpu.l1d_bytes", capability="cpu_l1d_bytes"),
    HostProjectionRule(key="host.cpu.l1i_bytes", capability="cpu_l1i_bytes"),
    HostProjectionRule(key="host.cpu.l2_bytes", capability="cpu_l2_bytes"),
    HostProjectionRule(key="host.cpu.l3_bytes", capability="cpu_l3_bytes"),
    HostProjectionRule(key="host.cpu.flags", capability="cpu_flags"),
    HostProjectionRule(key="host.cpu.interesting_flags", capability="cpu_interesting_flags"),
)

# Memory: RAM/swap/hugepage totals from /proc/meminfo. DIMM-level inventory
# (slots, vendors, per-DIMM size) needs dmidecode + sudo and will land with
# the PhysicalPart/Placement lineage slice — *those* keys exit the capability
# bag and become first-class rows, not capabilities.
_MEMORY_RULES: tuple[HostProjectionRule, ...] = (
    HostProjectionRule(key="host.memory.mem_total_bytes", capability="mem_total_bytes"),
    HostProjectionRule(key="host.memory.mem_available_bytes", capability="mem_available_bytes"),
    HostProjectionRule(key="host.memory.mem_free_bytes", capability="mem_free_bytes"),
    HostProjectionRule(key="host.memory.swap_total_bytes", capability="swap_total_bytes"),
    HostProjectionRule(key="host.memory.swap_free_bytes", capability="swap_free_bytes"),
    HostProjectionRule(key="host.memory.hugepages_total", capability="hugepages_total"),
    HostProjectionRule(key="host.memory.hugepages_free", capability="hugepages_free"),
    HostProjectionRule(key="host.memory.hugepagesize_bytes", capability="hugepagesize_bytes"),
)

_HOST_RULES: tuple[HostProjectionRule, ...] = _IDENTITY_RULES + _CPU_RULES + _MEMORY_RULES


@dataclass
class ReconcileResult:
    """What changed on the host as a result of reconciliation."""

    host_id: uuid.UUID
    observations_seen: int
    # Deltas only: keys are "<attr>" for typed columns, "capabilities.<key>"
    # for capability-bag entries. Empty dict ⇒ re-run was a no-op.
    changes: dict[str, Any] = field(default_factory=dict)
    # Part-lineage activity. Empty ⇒ no PhysicalPart/Placement rows were
    # written. Tuples are ``(serial_or_None, slot)`` for human readability.
    parts_upserted: int = 0
    parts_skipped_no_serial: int = 0
    placements_opened: list[tuple[str | None, str]] = field(default_factory=list)
    placements_closed: list[tuple[str | None, str]] = field(default_factory=list)

    @property
    def touched_lineage(self) -> bool:
        return bool(
            self.parts_upserted
            or self.parts_skipped_no_serial
            or self.placements_opened
            or self.placements_closed
        )


class Reconciler:
    """Apply the latest observations for a host onto its inventory state.

    Stateless and cheap to instantiate; pass a fresh ``AsyncSession`` per
    ``reconcile_host`` call so transaction boundaries are well-defined. The
    caller commits.
    """

    def __init__(self, rules: tuple[HostProjectionRule, ...] = _HOST_RULES) -> None:
        self._rules = rules
        self._rules_by_key = {r.key: r for r in rules}

    async def reconcile_host(
        self,
        session: AsyncSession,
        host_id: uuid.UUID,
    ) -> ReconcileResult:
        """Re-derive Host state from the latest observations for ``host_id``.

        Reads only the keys this reconciler knows about. Unknown keys are
        ignored — they're someone else's slice.
        """
        host = await session.get(Host, host_id)
        if host is None:
            raise ValueError(f"no Host with id {host_id}")

        # Phase 1: Host-row projection (capability bag + typed columns).
        keys = [r.key for r in self._rules]
        latest = await self._latest_per_key(session, host_id, keys)

        changes: dict[str, Any] = {}
        new_caps = dict(host.capabilities or {})

        for key, raw_value in latest.items():
            rule = self._rules_by_key[key]
            value = rule.transform(raw_value) if rule.transform is not None else raw_value
            if rule.attr is not None:
                if getattr(host, rule.attr) != value:
                    setattr(host, rule.attr, value)
                    changes[rule.attr] = value
            else:
                assert rule.capability is not None  # invariant from __post_init__
                if new_caps.get(rule.capability) != value:
                    new_caps[rule.capability] = value
                    changes[f"capabilities.{rule.capability}"] = value

        # SQLAlchemy JSON columns require reassignment (not in-place mutation)
        # to be marked dirty — only assign when the contents actually changed.
        if new_caps != (host.capabilities or {}):
            host.capabilities = new_caps

        # Phase 2: Part-lineage projection (PhysicalPart + Placement rows).
        dimms_raw = await self._latest_observation_value(session, host_id, _DIMMS_KEY)
        lineage = await self._reconcile_dimm_lineage(session, host_id, dimms_raw)

        # Freshness markers — bump on any evidence, projection or lineage.
        dimms_seen = 1 if dimms_raw is not None else 0
        if latest or dimms_seen:
            now_ts = datetime.now(UTC)
            host.discovery_last_run = now_ts
            host.last_verified = now_ts

        await session.flush()
        return ReconcileResult(
            host_id=host_id,
            observations_seen=len(latest) + dimms_seen,
            changes=changes,
            parts_upserted=lineage["parts_upserted"],
            parts_skipped_no_serial=lineage["parts_skipped_no_serial"],
            placements_opened=lineage["placements_opened"],
            placements_closed=lineage["placements_closed"],
        )

    async def _latest_per_key(
        self,
        session: AsyncSession,
        host_id: uuid.UUID,
        keys: list[str],
    ) -> dict[str, Any]:
        """Return ``{key: value}`` taking the most-recent observation per key."""
        stmt = (
            select(Observation)
            .where(
                Observation.target_type == IntentTargetType.HOST,
                Observation.target_id == str(host_id),
                Observation.key.in_(keys),
            )
            .order_by(Observation.recorded_at.desc(), Observation.id.desc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        latest: dict[str, Any] = {}
        for obs in rows:
            # dict.setdefault keeps the first row seen per key, which is the
            # newest given the ORDER BY.
            latest.setdefault(obs.key, obs.value)
        return latest

    async def _latest_observation_value(
        self,
        session: AsyncSession,
        host_id: uuid.UUID,
        key: str,
    ) -> Any:
        """Return the latest observation value for one key, or ``None`` if absent."""
        stmt = (
            select(Observation.value)
            .where(
                Observation.target_type == IntentTargetType.HOST,
                Observation.target_id == str(host_id),
                Observation.key == key,
            )
            .order_by(Observation.recorded_at.desc(), Observation.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _reconcile_dimm_lineage(
        self,
        session: AsyncSession,
        host_id: uuid.UUID,
        dimms_raw: Any,
    ) -> dict[str, Any]:
        """Project the latest ``host.memory.dimms`` observation onto rows.

        ``dimms_raw`` is the observation value (list of slot dicts) or ``None``
        if no DIMM observation has ever landed for this host. ``None`` short-
        circuits: no-op (don't close placements just because the probe hasn't
        run yet).

        DIMMs missing a serial are counted in ``parts_skipped_no_serial`` and
        otherwise ignored — without a stable identity we'd churn parts every
        run. A future finding-generation slice will surface them.
        """
        result: dict[str, Any] = {
            "parts_upserted": 0,
            "parts_skipped_no_serial": 0,
            "placements_opened": [],
            "placements_closed": [],
        }
        if not isinstance(dimms_raw, list):
            return result

        # Build the active set of (part_id, slot) that should be open after
        # this reconcile, plus side-channel state for diffing.
        active: dict[tuple[uuid.UUID, str], str | None] = {}  # → serial for reporting
        for raw_entry in dimms_raw:
            if not isinstance(raw_entry, dict):
                continue
            slot = raw_entry.get("slot")
            serial = raw_entry.get("serial")
            if not isinstance(slot, str) or not slot:
                continue
            if not isinstance(serial, str) or not serial:
                result["parts_skipped_no_serial"] += 1
                continue
            part, created = await self._upsert_dimm_part(session, serial, raw_entry)
            if created:
                result["parts_upserted"] += 1
            active[(part.id, slot)] = serial

        # Close any open placements on THIS host whose (part_id, slot) isn't
        # in the active set — DIMMs removed or relocated within the host.
        host_open_stmt = select(Placement).where(
            Placement.host_id == host_id, Placement.to_date.is_(None)
        )
        host_open_rows = (await session.execute(host_open_stmt)).scalars().all()
        host_open_keys: set[tuple[uuid.UUID, str]] = set()
        now_ts = datetime.now(UTC)
        for p in host_open_rows:
            key = (p.part_id, p.slot)
            host_open_keys.add(key)
            if key not in active:
                p.to_date = now_ts
                # Look up part serial for the report — best effort.
                closing_part = await session.get(PhysicalPart, p.part_id)
                result["placements_closed"].append(
                    (closing_part.serial if closing_part is not None else None, p.slot)
                )

        # Close placements on OTHER hosts for any active part (the DIMM moved
        # here from somewhere else) and open new placements for newly-active
        # (part, slot) pairs on THIS host.
        for (part_id, slot), serial in active.items():
            if (part_id, slot) in host_open_keys:
                continue  # already open on this host at this slot
            await self._close_placements_elsewhere(
                session, part_id, host_id, now_ts, result["placements_closed"]
            )
            session.add(
                Placement(
                    part_id=part_id,
                    host_id=host_id,
                    slot=slot,
                    from_date=now_ts,
                )
            )
            result["placements_opened"].append((serial, slot))

        if result["placements_opened"] or result["placements_closed"]:
            await session.flush()
        return result

    async def _upsert_dimm_part(
        self,
        session: AsyncSession,
        serial: str,
        entry: dict[str, Any],
    ) -> tuple[PhysicalPart, bool]:
        """Find a DIMM PhysicalPart by serial; create if missing.

        Returns ``(part, created)``. On an existing part, fill in any fields
        the latest observation provides that we don't yet have — a part first
        seen via a probe without manufacturer metadata can be enriched later
        without losing identity.
        """
        existing = (
            await session.execute(
                select(PhysicalPart).where(
                    PhysicalPart.kind == PartKind.DIMM, PhysicalPart.serial == serial
                )
            )
        ).scalar_one_or_none()
        size = entry.get("size_bytes") if isinstance(entry.get("size_bytes"), int) else None
        speed = entry.get("speed_mts") if isinstance(entry.get("speed_mts"), int) else None
        mfr = entry.get("manufacturer") if isinstance(entry.get("manufacturer"), str) else None
        model = entry.get("part_number") if isinstance(entry.get("part_number"), str) else None
        dimm_type = entry.get("type") if isinstance(entry.get("type"), str) else None

        if existing is not None:
            # Enrich nullable fields the prior probe didn't supply.
            if existing.manufacturer is None and mfr is not None:
                existing.manufacturer = mfr
            if existing.model is None and model is not None:
                existing.model = model
            if existing.capacity_bytes is None and size is not None:
                existing.capacity_bytes = size
            if existing.speed_mts is None and speed is not None:
                existing.speed_mts = speed
            if dimm_type is not None and existing.attributes.get("type") != dimm_type:
                updated_attrs = dict(existing.attributes or {})
                updated_attrs["type"] = dimm_type
                existing.attributes = updated_attrs
            return existing, False

        new_attrs: dict[str, Any] = {}
        if dimm_type is not None:
            new_attrs["type"] = dimm_type
        part = PhysicalPart(
            kind=PartKind.DIMM,
            serial=serial,
            manufacturer=mfr,
            model=model,
            capacity_bytes=size,
            speed_mts=speed,
            attributes=new_attrs,
        )
        session.add(part)
        await session.flush()
        return part, True

    async def _close_placements_elsewhere(
        self,
        session: AsyncSession,
        part_id: uuid.UUID,
        host_id: uuid.UUID,
        when: datetime,
        report: list[tuple[str | None, str]],
    ) -> None:
        """Close any open placement of ``part_id`` not on ``host_id``."""
        stmt = select(Placement).where(
            Placement.part_id == part_id,
            Placement.host_id != host_id,
            Placement.to_date.is_(None),
        )
        for p in (await session.execute(stmt)).scalars().all():
            p.to_date = when
            part = await session.get(PhysicalPart, p.part_id)
            report.append((part.serial if part is not None else None, p.slot))


__all__ = ["HostProjectionRule", "ReconcileResult", "Reconciler", "normalize_arch"]
