"""Bottleneck analyzer — known-pattern detection with generated mitigations (P5-AC4).

A small library of deterministic patterns over the reconciled fleet. The
*patterns* are curated; the *output* is not — every hit's subjects, evidence,
and candidate mitigations are constructed from the detected facts (which host,
which speeds, which VM), never from canned text about a hypothetical lab.

Patterns in this slice:

- **cluster-link-asymmetry** → ``CEPH_BOTTLENECK``. Nodes of one cluster whose
  fastest NICs differ: replicated storage traffic runs at the slowest member's
  speed. Generates the four classic mitigations from the detected facts:
  reweight data away from the slow node, upgrade its link to the fleet speed,
  relocate storage daemons to a full-speed node, or accept it as a tier.
- **memory-pressure** → ``CHOKEPOINT``. A host committed past 90%: migrate the
  named largest VM, add RAM, or accept.
- **storage-single-uplink** → ``CHOKEPOINT``. A bulk-storage host on a single
  ≤1 GbE uplink serving a multi-node fleet: every consumer shares that link.

``--persist`` records hits as findings with the standard fingerprint +
reopen-on-recurrence lifecycle. Because every pattern is evaluated on every
run, a previously open analyzer finding whose condition no longer holds is
resolved — the category was observed, so this is not absence-auto-resolve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus, PartKind
from homelab_helper.db.models import (
    Cluster,
    Host,
    PhysicalPart,
    Placement,
    ReconciliationFinding,
    VirtualMachine,
)
from homelab_helper.engine.fingerprint import make_fingerprint
from homelab_helper.engine.retire import retired_host_ids

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_GB = 1024**3
_TB = 1024**4

_MEMORY_PRESSURE_RATIO = 0.90
_OS_RESERVE_BYTES = 1 * _GB
_BULK_STORAGE_BYTES = 2 * _TB
_SLOW_UPLINK_MBPS = 1000
_MIN_CLUSTER_NODES = 2
_IDLE_RATIO = 0.5  # a migration destination should be under half-committed
_EVIDENCE_TAG = "bottleneck_pattern"


@dataclass
class BottleneckHit:
    pattern: str
    kind: FindingKind
    severity: FindingSeverity
    subject_type: str  # "cluster" | "host"
    subject: str
    title: str
    description: str
    evidence: dict[str, Any] = field(default_factory=dict)
    mitigations: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return make_fingerprint(self.kind.value, self.subject_type, self.subject, self.pattern)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view for the MCP surface (enums as values)."""
        return {
            "pattern": self.pattern,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "subject_type": self.subject_type,
            "subject": self.subject,
            "title": self.title,
            "description": self.description,
            "evidence": dict(self.evidence),
            "mitigations": list(self.mitigations),
            "fingerprint": self.fingerprint,
        }


@dataclass
class _Fleet:
    hosts: list[Host]
    nic_speed: dict[Any, int]  # host_id -> fastest NIC Mbps
    nic_count: dict[Any, int]
    cluster_nodes: dict[str, list[Host]]  # cluster name -> member hosts
    committed: dict[Any, int]  # host_id -> running VM bytes
    largest_vm: dict[Any, tuple[str, int]]  # host_id -> (name, bytes)


async def _load_fleet(session: AsyncSession) -> _Fleet:
    retired = await retired_host_ids(session)
    hosts = [
        h
        for h in (await session.execute(select(Host).order_by(Host.hostname))).scalars().all()
        if h.id not in retired
    ]
    by_id = {h.id: h for h in hosts}

    nic_speed: dict[Any, int] = {}
    nic_count: dict[Any, int] = {}
    rows = await session.execute(
        select(Placement, PhysicalPart)
        .join(PhysicalPart, Placement.part_id == PhysicalPart.id)
        .where(Placement.to_date.is_(None), PhysicalPart.kind == PartKind.NIC)
    )
    for placement, part in rows.all():
        nic_count[placement.host_id] = nic_count.get(placement.host_id, 0) + 1
        if part.speed_mbps:
            current = nic_speed.get(placement.host_id, 0)
            nic_speed[placement.host_id] = max(current, int(part.speed_mbps))

    clusters = {c.id: c.name for c in (await session.execute(select(Cluster))).scalars().all()}
    cluster_hosts: dict[str, dict[Any, Host]] = {}
    committed: dict[Any, int] = {}
    largest_vm: dict[Any, tuple[str, int]] = {}
    for vm in (await session.execute(select(VirtualMachine))).scalars().all():
        if vm.node_host_id not in by_id:
            continue
        cluster_name = clusters.get(vm.cluster_id)
        if cluster_name is not None:
            cluster_hosts.setdefault(cluster_name, {})[vm.node_host_id] = by_id[vm.node_host_id]
        if vm.status == "running" and vm.memory_bytes:
            committed[vm.node_host_id] = committed.get(vm.node_host_id, 0) + int(vm.memory_bytes)
            best = largest_vm.get(vm.node_host_id)
            if best is None or int(vm.memory_bytes) > best[1]:
                largest_vm[vm.node_host_id] = (vm.name, int(vm.memory_bytes))

    return _Fleet(
        hosts=hosts,
        nic_speed=nic_speed,
        nic_count=nic_count,
        cluster_nodes={
            name: sorted(m.values(), key=lambda h: h.hostname) for name, m in cluster_hosts.items()
        },
        committed=committed,
        largest_vm=largest_vm,
    )


def _cap(host: Host, key: str) -> Any:
    return (host.capabilities or {}).get(key)


def _detect_link_asymmetry(fleet: _Fleet) -> list[BottleneckHit]:
    hits: list[BottleneckHit] = []
    for cluster_name, nodes in fleet.cluster_nodes.items():
        speeds = {h.hostname: fleet.nic_speed.get(h.id) for h in nodes}
        known = {name: s for name, s in speeds.items() if s}
        if len(known) < _MIN_CLUSTER_NODES:
            continue
        fast_speed = max(known.values())
        slow = [name for name, s in known.items() if s < fast_speed]
        if not slow:
            continue
        slow_names = ", ".join(slow)
        slow_speed = min(known.values())
        fast_nodes = [name for name, s in known.items() if s == fast_speed and name not in slow]
        relocate_target = fast_nodes[0] if fast_nodes else "a full-speed node"
        hits.append(
            BottleneckHit(
                pattern="cluster-link-asymmetry",
                kind=FindingKind.CEPH_BOTTLENECK,
                severity=FindingSeverity.HIGH
                if fast_speed >= 2 * slow_speed
                else FindingSeverity.MEDIUM,
                subject_type="cluster",
                subject=cluster_name,
                title=f"Link asymmetry in cluster {cluster_name}: {slow_names} at {slow_speed} Mbps",
                description=(
                    f"Cluster {cluster_name!r} nodes have asymmetric uplinks: "
                    + ", ".join(f"{n}={s} Mbps" for n, s in sorted(known.items()))
                    + ". Replicated storage traffic (Ceph, DRBD, longhorn) runs at the "
                    "slowest member's speed — every write replicating through "
                    f"{slow_names} is capped at {slow_speed} Mbps."
                ),
                evidence={"speeds_mbps": known},
                mitigations=[
                    f"rebalance replicated data away from {slow_names} "
                    f"(Ceph: CRUSH-reweight toward the {fast_speed} Mbps nodes)",
                    f"bring {slow_names} to {fast_speed} Mbps — e.g. a USB "
                    f"{fast_speed / 1000:g} GbE adapter — to remove the asymmetry",
                    f"relocate storage daemons/OSDs from {slow_names} to {relocate_target}",
                    f"accept: declare {slow_names} a capacity/cold tier and keep the link as-is",
                ],
            )
        )
    return hits


def _detect_memory_pressure(fleet: _Fleet) -> list[BottleneckHit]:
    hits: list[BottleneckHit] = []
    idle_hosts = [
        h.hostname
        for h in fleet.hosts
        if _cap(h, "mem_total_bytes")
        and fleet.committed.get(h.id, 0)
        / max(int(_cap(h, "mem_total_bytes")) - _OS_RESERVE_BYTES, 1)
        < _IDLE_RATIO
    ]
    for host in fleet.hosts:
        mem = _cap(host, "mem_total_bytes")
        if not mem:
            continue
        capacity = max(int(mem) - _OS_RESERVE_BYTES, 1)
        ratio = fleet.committed.get(host.id, 0) / capacity
        if ratio <= _MEMORY_PRESSURE_RATIO:
            continue
        biggest = fleet.largest_vm.get(host.id)
        mitigations = []
        if biggest and idle_hosts:
            mitigations.append(
                f"migrate VM {biggest[0]!r} ({biggest[1] / _GB:.0f} GiB) to "
                f"{idle_hosts[0]} (see `helper plan rebalance`)"
            )
        mitigations.append(f"add RAM to {host.hostname} (see `helper plan rebalance` for sizing)")
        mitigations.append("accept: the host runs hot by design — declare it and monitor swap")
        hits.append(
            BottleneckHit(
                pattern="memory-pressure",
                kind=FindingKind.CHOKEPOINT,
                severity=FindingSeverity.HIGH,
                subject_type="host",
                subject=host.hostname,
                title=f"Memory pressure on {host.hostname}: {ratio:.0%} committed",
                description=(
                    f"{host.hostname} has {fleet.committed.get(host.id, 0) / _GB:.0f} GiB "
                    f"committed to running VMs of {capacity / _GB:.0f} GiB usable "
                    f"({ratio:.0%}) — ballooning and OOM risk under load."
                ),
                evidence={"ratio": round(ratio, 2)},
                mitigations=mitigations,
            )
        )
    return hits


def _detect_storage_single_uplink(fleet: _Fleet) -> list[BottleneckHit]:
    hits: list[BottleneckHit] = []
    consumer_count = sum(1 for h in fleet.hosts if fleet.committed.get(h.id, 0) > 0)
    for host in fleet.hosts:
        disk = _cap(host, "total_disk_bytes")
        if not disk or int(disk) < _BULK_STORAGE_BYTES:
            continue
        nics = fleet.nic_count.get(host.id, 0)
        speed = fleet.nic_speed.get(host.id)
        if (
            nics != 1
            or (speed and speed > _SLOW_UPLINK_MBPS)
            or consumer_count < _MIN_CLUSTER_NODES
        ):
            continue
        speed_txt = f"{speed} Mbps" if speed else "unknown speed"
        hits.append(
            BottleneckHit(
                pattern="storage-single-uplink",
                kind=FindingKind.CHOKEPOINT,
                severity=FindingSeverity.MEDIUM,
                subject_type="host",
                subject=host.hostname,
                title=f"Bulk storage on {host.hostname} rides one {speed_txt} uplink",
                description=(
                    f"{host.hostname} carries {int(disk) / _TB:.1f} TiB and serves a "
                    f"{consumer_count}-consumer fleet through a single {speed_txt} NIC — "
                    "NFS/SMB/backup traffic all contend for that one link."
                ),
                evidence={"nics": nics, "speed_mbps": speed, "disk_tib": round(int(disk) / _TB, 1)},
                mitigations=[
                    f"add a second NIC to {host.hostname} and bond or segment storage traffic",
                    f"upgrade {host.hostname}'s uplink beyond {speed_txt}",
                    "move the heaviest consumers onto the storage host itself (data gravity)",
                    "accept: current traffic fits comfortably in the link — monitor it",
                ],
            )
        )
    return hits


async def analyze_bottlenecks(session: AsyncSession) -> list[BottleneckHit]:
    """Run every pattern over the reconciled fleet."""
    fleet = await _load_fleet(session)
    hits: list[BottleneckHit] = []
    hits.extend(_detect_link_asymmetry(fleet))
    hits.extend(_detect_memory_pressure(fleet))
    hits.extend(_detect_storage_single_uplink(fleet))
    return hits


@dataclass
class BottleneckPersistResult:
    opened: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)


async def persist_bottlenecks(
    session: AsyncSession, hits: list[BottleneckHit], *, when: datetime | None = None
) -> BottleneckPersistResult:
    """Standard finding lifecycle for analyzer hits.

    All patterns ran this call, so an open analyzer finding not re-detected has
    genuinely cleared → resolve. (Analyzer findings are identified by the
    ``bottleneck_pattern`` evidence tag; findings from other generators that
    share these kinds are never touched.)
    """
    result = BottleneckPersistResult()
    now_ts = when or datetime.now(UTC)
    seen = {hit.fingerprint for hit in hits}

    for hit in hits:
        existing = (
            await session.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.fingerprint == hit.fingerprint
                )
            )
        ).scalar_one_or_none()
        proposed = [{"summary": m} for m in hit.mitigations]
        affected = [{"target_type": hit.subject_type, "target_id": hit.subject}]
        evidence = [{"type": _EVIDENCE_TAG, "id": hit.pattern}]
        if existing is None:
            session.add(
                ReconciliationFinding(
                    kind=hit.kind,
                    severity=hit.severity,
                    fingerprint=hit.fingerprint,
                    title=hit.title,
                    description=hit.description,
                    affected=affected,
                    evidence_refs=evidence,
                    proposed_actions=proposed,
                    status=FindingStatus.OPEN,
                    first_seen=now_ts,
                    last_seen=now_ts,
                )
            )
            result.opened.append(hit.subject)
        else:
            if existing.status == FindingStatus.RESOLVED:
                existing.status = FindingStatus.OPEN
                existing.resolved_at = None
                existing.first_seen = now_ts
                result.reopened.append(hit.subject)
            else:
                result.updated.append(hit.subject)
            existing.last_seen = now_ts
            existing.severity = hit.severity
            existing.title = hit.title
            existing.description = hit.description
            existing.affected = affected
            existing.evidence_refs = evidence
            existing.proposed_actions = proposed

    open_rows = (
        (
            await session.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.status.in_(
                        [FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED]
                    ),
                    ReconciliationFinding.kind.in_(
                        [FindingKind.CEPH_BOTTLENECK, FindingKind.CHOKEPOINT]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in open_rows:
        is_ours = any(e.get("type") == _EVIDENCE_TAG for e in (row.evidence_refs or []))
        if is_ours and row.fingerprint not in seen:
            row.status = FindingStatus.RESOLVED
            row.resolved_at = now_ts
            result.resolved.append(row.title)

    await session.flush()
    return result


__all__ = [
    "BottleneckHit",
    "BottleneckPersistResult",
    "analyze_bottlenecks",
    "persist_bottlenecks",
]
