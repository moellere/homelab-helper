"""Placement recommender — "where should this workload run?" (Phase 5, AC2).

Deterministic and explainable, in that order. Hard constraints eliminate hosts
with a stated reason (wrong arch, not enough RAM, GPU required but absent);
survivors are scored on headroom and affinity, and every point of score comes
with a human-readable reason. The Planner agent narrates this output — it
never decides it. (The multi-workload rebalance solver is a later slice; a
single workload against a homelab-sized fleet doesn't need OR-Tools.)

Resource facts come from ``Host.capabilities`` as the reconciler projects them
(``cpu_threads``, ``mem_total_bytes``, ``total_disk_bytes``, ``gpu_count``).
A host missing a fact is not silently eliminated — the gap becomes a caveat on
its candidacy, because "we don't know" and "it doesn't fit" are different
answers. Guest counts approximate current load until per-host utilization
lands with the Phase-2 time-series.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import IntentState
from homelab_helper.db.models import Host, OperationalIntent, VirtualMachine
from homelab_helper.engine.network_path import PathCharacteristics, Topology, load_topology

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.engine.workloads import WorkloadProfile

_MB = 1024 * 1024
_GB = 1024 * 1024 * 1024
_RAM_RESERVE_MB = 1024  # leave the host OS a gigabyte

# Data-gravity affinity: a host whose total disk exceeds this is a plausible
# home for a gravity-bearing workload's dataset.
_BULK_STORAGE_BYTES = 2 * 1024**4  # 2 TiB


@dataclass
class PlacementCandidate:
    hostname: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


@dataclass
class PlacementReport:
    workload: str
    candidates: list[PlacementCandidate] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def best(self) -> PlacementCandidate | None:
        return self.candidates[0] if self.candidates else None

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view for the MCP surface; candidates keep their rank order."""
        return {
            "workload": self.workload,
            "candidates": [
                {
                    "rank": i,
                    "hostname": c.hostname,
                    "score": round(c.score, 2),
                    "reasons": list(c.reasons),
                    "caveats": list(c.caveats),
                }
                for i, c in enumerate(self.candidates, 1)
            ],
            "rejected": [{"hostname": h, "reason": r} for h, r in self.rejected],
        }


def _cap(host: Host, key: str) -> Any:
    return (host.capabilities or {}).get(key)


def _hard_rejection(host: Host, profile: WorkloadProfile) -> str | None:
    """The constraint this host fails, or None if it's a candidate."""
    if host.arch.value != "other" and host.arch.value not in profile.arch:
        return f"arch {host.arch.value} not supported (needs {'/'.join(profile.arch)})"
    mem_total = _cap(host, "mem_total_bytes")
    if mem_total is not None and int(mem_total) / _MB - _RAM_RESERVE_MB < profile.ram_mb:
        return (
            f"insufficient RAM: {int(mem_total) / _GB:.1f} GiB total < "
            f"{profile.ram_mb} MB baseline + OS reserve"
        )
    if profile.gpu == "required" and not _cap(host, "gpu_count"):
        return "workload requires a GPU; none observed on this host"
    return None


def _evaluate(
    host: Host, profile: WorkloadProfile, guest_count: int
) -> PlacementCandidate | tuple[str, str]:
    """One host against one profile: a candidate, or (hostname, rejection)."""
    rejection = _hard_rejection(host, profile)
    if rejection is not None:
        return (host.hostname, rejection)

    mem_total = _cap(host, "mem_total_bytes")
    gpu_count = _cap(host, "gpu_count")
    candidate = PlacementCandidate(hostname=host.hostname)

    if mem_total is None:
        candidate.caveats.append("RAM unknown (host not deep-probed) — verify before placing")
    else:
        headroom_gb = (int(mem_total) / _MB - _RAM_RESERVE_MB - profile.ram_mb) / 1024
        candidate.score += min(headroom_gb, 32.0)
        candidate.reasons.append(f"{headroom_gb:.1f} GiB RAM headroom after baseline")

    threads = _cap(host, "cpu_threads")
    if threads is not None:
        spare = max(float(threads) - profile.cpu_cores, 0.0)
        candidate.score += min(spare, 16.0) * 0.5
        candidate.reasons.append(f"{threads} CPU threads vs {profile.cpu_cores} baseline cores")
    else:
        candidate.caveats.append("CPU unknown (host not deep-probed)")

    if profile.gpu == "optional" and gpu_count:
        candidate.score += 8.0
        purpose = f" ({profile.gpu_purpose})" if profile.gpu_purpose else ""
        candidate.reasons.append(f"GPU present for optional acceleration{purpose}")
    elif profile.gpu == "required" and gpu_count:
        candidate.reasons.append("satisfies the GPU requirement")

    disk_total = _cap(host, "total_disk_bytes")
    if profile.data_gravity and disk_total is not None and int(disk_total) >= _BULK_STORAGE_BYTES:
        candidate.score += 6.0
        candidate.reasons.append(
            f"bulk storage ({int(disk_total) / 1024**4:.1f} TiB) suits "
            f"data gravity {profile.data_gravity!r}"
        )
    elif profile.data_gravity:
        candidate.caveats.append(
            f"data gravity {profile.data_gravity!r}: dataset would live remotely "
            "unless this host carries it"
        )

    candidate.score -= min(guest_count, 10) * 0.5
    if guest_count:
        candidate.reasons.append(f"already runs {guest_count} guest(s)")
    return candidate


def network_verdict(profile: WorkloadProfile, path: PathCharacteristics) -> tuple[str, str | None]:
    """('ok' | 'warn' | 'refuse', message) for one path under one profile.

    This is P5-AC6's rule: a sync-replicated workload (``lan-required``) is
    *refused* across a non-LAN-grade path, and the message explains the
    worst-link inheritance rather than just saying no.
    """
    if path.same_site or path.lan_grade:
        return "ok", None
    worst = path.worst_link
    assert worst is not None  # non-LAN-grade implies at least one link
    detail = (
        f"{path.describe()}; worst link {worst.a}↔{worst.b} is {worst.kind} "
        f"({worst.latency_ms:.0f} ms, {worst.reliability})"
    )
    if profile.network_class == "lan-required":
        return "refuse", (
            f"{profile.name} replicates synchronously — every replica must be "
            f"LAN-grade of its peers, and this path is not: {detail}. A path "
            "inherits latency and reliability from its worst link, so the VPN "
            "hop sets the character of the whole route."
        )
    if profile.network_class == "lan-preferred" or profile.data_gravity:
        return "warn", f"cross-site path is degraded: {detail}"
    return "ok", None


def _gravity_anchor(hosts: list[Host], profile: WorkloadProfile) -> Host | None:
    """The host most plausibly carrying this workload's dataset today."""
    if not profile.data_gravity:
        return None
    carriers = [
        h
        for h in hosts
        if (_cap(h, "total_disk_bytes") or 0)
        and int(_cap(h, "total_disk_bytes")) >= _BULK_STORAGE_BYTES
    ]
    if not carriers:
        return None
    return max(carriers, key=lambda h: int(_cap(h, "total_disk_bytes")))


def _apply_network(
    report: PlacementReport,
    hosts: list[Host],
    profile: WorkloadProfile,
    topology: Topology,
) -> None:
    """Fold path verdicts into an already-scored report (AC6)."""
    anchor = _gravity_anchor(hosts, profile)
    surviving: list[PlacementCandidate] = []
    for candidate in report.candidates:
        if anchor is not None and anchor.hostname != candidate.hostname:
            path = topology.path(candidate.hostname, anchor.hostname)
            if path is None:
                report.rejected.append(
                    (candidate.hostname, f"no network route to {anchor.hostname} in the topology")
                )
                continue
            verdict, message = network_verdict(profile, path)
            if verdict == "refuse":
                report.rejected.append((candidate.hostname, message or "network unsuitable"))
                continue
            if verdict == "warn" and message:
                candidate.score -= 8.0
                candidate.caveats.append(message)
        surviving.append(candidate)
    report.candidates = surviving

    if profile.network_class == "lan-required" and anchor is None:
        sites = {topology.site_of(c.hostname) for c in report.candidates}
        if len(sites) > 1:
            for candidate in report.candidates:
                candidate.caveats.append(
                    "sync-replicated workload: keep every replica within one "
                    f"site (candidates span {len(sites)} sites)"
                )


async def recommend_placement(
    session: AsyncSession,
    profile: WorkloadProfile,
    *,
    topology: Topology | None = None,
) -> PlacementReport:
    """Rank every known host for one workload, with reasons either way.

    ``topology`` defaults to the operator's declared file (env); no topology
    means the single-site assumption and no network filtering.
    """
    report = PlacementReport(workload=profile.name)
    hosts = list((await session.execute(select(Host).order_by(Host.hostname))).scalars().all())
    if topology is None:
        topology = load_topology()

    guests: dict[Any, int] = {}
    for vm in (await session.execute(select(VirtualMachine))).scalars().all():
        if vm.node_host_id is not None and vm.status == "running":
            guests[vm.node_host_id] = guests.get(vm.node_host_id, 0) + 1

    decommissioning = {
        i.target_id
        for i in (await session.execute(select(OperationalIntent))).scalars().all()
        if i.intent == IntentState.DECOMMISSIONING
    }

    for host in hosts:
        if str(host.id) in decommissioning:
            report.rejected.append((host.hostname, "host intent is decommissioning"))
            continue
        outcome = _evaluate(host, profile, guests.get(host.id, 0))
        if isinstance(outcome, PlacementCandidate):
            report.candidates.append(outcome)
        else:
            report.rejected.append(outcome)

    if topology is not None:
        _apply_network(report, hosts, profile, topology)

    report.candidates.sort(key=lambda c: (-c.score, c.hostname))
    return report


__all__ = [
    "PlacementCandidate",
    "PlacementReport",
    "network_verdict",
    "recommend_placement",
]
