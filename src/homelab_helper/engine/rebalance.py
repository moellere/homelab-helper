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
split cluster). Hosts with unknown RAM are excluded from the math and listed
as caveats — "we don't know" must not read as "empty."

The framework proposes; the operator migrates/moves/buys by hand (L1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import PartKind
from homelab_helper.db.models import Host, PhysicalPart, Placement, VirtualMachine
from homelab_helper.engine.network_path import Topology, load_topology

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


@dataclass
class HostLoad:
    hostname: str
    host_id: Any
    mem_total: int | None
    committed: int = 0
    vms: list[VMLoad] = field(default_factory=list)
    dimms: list[tuple[str, int]] = field(default_factory=list)  # (label, bytes)

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

    @property
    def spread(self) -> float:
        ratios = [h.ratio for h in self.hosts if h.ratio is not None]
        return (max(ratios) - min(ratios)) if len(ratios) > 1 else 0.0


async def _load_fleet(session: AsyncSession) -> tuple[list[HostLoad], list[str]]:
    hosts = (await session.execute(select(Host).order_by(Host.hostname))).scalars().all()
    by_id: dict[Any, HostLoad] = {}
    unknown: list[str] = []
    for h in hosts:
        mem = (h.capabilities or {}).get("mem_total_bytes")
        load = HostLoad(hostname=h.hostname, host_id=h.id, mem_total=int(mem) if mem else None)
        if load.mem_total is None:
            unknown.append(h.hostname)
        by_id[h.id] = load

    for vm in (await session.execute(select(VirtualMachine))).scalars().all():
        if vm.status != "running" or vm.node_host_id not in by_id or not vm.memory_bytes:
            continue
        load = by_id[vm.node_host_id]
        load.committed += int(vm.memory_bytes)
        load.vms.append(
            VMLoad(
                name=vm.name,
                memory_bytes=int(vm.memory_bytes),
                cluster_id=vm.cluster_id,
                vmid=vm.vmid,
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
    """A migration the plan may propose: same cluster, LAN-grade path, fits."""
    if any(v.cluster_id == vm.cluster_id for v in dst.vms) or not dst.vms:
        pass  # same cluster present on dst, or dst is empty (joinable)
    else:
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

    def ratio(h: HostLoad) -> float:
        return committed[h.hostname] / h.capacity if h.capacity else 0.0

    for _ in range(_MAX_MOVES):
        ranked = sorted(hosts, key=ratio, reverse=True)
        src, dst = ranked[0], ranked[-1]
        if ratio(src) - ratio(dst) <= _TARGET_SPREAD and ratio(src) <= _TARGET_MAX_RATIO:
            break
        # A move must strictly improve the pairwise max ratio, or the loop
        # oscillates: overshooting swaps src/dst and ping-pongs the same VM.
        pair_max = max(ratio(src), ratio(dst))
        move = None
        for vm in sorted(placed_vms[src.hostname], key=lambda v: -v.memory_bytes):
            probe_dst = HostLoad(
                hostname=dst.hostname,
                host_id=dst.host_id,
                mem_total=dst.mem_total,
                committed=committed[dst.hostname],
                vms=placed_vms[dst.hostname],
            )
            if not _movable(vm, src, probe_dst, topology):
                continue
            new_src = (committed[src.hostname] - vm.memory_bytes) / (src.capacity or 1)
            new_dst = (committed[dst.hostname] + vm.memory_bytes) / (dst.capacity or 1)
            if max(new_src, new_dst) < pair_max:
                move = vm
                break
        if move is None:
            break
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
    session: AsyncSession, *, topology: Topology | None = None
) -> RebalanceReport:
    """Fleet load model + up to three candidate plans across cost classes."""
    if topology is None:
        topology = load_topology()
    hosts, unknown = await _load_fleet(session)
    report = RebalanceReport(hosts=hosts, unknown_hosts=unknown)

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
