"""Reconciler — observation → inventory.

This is the reconciler — what the architecture calls "the most important
component." It runs three phases:

1. **Host-row projection** (rule registry, capability bag + typed columns):

   - ``host.identity.*`` → the ``Host`` capability bag.
   - ``host.cpu.*`` → ``Host.arch`` via a value transform; rest into the bag.
   - ``host.memory.*`` → the capability bag (RAM/swap/hugepage totals).
   - ``host.storage.*`` scalars → the capability bag.
   - ``host.network.*`` scalars → the capability bag.

2. **Part-lineage projection** (first-class ``PhysicalPart`` + ``Placement``):

   - ``host.memory.dimms`` → DIMM upsert by serial.
   - ``host.storage.devices`` → SSD/HDD/NVME upsert by WWN (preferred) or
     serial. Only top-level disks become parts; partitions/LVM/RAID ignored.
   - ``host.network.interfaces`` → NIC upsert by lowercased MAC (stored in
     ``serial``). Virtual interfaces filtered by name prefix.

   Each lineage path uses append-only ``Placement`` open/close. Cross-host
   moves close the prior placement on the source host. The close-on-this-host
   scan is filtered by ``PartKind`` so one kind never disturbs another's
   placements, and absence of an observation short-circuits — never close
   placements just because the probe hasn't run yet.

3. **Finding generation + auto-resolve** (``ReconciliationFinding`` rows):

   - Each part-lineage path emits an ``INVENTORY_GAP`` finding for parts that
     lack stable identity (DIMM no serial, storage no WWN+serial, NIC no MAC).
   - Fingerprints are deterministic per ``(kind, target, root-cause-token)``;
     re-running an unchanged world updates ``last_seen`` and never duplicates.
   - The auto-resolve pass closes findings whose underlying condition has
     cleared — scoped per category so an absent observation does NOT silently
     resolve findings of that category (the critical safety property).
   - Resolved findings flip back to ``OPEN`` if the condition recurs, same
     fingerprint preserved.

Precedence rule today: **latest observation per (target, key) wins.** The
Observation table is append-only, so the freshest row reflects the most
recent probe run. Multi-source precedence (kernel beats management-plane)
arrives when the second source does.

Idempotency: re-running the reconciler over the same observations is a
no-op — the returned ``changes`` dict is empty, no part/placement/finding
rows are written, and freshness markers (``discovery_last_run`` /
``last_verified``) bump silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import (
    Architecture,
    FindingKind,
    FindingSeverity,
    FindingStatus,
    IntentTargetType,
    PartKind,
)
from homelab_helper.db.models import (
    Host,
    Observation,
    PhysicalPart,
    Placement,
    ReconciliationFinding,
)
from homelab_helper.engine.fingerprint import make_fingerprint

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession


# Lineage observation keys. The host.storage probe already emits the storage
# contract via lsblk; the host.network probe already emits the NIC contract
# via ip-link. The dmidecode-driven DIMM probe is future work but the
# reconciler treats the contract as the same shape: a list of slot dicts.
_DIMMS_KEY = "host.memory.dimms"
_STORAGE_DEVICES_KEY = "host.storage.devices"
_INTERFACES_KEY = "host.network.interfaces"

# Storage parts created via the lineage path use one of these kinds, chosen
# per-device by transport + rotational flag. The close-on-this-host filter
# uses this tuple so the storage reconcile only touches storage placements.
_STORAGE_PART_KINDS: tuple[PartKind, ...] = (
    PartKind.NVME,
    PartKind.SSD,
    PartKind.HDD,
    PartKind.OTHER,
)

# Interface-name prefixes that mean "software construct, not a physical NIC."
# Heuristic — the existing probe doesn't carry a PCI address. A future probe
# version emitting one would let us replace this with a positive
# "has-PCI-backing" test.
_VIRTUAL_NIC_PREFIXES: tuple[str, ...] = (
    "br",
    "vmbr",
    "docker",
    "virbr",
    "veth",
    "tap",
    "tun",
    "wg",
    "tailscale",
    "cilium",
    "cni",
    "flannel",
    "lxc",
    "lxd",
    "bond",
    # Hypervisor firewall plumbing (Proxmox): fwbr* bridge, fwpr*/fwln* veth pair.
    "fwbr",
    "fwpr",
    "fwln",
)


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
# arrives via host.memory.dimms (handled by the lineage path below), not here.
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

# Storage: top-level scalars from host.storage probe. Per-device PhysicalPart
# rows are created by the lineage path (_reconcile_storage_lineage) from
# host.storage.devices, not from these capability projections.
_STORAGE_RULES: tuple[HostProjectionRule, ...] = (
    HostProjectionRule(key="host.storage.disk_count", capability="disk_count"),
    HostProjectionRule(key="host.storage.disk_names", capability="disk_names"),
    HostProjectionRule(key="host.storage.total_disk_bytes", capability="total_disk_bytes"),
)

# Network: top-level scalars from host.network probe. Per-interface NIC
# PhysicalPart rows are created by the lineage path (_reconcile_nic_lineage)
# from host.network.interfaces.
_NETWORK_RULES: tuple[HostProjectionRule, ...] = (
    HostProjectionRule(key="host.network.interface_count", capability="network_interface_count"),
    HostProjectionRule(key="host.network.interface_names", capability="network_interface_names"),
)

_HOST_RULES: tuple[HostProjectionRule, ...] = (
    _IDENTITY_RULES + _CPU_RULES + _MEMORY_RULES + _STORAGE_RULES + _NETWORK_RULES
)


@dataclass
class ReconcileResult:
    """What changed on the host as a result of reconciliation."""

    host_id: uuid.UUID
    observations_seen: int
    # Deltas only: keys are "<attr>" for typed columns, "capabilities.<key>"
    # for capability-bag entries. Empty dict ⇒ re-run was a no-op.
    changes: dict[str, Any] = field(default_factory=dict)
    # Part-lineage activity. Empty ⇒ no PhysicalPart/Placement rows were
    # written. Tuples are ``(identity_or_None, slot)`` for human readability —
    # identity is the serial (DIMM/NIC) or WWN (storage) the part was keyed on.
    parts_upserted: int = 0
    parts_skipped_no_identity: int = 0
    # Entries excluded by policy rather than missing identity — e.g. virtual
    # network interfaces (docker0, br0, veth*). They have an identity (MAC) but
    # aren't physical parts; conflating with no-identity would hide the reason.
    parts_skipped_filtered: int = 0
    placements_opened: list[tuple[str | None, str]] = field(default_factory=list)
    placements_closed: list[tuple[str | None, str]] = field(default_factory=list)
    # Finding activity, by fingerprint. Each list is disjoint for one run.
    # Re-run of an unchanged world ⇒ findings_updated bumps last_seen, others
    # stay empty (idempotent for the audit output, per AC4).
    findings_opened: list[str] = field(default_factory=list)
    findings_updated: list[str] = field(default_factory=list)
    findings_resolved: list[str] = field(default_factory=list)

    @property
    def touched_lineage(self) -> bool:
        return bool(
            self.parts_upserted
            or self.parts_skipped_no_identity
            or self.parts_skipped_filtered
            or self.placements_opened
            or self.placements_closed
        )

    @property
    def touched_findings(self) -> bool:
        return bool(self.findings_opened or self.findings_resolved)


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

        # Per-run findings ledger. Lineage methods write into this; the
        # auto-resolve pass at end-of-run uses it to close findings whose
        # underlying condition is no longer present.
        #
        # categories_observed: which inventory-category observations landed
        #   this run (e.g. {"dimm", "storage"}). Auto-resolve runs ONLY for
        #   observed categories — an absent probe observation must not look
        #   like "everything in that category is now resolved."
        # active_fingerprints: category -> set of fingerprints emitted this
        #   run for that category. Anything not in the active set for an
        #   observed category transitions to RESOLVED.
        ledger: dict[str, Any] = {
            "active_fingerprints": {},
            "categories_observed": set(),
            "findings_opened": [],
            "findings_updated": [],
            "findings_resolved": [],
        }

        # Phase 2: Part-lineage projection (PhysicalPart + Placement rows).
        dimms_raw = await self._latest_observation_value(session, host_id, _DIMMS_KEY)
        dimm_summary = await self._reconcile_dimm_lineage(session, host_id, dimms_raw, ledger)
        storage_raw = await self._latest_observation_value(session, host_id, _STORAGE_DEVICES_KEY)
        storage_summary = await self._reconcile_storage_lineage(
            session, host_id, storage_raw, ledger
        )
        nic_raw = await self._latest_observation_value(session, host_id, _INTERFACES_KEY)
        nic_summary = await self._reconcile_nic_lineage(session, host_id, nic_raw, ledger)

        # Phase 3: Auto-resolve findings whose conditions are no longer present.
        await self._auto_resolve_findings(session, host_id, ledger)

        lineage_seen = sum(1 for raw in (dimms_raw, storage_raw, nic_raw) if raw is not None)
        if latest or lineage_seen:
            now_ts = datetime.now(UTC)
            host.discovery_last_run = now_ts
            host.last_verified = now_ts

        summaries = (dimm_summary, storage_summary, nic_summary)
        await session.flush()
        return ReconcileResult(
            host_id=host_id,
            observations_seen=len(latest) + lineage_seen,
            changes=changes,
            parts_upserted=sum(s["parts_upserted"] for s in summaries),
            parts_skipped_no_identity=sum(s["parts_skipped_no_identity"] for s in summaries),
            parts_skipped_filtered=sum(s.get("parts_skipped_filtered", 0) for s in summaries),
            placements_opened=[p for s in summaries for p in s["placements_opened"]],
            placements_closed=[p for s in summaries for p in s["placements_closed"]],
            findings_opened=ledger["findings_opened"],
            findings_updated=ledger["findings_updated"],
            findings_resolved=ledger["findings_resolved"],
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
        ledger: dict[str, Any],
    ) -> dict[str, Any]:
        """Project the latest ``host.memory.dimms`` observation onto rows.

        ``dimms_raw`` is the observation value (list of slot dicts) or ``None``
        if no DIMM observation has ever landed for this host. ``None`` short-
        circuits: no-op (don't close placements just because the probe hasn't
        run yet, and don't auto-resolve DIMM findings without evidence).

        DIMMs missing a serial are counted in ``parts_skipped_no_identity`` and
        each surfaces an ``INVENTORY_GAP`` finding (severity LOW) so the
        operator sees the part-by-part identity gap rather than just a counter.
        """
        result: dict[str, Any] = {
            "parts_upserted": 0,
            "parts_skipped_no_identity": 0,
            "placements_opened": [],
            "placements_closed": [],
        }
        if not isinstance(dimms_raw, list):
            return result
        ledger["categories_observed"].add("dimm")
        ledger["active_fingerprints"].setdefault("dimm", set())

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
                result["parts_skipped_no_identity"] += 1
                await self._emit_finding(
                    session,
                    host_id=host_id,
                    category="dimm",
                    root_cause_token=f"dimm:{slot}:no-serial",
                    kind=FindingKind.INVENTORY_GAP,
                    severity=FindingSeverity.LOW,
                    title=f"DIMM in {slot} has no serial",
                    description=(
                        f"Probe reported a populated DIMM in slot {slot} but no "
                        "serial was readable. Part lineage cannot track this "
                        "DIMM across moves until a higher-privilege probe "
                        "(dmidecode with sudo) supplies one."
                    ),
                    ledger=ledger,
                )
                continue
            part, created = await self._upsert_dimm_part(session, serial, raw_entry)
            if created:
                result["parts_upserted"] += 1
            active[(part.id, slot)] = serial

        # Close open DIMM placements on THIS host whose (part_id, slot) isn't
        # in the active set. The kind filter is critical: without it, the
        # DIMM reconcile would close storage/NIC placements as "missing."
        host_open_stmt = (
            select(Placement)
            .join(PhysicalPart, Placement.part_id == PhysicalPart.id)
            .where(
                Placement.host_id == host_id,
                Placement.to_date.is_(None),
                PhysicalPart.kind == PartKind.DIMM,
            )
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
            # Prefer WWN over serial — for storage parts that have both, WWN
            # is the identity of record; DIMMs/NICs have only serial.
            report.append(((part.wwid or part.serial) if part is not None else None, p.slot))

    async def _reconcile_storage_lineage(
        self,
        session: AsyncSession,
        host_id: uuid.UUID,
        devices_raw: Any,
        ledger: dict[str, Any],
    ) -> dict[str, Any]:
        """Project the latest ``host.storage.devices`` observation onto rows.

        Only top-level disks (``type == "disk"``) become PhysicalPart rows.
        Partitions, LVM volumes, and RAID arrays are logical subdivisions of
        a disk, not first-class parts. Identity is **WWN preferred, serial
        fallback** — many USB enclosures forge serials but expose WWN. Devices
        with neither are counted in ``parts_skipped_no_identity`` and surface
        an ``INVENTORY_GAP`` finding per device.

        Forged-WWN guard: some enclosures invert the trade-off and expose a
        *constant* WWN across distinct drives while still carrying a unique
        serial (the cp USB SSDs all report ``naa.5000000000000099``). When a
        WWN is already owned by a part with a different serial,
        :meth:`_resolve_storage_identity` re-keys the device by serial so the
        drives stay distinct, and a ``STORAGE_PROVENANCE_DELTA`` finding is
        raised instead of silently merging three disks into one.

        The slot label is the kernel device path (``/dev/<name>``). Cross-host
        moves close the prior placement; intra-host moves (device renamed
        because PCI scan order changed) close the old slot and open the new.
        """
        result: dict[str, Any] = {
            "parts_upserted": 0,
            "parts_skipped_no_identity": 0,
            "placements_opened": [],
            "placements_closed": [],
        }
        if not isinstance(devices_raw, list):
            return result
        ledger["categories_observed"].add("storage")
        ledger["active_fingerprints"].setdefault("storage", set())

        # We only insert keyed identities (str), never None — narrow the
        # value type accordingly so the reporting loop type-checks.
        active: dict[tuple[uuid.UUID, str], str] = {}
        for raw_entry in devices_raw:
            if not isinstance(raw_entry, dict):
                continue
            if raw_entry.get("type") != "disk":
                continue  # partitions/LVM/RAID — not physical parts
            name = raw_entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            slot = f"/dev/{name}"
            identity = _storage_identity(raw_entry)
            if identity is None:
                result["parts_skipped_no_identity"] += 1
                await self._emit_finding(
                    session,
                    host_id=host_id,
                    category="storage",
                    root_cause_token=f"storage:{slot}:no-identity",
                    kind=FindingKind.INVENTORY_GAP,
                    severity=FindingSeverity.LOW,
                    title=f"Storage device {slot} has no stable identity",
                    description=(
                        f"Probe reported a disk at {slot} but neither WWN nor "
                        "serial was readable. Part lineage cannot track this "
                        "device across moves; USB enclosures and consumer "
                        "drives are the typical cause."
                    ),
                    ledger=ledger,
                )
                continue
            identity_column, identity_value, collision_wwn = await self._resolve_storage_identity(
                session, raw_entry, identity
            )
            entry_for_upsert = await self._guard_forged_wwn(
                session,
                host_id=host_id,
                slot=slot,
                entry=raw_entry,
                serial=identity_value,
                collision_wwn=collision_wwn,
                ledger=ledger,
            )
            part, created = await self._upsert_storage_part(
                session, identity_column, identity_value, entry_for_upsert
            )
            if created:
                result["parts_upserted"] += 1
            active[(part.id, slot)] = identity_value

        # Close-on-this-host: filter to storage kinds so the DIMM/NIC lineage
        # paths' placements are untouched.
        host_open_stmt = (
            select(Placement)
            .join(PhysicalPart, Placement.part_id == PhysicalPart.id)
            .where(
                Placement.host_id == host_id,
                Placement.to_date.is_(None),
                PhysicalPart.kind.in_(_STORAGE_PART_KINDS),
            )
        )
        host_open_rows = (await session.execute(host_open_stmt)).scalars().all()
        host_open_keys: set[tuple[uuid.UUID, str]] = set()
        now_ts = datetime.now(UTC)
        for p in host_open_rows:
            key = (p.part_id, p.slot)
            host_open_keys.add(key)
            if key not in active:
                p.to_date = now_ts
                closing_part = await session.get(PhysicalPart, p.part_id)
                result["placements_closed"].append(
                    (
                        (closing_part.wwid or closing_part.serial)
                        if closing_part is not None
                        else None,
                        p.slot,
                    )
                )

        for (part_id, slot), identity_value in active.items():
            if (part_id, slot) in host_open_keys:
                continue
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
            result["placements_opened"].append((identity_value, slot))

        if result["placements_opened"] or result["placements_closed"]:
            await session.flush()
        return result

    async def _upsert_storage_part(
        self,
        session: AsyncSession,
        identity_column: str,
        identity_value: str,
        entry: dict[str, Any],
    ) -> tuple[PhysicalPart, bool]:
        """Find or create a storage PhysicalPart, keyed by WWN or serial.

        Looks up by ``(kind ∈ storage_kinds, identity_column == identity_value)``
        so a disk reclassified from SSD to NVME (transport reporting improved)
        still resolves to the same row. On a hit, fills in nullable fields and
        reclassifies kind if the latest observation disagrees. ``identity_value``
        is also written into its column on a miss so the part is permanently
        addressable by either side.
        """
        column = PhysicalPart.wwid if identity_column == "wwid" else PhysicalPart.serial
        existing = (
            await session.execute(
                select(PhysicalPart).where(
                    PhysicalPart.kind.in_(_STORAGE_PART_KINDS),
                    column == identity_value,
                )
            )
        ).scalar_one_or_none()

        kind = _classify_storage_kind(entry)
        size = entry.get("size_bytes") if isinstance(entry.get("size_bytes"), int) else None
        vendor = entry.get("vendor") if isinstance(entry.get("vendor"), str) else None
        model = entry.get("model") if isinstance(entry.get("model"), str) else None
        transport = entry.get("transport") if isinstance(entry.get("transport"), str) else None
        rotational = entry.get("rotational") if isinstance(entry.get("rotational"), bool) else None

        if existing is not None:
            _enrich_storage_part(existing, kind, vendor, model, size, transport, rotational)
            # Fill in the *other* identity field if we now know it (a disk
            # first seen via serial may report WWN on a later run).
            if identity_column == "wwid" and existing.wwid is None:
                existing.wwid = identity_value
            elif identity_column == "serial" and existing.serial is None:
                existing.serial = identity_value
            return existing, False

        new_attrs: dict[str, Any] = {}
        if transport is not None:
            new_attrs["transport"] = transport
        if rotational is not None:
            new_attrs["rotational"] = rotational
        kwargs: dict[str, Any] = {
            "kind": kind,
            "manufacturer": vendor,
            "model": model,
            "capacity_bytes": size,
            "attributes": new_attrs,
        }
        kwargs[identity_column] = identity_value
        # Record the *other* identity column too when the probe supplied both —
        # makes future lookups by either side resolve to the same part.
        other_column = "serial" if identity_column == "wwid" else "wwid"
        other_value = entry.get(other_column if other_column == "serial" else "wwn")
        if isinstance(other_value, str) and other_value and other_column not in kwargs:
            kwargs[other_column] = other_value
        part = PhysicalPart(**kwargs)
        session.add(part)
        await session.flush()
        return part, True

    async def _guard_forged_wwn(
        self,
        session: AsyncSession,
        *,
        host_id: uuid.UUID,
        slot: str,
        entry: dict[str, Any],
        serial: str,
        collision_wwn: str | None,
        ledger: dict[str, Any],
    ) -> dict[str, Any]:
        """Raise a provenance finding for a forged WWN and strip it from ``entry``.

        Passthrough when ``collision_wwn`` is ``None``. Otherwise emit a
        ``STORAGE_PROVENANCE_DELTA`` finding and return a copy of ``entry`` with
        the ``wwn`` key removed, so the forged WWN never lands on the
        serial-keyed part (which would make later WWN lookups ambiguous).
        """
        if collision_wwn is None:
            return entry
        await self._emit_finding(
            session,
            host_id=host_id,
            category="storage",
            root_cause_token=f"storage:{serial}:wwn-collision:{collision_wwn}",
            kind=FindingKind.STORAGE_PROVENANCE_DELTA,
            severity=FindingSeverity.MEDIUM,
            title=f"Storage device {slot} shares forged WWN {collision_wwn}",
            description=(
                f"Disk at {slot} reports WWN {collision_wwn}, already claimed by a "
                "different drive (distinct serial). USB/SATA enclosures commonly "
                "forge a constant WWN; without serial disambiguation the reconciler "
                "would merge these physically-distinct drives into one part and "
                f"model it as a single disk moving between hosts. Keyed by serial "
                f"{serial} instead."
            ),
            ledger=ledger,
        )
        return {k: v for k, v in entry.items() if k != "wwn"}

    async def _resolve_storage_identity(
        self,
        session: AsyncSession,
        entry: dict[str, Any],
        identity: tuple[str, str],
    ) -> tuple[str, str, str | None]:
        """Disambiguate a forged WWN via serial.

        Returns ``(identity_column, identity_value, collision_wwn)``. The
        WWN-preferred ``identity`` passes through unchanged with
        ``collision_wwn=None`` in the normal case. But when the entry is
        WWN-keyed *and* carries a serial, and that WWN is already owned by an
        earlier part with a *different* serial, the WWN is forged: re-key by
        serial (so each physical drive becomes its own part) and return the
        offending WWN so the caller raises a ``STORAGE_PROVENANCE_DELTA``
        finding. First-created part wins ownership of the WWN (deterministic via
        the time-ordered ``uuid7`` id + ``created_at``), so re-runs are stable.
        """
        column, value = identity
        if column != "wwid":
            return column, value, None
        serial = entry.get("serial")
        if not (isinstance(serial, str) and serial):
            return column, value, None  # no serial — can't disambiguate, keep WWN
        owner = (
            (
                await session.execute(
                    select(PhysicalPart)
                    .where(
                        PhysicalPart.kind.in_(_STORAGE_PART_KINDS),
                        PhysicalPart.wwid == value,
                    )
                    .order_by(PhysicalPart.created_at, PhysicalPart.id)
                )
            )
            .scalars()
            .first()
        )
        if owner is not None and owner.serial and owner.serial != serial:
            return "serial", serial, value
        return column, value, None

    async def _reconcile_nic_lineage(
        self,
        session: AsyncSession,
        host_id: uuid.UUID,
        interfaces_raw: Any,
        ledger: dict[str, Any],
    ) -> dict[str, Any]:
        """Project the latest ``host.network.interfaces`` observation onto rows.

        Filtering: only ``link_type == "ether"`` entries with a non-virtual name
        become PhysicalParts. Virtual interfaces (bridges, veth, docker, wg, …)
        are counted in ``parts_skipped_filtered`` — they have a MAC but aren't
        physical parts, and conflating with no-identity would hide the reason.
        Real NICs missing a MAC (rare) hit ``parts_skipped_no_identity`` and
        produce an ``INVENTORY_GAP`` finding.

        Slot label is the kernel interface name; a future probe version
        emitting PCI addresses can switch to a more stable identifier.
        """
        result: dict[str, Any] = {
            "parts_upserted": 0,
            "parts_skipped_no_identity": 0,
            "parts_skipped_filtered": 0,
            "placements_opened": [],
            "placements_closed": [],
        }
        if not isinstance(interfaces_raw, list):
            return result
        ledger["categories_observed"].add("nic")
        ledger["active_fingerprints"].setdefault("nic", set())

        active: dict[tuple[uuid.UUID, str], str] = {}
        for raw_entry in interfaces_raw:
            if not isinstance(raw_entry, dict):
                continue
            if not _is_real_nic(raw_entry):
                mac = raw_entry.get("mac")
                if isinstance(mac, str) and mac:
                    result["parts_skipped_filtered"] += 1
                continue
            name = raw_entry.get("name")
            assert isinstance(name, str)  # _is_real_nic narrowed this
            mac_norm = _normalize_mac(raw_entry.get("mac"))
            if mac_norm is None:
                result["parts_skipped_no_identity"] += 1
                await self._emit_finding(
                    session,
                    host_id=host_id,
                    category="nic",
                    root_cause_token=f"nic:{name}:no-mac",
                    kind=FindingKind.INVENTORY_GAP,
                    severity=FindingSeverity.LOW,
                    title=f"Network interface {name} has no MAC",
                    description=(
                        f"Probe reported an ether interface {name} but no MAC "
                        "was readable. Part lineage cannot track this NIC."
                    ),
                    ledger=ledger,
                )
                continue
            part, created = await self._upsert_nic_part(session, mac_norm, raw_entry)
            if created:
                result["parts_upserted"] += 1
            active[(part.id, name)] = mac_norm

        host_open_stmt = (
            select(Placement)
            .join(PhysicalPart, Placement.part_id == PhysicalPart.id)
            .where(
                Placement.host_id == host_id,
                Placement.to_date.is_(None),
                PhysicalPart.kind == PartKind.NIC,
            )
        )
        host_open_rows = (await session.execute(host_open_stmt)).scalars().all()
        host_open_keys: set[tuple[uuid.UUID, str]] = set()
        now_ts = datetime.now(UTC)
        for p in host_open_rows:
            key = (p.part_id, p.slot)
            host_open_keys.add(key)
            if key not in active:
                p.to_date = now_ts
                closing_part = await session.get(PhysicalPart, p.part_id)
                result["placements_closed"].append(
                    (closing_part.serial if closing_part is not None else None, p.slot)
                )

        for (part_id, slot), mac in active.items():
            if (part_id, slot) in host_open_keys:
                continue
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
            result["placements_opened"].append((mac, slot))

        if result["placements_opened"] or result["placements_closed"]:
            await session.flush()
        return result

    async def _upsert_nic_part(
        self,
        session: AsyncSession,
        mac: str,
        entry: dict[str, Any],
    ) -> tuple[PhysicalPart, bool]:
        """Find or create a NIC PhysicalPart keyed by MAC (stored in ``serial``).

        NICs use only the ``serial`` column for identity — MAC is the natural
        permanent ID; there's no WWN analogue. Speed lives in ``speed_mbps``;
        MTU / link_type / master / addresses go into attributes.
        """
        existing = (
            await session.execute(
                select(PhysicalPart).where(
                    PhysicalPart.kind == PartKind.NIC, PhysicalPart.serial == mac
                )
            )
        ).scalar_one_or_none()

        speed_mbps = entry.get("speed_mbps") if isinstance(entry.get("speed_mbps"), int) else None
        mtu = entry.get("mtu") if isinstance(entry.get("mtu"), int) else None
        link_type = entry.get("link_type") if isinstance(entry.get("link_type"), str) else None
        master = entry.get("master") if isinstance(entry.get("master"), str) else None

        extracted_attrs: dict[str, Any] = {}
        if mtu is not None:
            extracted_attrs["mtu"] = mtu
        if link_type is not None:
            extracted_attrs["link_type"] = link_type
        if master is not None:
            extracted_attrs["master"] = master

        if existing is not None:
            if existing.speed_mbps is None and speed_mbps is not None:
                existing.speed_mbps = speed_mbps
            if extracted_attrs:
                merged = {**(existing.attributes or {}), **extracted_attrs}
                if merged != (existing.attributes or {}):
                    existing.attributes = merged
            return existing, False

        part = PhysicalPart(
            kind=PartKind.NIC,
            serial=mac,
            speed_mbps=speed_mbps,
            attributes=extracted_attrs,
        )
        session.add(part)
        await session.flush()
        return part, True

    async def _emit_finding(
        self,
        session: AsyncSession,
        *,
        host_id: uuid.UUID,
        category: str,
        root_cause_token: str,
        kind: FindingKind,
        severity: FindingSeverity,
        title: str,
        description: str,
        ledger: dict[str, Any],
    ) -> None:
        """Upsert a ReconciliationFinding by deterministic fingerprint.

        New conditions create a row with ``status=OPEN``. Re-encountering an
        existing OPEN/ACKNOWLEDGED finding bumps ``last_seen`` and refreshes
        the human-facing fields (title/description) in case the wording
        changed between runs. A previously RESOLVED finding whose condition
        recurs flips back to OPEN with ``first_seen`` reset to now.

        ``category`` ("dimm" | "storage" | "nic") is encoded into the
        ``affected`` list so the auto-resolve pass can scope correctly: only
        findings of an observed category should ever be auto-closed.
        """
        fp = make_fingerprint(kind.value, "host", str(host_id), root_cause_token)
        ledger["active_fingerprints"][category].add(fp)
        affected: list[dict[str, str]] = [
            {"target_type": "host", "target_id": str(host_id)},
            {"target_type": "inventory-category", "target_id": category},
        ]

        existing = (
            await session.execute(
                select(ReconciliationFinding).where(ReconciliationFinding.fingerprint == fp)
            )
        ).scalar_one_or_none()
        now_ts = datetime.now(UTC)

        if existing is not None:
            if existing.status == FindingStatus.RESOLVED:
                existing.status = FindingStatus.OPEN
                existing.resolved_at = None
                existing.first_seen = now_ts
                ledger["findings_opened"].append(fp)
            else:
                ledger["findings_updated"].append(fp)
            existing.last_seen = now_ts
            if existing.title != title:
                existing.title = title
            if existing.description != description:
                existing.description = description
            existing.affected = affected
            return

        session.add(
            ReconciliationFinding(
                kind=kind,
                severity=severity,
                fingerprint=fp,
                title=title,
                description=description,
                affected=affected,
                status=FindingStatus.OPEN,
                first_seen=now_ts,
                last_seen=now_ts,
            )
        )
        ledger["findings_opened"].append(fp)

    async def _auto_resolve_findings(
        self,
        session: AsyncSession,
        host_id: uuid.UUID,
        ledger: dict[str, Any],
    ) -> None:
        """Close findings whose underlying condition is no longer present.

        Scoped per-category: only categories observed THIS run participate.
        A finding tagged ``category=dimm`` won't be auto-resolved unless the
        run actually saw a DIMM observation — otherwise an absent probe
        observation would silently look like "everything's fine now."
        """
        observed_cats: set[str] = ledger["categories_observed"]
        if not observed_cats:
            return
        active_by_cat: dict[str, set[str]] = ledger["active_fingerprints"]
        now_ts = datetime.now(UTC)

        stmt = select(ReconciliationFinding).where(
            ReconciliationFinding.kind == FindingKind.INVENTORY_GAP,
            ReconciliationFinding.status.in_([FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED]),
        )
        for f in (await session.execute(stmt)).scalars().all():
            host_match = False
            category: str | None = None
            for a in f.affected or []:
                if a.get("target_type") == "host" and a.get("target_id") == str(host_id):
                    host_match = True
                elif a.get("target_type") == "inventory-category":
                    category = a.get("target_id")
            if not host_match or category is None:
                continue
            if category not in observed_cats:
                continue  # no probe evidence this run — don't presume "gone"
            if f.fingerprint in active_by_cat.get(category, set()):
                continue  # still actively reported
            f.status = FindingStatus.RESOLVED
            f.resolved_at = now_ts
            ledger["findings_resolved"].append(f.fingerprint)


def _storage_identity(entry: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``("wwid", wwn)`` or ``("serial", serial)`` for a storage entry.

    WWN preferred — USB enclosures and consumer drives often fake serials but
    expose stable WWN. Empty strings count as missing.
    """
    wwn = entry.get("wwn")
    if isinstance(wwn, str) and wwn:
        return ("wwid", wwn)
    serial = entry.get("serial")
    if isinstance(serial, str) and serial:
        return ("serial", serial)
    return None


def _classify_storage_kind(entry: dict[str, Any]) -> PartKind:
    """Choose ``PartKind`` from probe metadata. Unknown → OTHER (never raises)."""
    if entry.get("transport") == "nvme":
        return PartKind.NVME
    rotational = entry.get("rotational")
    if rotational is True:
        return PartKind.HDD
    if rotational is False:
        return PartKind.SSD
    return PartKind.OTHER


def _enrich_storage_part(
    existing: PhysicalPart,
    kind: PartKind,
    vendor: str | None,
    model: str | None,
    size: int | None,
    transport: str | None,
    rotational: bool | None,
) -> None:
    """Fill in nullable PhysicalPart fields and refresh storage attributes.

    Only writes when the new value is non-null AND the existing value is null
    or stale — a probe regression that loses a field doesn't blank what we
    already know.
    """
    if existing.kind != kind:
        existing.kind = kind
    if existing.manufacturer is None and vendor is not None:
        existing.manufacturer = vendor
    if existing.model is None and model is not None:
        existing.model = model
    if existing.capacity_bytes is None and size is not None:
        existing.capacity_bytes = size
    updated_attrs = dict(existing.attributes or {})
    attrs_changed = False
    if transport is not None and updated_attrs.get("transport") != transport:
        updated_attrs["transport"] = transport
        attrs_changed = True
    if rotational is not None and updated_attrs.get("rotational") != rotational:
        updated_attrs["rotational"] = rotational
        attrs_changed = True
    if attrs_changed:
        existing.attributes = updated_attrs


def _is_real_nic(entry: dict[str, Any]) -> bool:
    """Heuristic — return True for entries that look like physical NICs.

    Drops loopback / non-ether link types and the common virtual-interface
    name prefixes. The probe today doesn't expose PCI addresses; once it does,
    this can be replaced with a positive "has-PCI-backing" check.
    """
    if entry.get("link_type") != "ether":
        return False
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        return False
    return not any(name.startswith(p) for p in _VIRTUAL_NIC_PREFIXES)


def _normalize_mac(mac: Any) -> str | None:
    """Lower-case + strip so case-only-different MACs merge to one identity."""
    if not isinstance(mac, str):
        return None
    s = mac.strip().lower()
    return s or None


__all__ = ["HostProjectionRule", "ReconcileResult", "Reconciler", "normalize_arch"]
