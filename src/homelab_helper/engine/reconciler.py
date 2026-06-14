"""Reconciler — observation → inventory.

This is the reconciler — what the architecture calls "the most important
component." It currently covers three namespaces:

- ``host.identity.*`` → the ``Host`` capability bag (kernel, OS, machine-id, …).
- ``host.cpu.*`` → ``Host.arch`` (typed column, via a value transform) and the
  capability bag (model, topology, cache, flags).
- ``host.memory.*`` → the capability bag (RAM/swap/hugepage totals from
  ``/proc/meminfo``). DIMM-level inventory arrives with the lineage slice as
  first-class ``PhysicalPart`` / ``Placement`` rows, not capabilities.

Future slices add ``host.storage.*``, ``host.network.*``, then part lineage
(PhysicalPart/Placement), then the NetBox write path, then findings. The shape
here is deliberately easy to extend by appending rules; no other code changes
when a new key lands.

Precedence rule in this slice: **latest observation per (target, key) wins.**
The Observation table is append-only, so the freshest row reflects the most
recent probe run. Multi-source precedence (kernel beats management-plane)
arrives when the second source does.

Idempotency: re-running the reconciler over the same observations is a
no-op — the returned ``changes`` dict is empty, and freshness markers
(``discovery_last_run`` / ``last_verified``) bump silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import Architecture, IntentTargetType
from homelab_helper.db.models import Host, Observation

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession


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

        if latest:
            now_ts = datetime.now(UTC)
            host.discovery_last_run = now_ts
            host.last_verified = now_ts

        await session.flush()
        return ReconcileResult(
            host_id=host_id,
            observations_seen=len(latest),
            changes=changes,
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


__all__ = ["HostProjectionRule", "ReconcileResult", "Reconciler", "normalize_arch"]
