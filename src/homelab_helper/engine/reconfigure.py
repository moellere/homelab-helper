"""Reconfiguration reasoner — surplus capacity → options (P5-AC5).

The mirror image of the bottleneck analyzer: instead of "where does it hurt,"
this asks "what are you *not using*, and what could that hardware be doing?"
The AC's motivating case is a node sitting on 24 GB of RAM with two stopped
VMs — the reasoner proposes exactly the three honest options: spin the VMs
back up, move the DIMMs somewhere loaded, or declare the reserve deliberate.

Detection is structural: a host with real capacity whose running commitment is
low, that also has *reconfigurable slack* — stopped VMs (capacity already
provisioned but idle) or spare DIMMs (capacity that could serve another host).
Options are generated from the detected facts (which VMs, which DIMMs, which
loaded host would benefit); an "accept" option is always present because a
deliberate reserve is a legitimate configuration, and declaring it
(``OperationalIntent`` / stopped-by-design) is how it stops resurfacing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import PartKind
from homelab_helper.db.models import Host, PhysicalPart, Placement, VirtualMachine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_GB = 1024**3
_OS_RESERVE_BYTES = 1 * _GB
_SURPLUS_MAX_RATIO = 0.25
_LOADED_MIN_RATIO = 0.60
_MIN_KEEP_DIMMS = 1


@dataclass
class SurplusHit:
    hostname: str
    ram_gb: float
    ratio: float
    cpu_model: str | None
    stopped_vms: list[str] = field(default_factory=list)
    spare_dimm_gb: list[float] = field(default_factory=list)
    options: list[str] = field(default_factory=list)


async def analyze_surplus(session: AsyncSession) -> list[SurplusHit]:
    """Hosts with capacity to spare and something reconfigurable about it."""
    hosts = list((await session.execute(select(Host).order_by(Host.hostname))).scalars().all())

    committed: dict[Any, int] = {}
    stopped: dict[Any, list[str]] = {}
    for vm in (await session.execute(select(VirtualMachine))).scalars().all():
        if vm.node_host_id is None:
            continue
        if vm.status == "running" and vm.memory_bytes:
            committed[vm.node_host_id] = committed.get(vm.node_host_id, 0) + int(vm.memory_bytes)
        elif vm.status == "stopped" and not vm.template:
            stopped.setdefault(vm.node_host_id, []).append(vm.name)

    dimms: dict[Any, list[int]] = {}
    rows = await session.execute(
        select(Placement, PhysicalPart)
        .join(PhysicalPart, Placement.part_id == PhysicalPart.id)
        .where(Placement.to_date.is_(None), PhysicalPart.kind == PartKind.DIMM)
    )
    for placement, part in rows.all():
        if part.capacity_bytes:
            dimms.setdefault(placement.host_id, []).append(int(part.capacity_bytes))

    def ratio_of(h: Host) -> float | None:
        mem = (h.capabilities or {}).get("mem_total_bytes")
        if not mem:
            return None
        capacity = max(int(mem) - _OS_RESERVE_BYTES, 1)
        return committed.get(h.id, 0) / capacity

    loaded = [h.hostname for h in hosts if (ratio_of(h) or 0) >= _LOADED_MIN_RATIO]

    hits: list[SurplusHit] = []
    for host in hosts:
        ratio = ratio_of(host)
        if ratio is None or ratio > _SURPLUS_MAX_RATIO:
            continue
        host_stopped = sorted(stopped.get(host.id, []))
        host_dimms = sorted(dimms.get(host.id, []))
        spare = host_dimms[:-_MIN_KEEP_DIMMS] if len(host_dimms) > _MIN_KEEP_DIMMS else []
        if not host_stopped and not spare:
            continue

        mem = int((host.capabilities or {}).get("mem_total_bytes", 0))
        hit = SurplusHit(
            hostname=host.hostname,
            ram_gb=mem / _GB,
            ratio=ratio,
            cpu_model=(host.capabilities or {}).get("cpu_model"),
            stopped_vms=host_stopped,
            spare_dimm_gb=[d / _GB for d in spare],
        )
        if host_stopped:
            names = ", ".join(host_stopped)
            hit.options.append(
                f"spin the stopped VM(s) back up ({names}) — the capacity is already provisioned"
            )
        if spare:
            sizes = " + ".join(f"{d / _GB:.0f} GiB" for d in spare)
            target = loaded[0] if loaded else "a more-loaded host"
            hit.options.append(
                f"move spare DIMM(s) ({sizes}) to {target} (see `helper plan rebalance`)"
            )
        hit.options.append(
            "accept: declare the surplus a deliberate capacity reserve — mark the "
            "stopped VMs stopped-by-design (OperationalIntent) so this stops resurfacing"
        )
        hits.append(hit)
    return hits


__all__ = ["SurplusHit", "analyze_surplus"]
