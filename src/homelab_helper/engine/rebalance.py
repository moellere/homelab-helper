"""Rebalance solver — three candidate plans with tradeoffs (P5-AC3).

Takes the fleet as reconciled (host RAM capacity, running VMs' committed
memory, DIMM placements, the network topology) and produces **up to three
candidate plans**, deliberately spanning three cost classes:

1. **current-hardware** — VM migrations only. Cheapest, reversible.
2. **one-dimm-move** — move one existing DIMM from the emptiest donor to the
   most-constrained host, then fewer migrations. Physical access + downtime
   on two hosts, zero spend.
3. **one-part-purchase** — the smallest standard DIMM purchase that relieves
   the most-constrained host. Costs money, touches one host.

Deterministic greedy search, not OR-Tools: a homelab fleet is a handful of
hosts, the plan must be explainable step by step, and every step carries its
reason. (If constraint interactions ever outgrow greedy — anti-affinity,
storage co-placement, multi-resource bin packing — that's the point a real
CP solver earns its way in; the plan/step shapes here are solver-agnostic.)

Migrations respect the same physics as placement: only within a cluster, and
never across a non-LAN-grade path (live migration over a VPN is how you get a
split cluster). Two more constraints come from the fleet itself: a guest with
a volume on node-local storage cannot live-migrate and is never proposed to
(``VirtualMachine.attributes["shared_storage"]`` is False), and a host the
operator has marked ``no-new-guests`` (an ``OperationalIntent``) is never a
destination — what runs there may leave, nothing arrives. Both are listed as
caveats so a plan says what it refused to touch. Hosts with unknown RAM are
excluded from the math and listed as caveats — "we don't know" must not read
as "empty."

The framework proposes; the operator migrates/moves/buys by hand (L1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import IntentState, IntentTargetType, PartKind
from homelab_helper.db.models import (
    Cluster,
    Host,
    OperationalIntent,
    PhysicalPart,
    Placement,
    VirtualMachine,
)
from homelab_helper.engine.cluster_nodes import cluster_nodes
from homelab_helper.engine.network_path import Topology, load_topology
from homelab_helper.engine.retire import retired_host_ids
from homelab_helper.engine.rightsizing import MIN_SAMPLES as USAGE_MIN_SAMPLES
from homelab_helper.engine.usage import summarize

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_GB = 1024**3
_OS_RESERVE_BYTES = 1 * _GB
_TARGET_MAX_RATIO = 0.75  # a host above this is "constrained"
_TARGET_SPREAD = 0.20  # max-min commitment ratio the plans aim under
_DEST_FILL_CEILING = 0.85  # never plan a destination past this
_MAX_MOVES = 6
_STANDARD_DIMM_GB = (8, 16, 32, 64)
_MIN_FLEET_FOR_MOVE = 2
_MIN_DONOR_DIMMS = 2  # a donor must keep at least one DIMM


@dataclass
class VMLoad:
    name: str
    memory_bytes: int
    cluster_id: Any
    vmid: int | None
    local_storage: bool = False
    """A volume on node-local storage: the guest cannot live-migrate."""


@dataclass
class HostLoad:
    hostname: str
    host_id: Any
    mem_total: int | None
    committed: int = 0
    vms: list[VMLoad] = field(default_factory=list)
    dimms: list[tuple[str, int]] = field(default_factory=list)  # (label, bytes)
    clusters: set[Any] = field(default_factory=set)
    """Clusters this host is a node of — the only places its guests may be sent."""
    no_new_guests: bool = False
    """Operator intent: never a destination; its own guests may still leave."""

    @property
    def capacity(self) -> int | None:
        return None if self.mem_total is None else max(self.mem_total - _OS_RESERVE_BYTES, 0)

    @property
    def ratio(self) -> float | None:
        cap = self.capacity
        if cap is None or cap == 0:
            return None
        return self.committed / cap


@dataclass
class PlanStep:
    action: str  # migrate-vm | move-dimm | buy-dimm
    description: str


@dataclass
class RebalancePlan:
    name: str
    summary: str
    steps: list[PlanStep] = field(default_factory=list)
    tradeoffs: list[str] = field(default_factory=list)
    resulting_ratios: dict[str, float] = field(default_factory=dict)


@dataclass
class RebalanceReport:
    hosts: list[HostLoad] = field(default_factory=list)
    unknown_hosts: list[str] = field(default_factory=list)
    plans: list[RebalancePlan] = field(default_factory=list)
    balanced: bool = False
    unmovable: list[str] = field(default_factory=list)
    """Guests the plans will not migrate, each with the reason."""
    closed_hosts: list[str] = field(default_factory=list)
    """Hosts marked no-new-guests: never a destination."""

    @property
    def spread(self) -> float:
        ratios = [h.ratio for h in self.hosts if h.ratio is not None]
        return (max(ratios) - min(ratios)) if len(ratios) > 1 else 0.0

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view for the MCP surface (bytes stay bytes; ratios rounded)."""
        return {
            "balanced": self.balanced,
            "spread": round(self.spread, 4),
            "hosts": [
                {
                    "hostname": h.hostname,
                    "mem_total_bytes": h.mem_total,
                    "capacity_bytes": h.capacity,
                    "committed_bytes": h.committed,
                    "ratio": None if h.ratio is None else round(h.ratio, 4),
                    "vms": [
                        {"name": v.name, "memory_bytes": v.memory_bytes, "vmid": v.vmid}
                        for v in h.vms
                    ],
                    "dimms": [{"label": label, "bytes": size} for label, size in h.dimms],
                }
                for h in self.hosts
            ],
            "unknown_hosts": list(self.unknown_hosts),
            "unmovable": list(self.unmovable),
            "closed_hosts": list(self.closed_hosts),
            "plans": [
                {
                    "name": p.name,
                    "summary": p.summary,
                    "steps": [{"action": s.action, "description": s.description} for s in p.steps],
                    "tradeoffs": list(p.tradeoffs),
                    "resulting_ratios": {k: round(v, 4) for k, v in p.resulting_ratios.items()},
                }
                for p in self.plans
            ],
        }


async def _observed_memory(session: AsyncSession, vm: VirtualMachine) -> int | None:
    """30-day memory p95 for a guest, or ``None`` with too little history (Phase 8.3/8.4)."""
    cluster = await session.get(Cluster, vm.cluster_id)
    if cluster is None:
        return None
    s = await summarize(session, subject_type="guest", subject_key=f"{cluster.name}/{vm.vmid}")
    if (s.get("samples") or 0) < USAGE_MIN_SAMPLES or s.get("mem_p95") is None:
        return None
    return int(s["mem_p95"])


async def _load_fleet(
    session: AsyncSession, basis: str = "allocated"
) -> tuple[list[HostLoad], list[str]]:
    retired = await retired_host_ids(session)
    hosts = [
        h
        for h in (await session.execute(select(Host).order_by(Host.hostname))).scalars().all()
        if h.id not in retired
    ]
    by_id: dict[Any, HostLoad] = {}
    unknown: list[str] = []
    for h in hosts:
        mem = (h.capabilities or {}).get("mem_total_bytes")
        load = HostLoad(hostname=h.hostname, host_id=h.id, mem_total=int(mem) if mem else None)
        if load.mem_total is None:
            unknown.append(h.hostname)
        by_id[h.id] = load

    # Membership is the shared rule in engine/cluster_nodes.py — a NAS or a Pi
    # with free RAM is not a migration target.
    nodes = await cluster_nodes(session, hosts)
    for host_id, load in by_id.items():
        load.clusters = set(nodes.of(host_id))
    closed = {
        i.target_id
        for i in (
            await session.execute(
                select(OperationalIntent).where(
                    OperationalIntent.target_type == IntentTargetType.HOST,
                    OperationalIntent.intent == IntentState.NO_NEW_GUESTS,
                )
            )
        )
        .scalars()
        .all()
    }
    for host_id, load in by_id.items():
        load.no_new_guests = str(host_id) in closed
    for vm in (await session.execute(select(VirtualMachine))).scalars().all():
        if vm.status != "running" or vm.node_host_id not in by_id or not vm.memory_bytes:
            continue
        load = by_id[vm.node_host_id]
        memory = int(vm.memory_bytes)
        if basis == "usage":
            # Never above the allocation: a VM's reported memory includes page cache.
            observed = await _observed_memory(session, vm)
            if observed is not None:
                memory = min(memory, observed)
        load.committed += memory
        load.vms.append(
            VMLoad(
                name=vm.name,
                memory_bytes=memory,
                cluster_id=vm.cluster_id,
                vmid=vm.vmid,
                local_storage=(vm.attributes or {}).get("shared_storage") is False,
            )
        )

    rows = await session.execute(
        select(Placement, PhysicalPart)
        .join(PhysicalPart, Placement.part_id == PhysicalPart.id)
        .where(Placement.to_date.is_(None), PhysicalPart.kind == PartKind.DIMM)
    )
    for placement, part in rows.all():
        if placement.host_id in by_id and part.capacity_bytes:
            label = part.model or part.serial or "dimm"
            by_id[placement.host_id].dimms.append((str(label), int(part.capacity_bytes)))

    eligible = [load for load in by_id.values() if load.mem_total is not None]
    return sorted(eligible, key=lambda h: h.hostname), unknown


def _movable(vm: VMLoad, src: HostLoad, dst: HostLoad, topology: Topology | None) -> bool:
    """A migration the plan may propose: a node of the same cluster, LAN-grade path, fits.

    Never a guest on node-local storage, never into a ``no-new-guests`` host.
    """
    if vm.local_storage or dst.no_new_guests or vm.cluster_id not in dst.clusters:
        return False
    if topology is not None:
        path = topology.path(src.hostname, dst.hostname)
        if path is None or not (path.same_site or path.lan_grade):
            return False
    cap = dst.capacity
    return cap is not None and dst.committed + vm.memory_bytes <= cap * _DEST_FILL_CEILING


def _greedy_moves(
    hosts: list[HostLoad], topology: Topology | None
) -> tuple[list[PlanStep], dict[str, float]]:
    """Bounded largest-VM-first moves from the most- to least-loaded host."""
    committed = {h.hostname: h.committed for h in hosts}
    placed_vms = {h.hostname: list(h.vms) for h in hosts}
    steps: list[PlanStep] = []
    moved: set[int] = set()  # a VM moves at most once per plan — no ping-pong

    def ratio(h: HostLoad) -> float:
        return committed[h.hostname] / h.capacity if h.capacity else 0.0

    for _ in range(_MAX_MOVES):
        ranked = sorted(hosts, key=ratio, reverse=True)
        src = ranked[0]
        if ratio(src) - ratio(ranked[-1]) <= _TARGET_SPREAD and ratio(src) <= _TARGET_MAX_RATIO:
            break
        # Destinations are tried emptiest-first, but the emptiest host is often
        # not a legal target (other cluster, off the LAN-grade map), so the
        # search continues up the ranking instead of giving up — a 90% host
        # next to a 14% cluster-mate must find it. A move must strictly improve
        # the pair's max ratio, or the loop oscillates: overshooting swaps
        # src/dst and ping-pongs the same VM.
        move: VMLoad | None = None
        dst: HostLoad | None = None
        for candidate in reversed(ranked[1:]):
            pair_max = max(ratio(src), ratio(candidate))
            probe_dst = HostLoad(
                hostname=candidate.hostname,
                host_id=candidate.host_id,
                mem_total=candidate.mem_total,
                committed=committed[candidate.hostname],
                vms=placed_vms[candidate.hostname],
                clusters=candidate.clusters,
                no_new_guests=candidate.no_new_guests,
            )
            for vm in sorted(placed_vms[src.hostname], key=lambda v: -v.memory_bytes):
                if id(vm) in moved or not _movable(vm, src, probe_dst, topology):
                    continue
                new_src = (committed[src.hostname] - vm.memory_bytes) / (src.capacity or 1)
                new_dst = (committed[candidate.hostname] + vm.memory_bytes) / (
                    candidate.capacity or 1
                )
                if max(new_src, new_dst) < pair_max:
                    move, dst = vm, candidate
                    break
            if move is not None:
                break
        if move is None or dst is None:
            break
        moved.add(id(move))
        committed[src.hostname] -= move.memory_bytes
        committed[dst.hostname] += move.memory_bytes
        placed_vms[src.hostname].remove(move)
        placed_vms[dst.hostname].append(move)
        steps.append(
            PlanStep(
                action="migrate-vm",
                description=(
                    f"migrate VM {move.name!r} ({move.memory_bytes / _GB:.0f} GiB) "
                    f"from {src.hostname} to {dst.hostname}"
                ),
            )
        )

    ratios = {h.hostname: committed[h.hostname] / h.capacity for h in hosts if h.capacity}
    return steps, ratios


def _plan_current_hardware(
    hosts: list[HostLoad], topology: Topology | None
) -> RebalancePlan | None:
    steps, ratios = _greedy_moves(hosts, topology)
    if not steps:
        return None
    return RebalancePlan(
        name="current-hardware",
        summary=f"rebalance with VM migrations only ({len(steps)} move(s))",
        steps=steps,
        tradeoffs=[
            "no cost, no physical access",
            f"{len(steps)} live migration(s) — brief per-VM disruption",
            "cluster and LAN-grade constraints respected",
        ],
        resulting_ratios=ratios,
    )


def _shifted(
    hosts: list[HostLoad], donor: HostLoad, receiver: HostLoad, size: int
) -> list[HostLoad]:
    out = []
    for h in hosts:
        mem = h.mem_total
        if h.hostname == donor.hostname and mem is not None:
            mem = mem - size
        elif h.hostname == receiver.hostname and mem is not None:
            mem = mem + size
        out.append(
            HostLoad(
                hostname=h.hostname,
                host_id=h.host_id,
                mem_total=mem,
                committed=h.committed,
                vms=list(h.vms),
                dimms=list(h.dimms),
                clusters=set(h.clusters),
                no_new_guests=h.no_new_guests,
            )
        )
    return out


def _plan_dimm_move(hosts: list[HostLoad], topology: Topology | None) -> RebalancePlan | None:
    """Move one DIMM from the emptiest donor with spares to the tightest host."""
    ranked = sorted((h for h in hosts if h.ratio is not None), key=lambda h: h.ratio or 0)
    if len(ranked) < _MIN_FLEET_FOR_MOVE:
        return None
    receiver = ranked[-1]
    donors = [h for h in ranked[:-1] if len(h.dimms) >= _MIN_DONOR_DIMMS]
    if not donors or (receiver.ratio or 0) <= _TARGET_MAX_RATIO:
        return None
    donor = donors[0]
    label, size = min(donor.dimms, key=lambda d: d[1])
    shifted = _shifted(hosts, donor, receiver, size)
    steps = [
        PlanStep(
            action="move-dimm",
            description=(
                f"move DIMM {label} ({size / _GB:.0f} GiB) from {donor.hostname} "
                f"to {receiver.hostname} (both hosts powered down briefly)"
            ),
        )
    ]
    move_steps, ratios = _greedy_moves(shifted, topology)
    steps.extend(move_steps)
    return RebalancePlan(
        name="one-dimm-move",
        summary=(
            f"shift {size / _GB:.0f} GiB of existing RAM from {donor.hostname} "
            f"to {receiver.hostname}, then {len(move_steps)} migration(s)"
        ),
        steps=steps,
        tradeoffs=[
            "zero spend — uses hardware you already own",
            f"physical access + downtime on {donor.hostname} and {receiver.hostname}",
            f"{donor.hostname} permanently loses {size / _GB:.0f} GiB",
        ],
        resulting_ratios=ratios,
    )


def _plan_purchase(hosts: list[HostLoad], topology: Topology | None) -> RebalancePlan | None:
    """The smallest standard DIMM that relieves the most-constrained host."""
    ranked = sorted((h for h in hosts if h.ratio is not None), key=lambda h: h.ratio or 0)
    if not ranked:
        return None
    receiver = ranked[-1]
    if (receiver.ratio or 0) <= _TARGET_MAX_RATIO:
        return None
    chosen = None
    for size_gb in _STANDARD_DIMM_GB:
        cap = (receiver.mem_total or 0) + size_gb * _GB - _OS_RESERVE_BYTES
        if cap > 0 and receiver.committed / cap <= _TARGET_MAX_RATIO:
            chosen = size_gb
            break
    if chosen is None:
        chosen = _STANDARD_DIMM_GB[-1]
    shifted = _shifted(hosts, receiver, receiver, 0)
    for h in shifted:
        if h.hostname == receiver.hostname and h.mem_total is not None:
            h.mem_total += chosen * _GB
    move_steps, ratios = _greedy_moves(shifted, topology)
    steps = [
        PlanStep(
            action="buy-dimm",
            description=(
                f"buy and install one {chosen} GiB DIMM in {receiver.hostname} "
                "(match the installed generation/speed)"
            ),
        ),
        *move_steps,
    ]
    return RebalancePlan(
        name="one-part-purchase",
        summary=f"add one {chosen} GiB DIMM to {receiver.hostname}, then {len(move_steps)} migration(s)",
        steps=steps,
        tradeoffs=[
            f"costs one {chosen} GiB DIMM",
            f"downtime on {receiver.hostname} only",
            "raises total fleet capacity instead of redistributing it",
        ],
        resulting_ratios=ratios,
    )


async def plan_rebalance(
    session: AsyncSession, *, topology: Topology | None = None, basis: str = "allocated"
) -> RebalanceReport:
    """Fleet load model + up to three candidate plans across cost classes.

    ``basis="usage"`` loads each guest at its observed 30-day memory p95 (capped
    at its allocation) instead of its allocation, where history allows.
    """
    if basis not in ("allocated", "usage"):
        raise ValueError(f"basis must be allocated or usage, not {basis!r}")
    if topology is None:
        topology = load_topology()
    hosts, unknown = await _load_fleet(session, basis)
    report = RebalanceReport(hosts=hosts, unknown_hosts=unknown)
    report.unmovable = [
        f"{vm.name} on {h.hostname} (volume on node-local storage)"
        for h in hosts
        for vm in h.vms
        if vm.local_storage
    ]
    report.closed_hosts = [h.hostname for h in hosts if h.no_new_guests]

    ratios = [h.ratio for h in hosts if h.ratio is not None]
    if not ratios or (max(ratios) <= _TARGET_MAX_RATIO and report.spread <= _TARGET_SPREAD):
        report.balanced = True
        return report

    for plan in (
        _plan_current_hardware(hosts, topology),
        _plan_dimm_move(hosts, topology),
        _plan_purchase(hosts, topology),
    ):
        if plan is not None:
            report.plans.append(plan)
    return report


__all__ = [
    "HostLoad",
    "PlanStep",
    "RebalancePlan",
    "RebalanceReport",
    "plan_rebalance",
]
