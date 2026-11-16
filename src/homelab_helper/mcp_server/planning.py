"""Phase-5 planners as deterministic reports: placement, rebalance, rightsizing, bottlenecks, surplus, network path."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homelab_helper.config import database_url
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.bottlenecks import analyze_bottlenecks as _analyze_bottlenecks
from homelab_helper.engine.bottlenecks import persist_bottlenecks
from homelab_helper.engine.cluster_nodes import PLACEMENT_TARGET_RULE
from homelab_helper.engine.network_path import TOPOLOGY_ENV_VAR, TopologyError, load_topology
from homelab_helper.engine.placement import network_verdict
from homelab_helper.engine.placement import recommend_placement as _recommend_placement
from homelab_helper.engine.rebalance import plan_rebalance as _plan_rebalance
from homelab_helper.engine.reconfigure import analyze_surplus as _analyze_surplus
from homelab_helper.engine.rightsizing import rightsizing_report
from homelab_helper.engine.workloads import WorkloadLibraryError, load_workload_library
from homelab_helper.mcp_server._app import (
    server,
)


def _workload_library() -> dict[str, Any] | Any:
    """The merged library, or an error dict when a library file is malformed."""
    try:
        return load_workload_library()
    except (WorkloadLibraryError, OSError) as exc:
        return {"error": f"workload library error: {exc}"}


@server.tool()
async def list_workloads(category: str | None = None) -> list[dict[str, Any]] | dict[str, Any]:
    """The workload profile library (baseline CPU/RAM/storage, arch, GPU need,
    data gravity, network class) — the inputs recommend_placement ranks against.
    Optionally filter by category."""
    library = _workload_library()
    if isinstance(library, dict) and "error" in library:
        return library
    return [
        p.as_dict()
        for p in sorted(library.values(), key=lambda p: (p.category, p.name))
        if category is None or p.category == category
    ]


@server.tool()
async def recommend_placement(workload: str) -> dict[str, Any]:
    """Where should this workload run? Ranks every known host for one library
    profile with a reason per point of score, rejections with their failed
    constraint, and caveats where a fact is unknown. Deterministic; nothing is
    changed. Use list_workloads for valid names."""
    library = _workload_library()
    if isinstance(library, dict) and "error" in library:
        return library
    profile = library.get(workload)
    if profile is None:
        from difflib import get_close_matches  # noqa: PLC0415 — only on the miss path

        close = get_close_matches(workload.lower(), list(library), n=5, cutoff=0.6)
        return {
            "error": f"no workload named {workload!r} in the library",
            "did_you_mean": close,
        }
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            report = await _recommend_placement(session, profile)
        return {"profile": profile.as_dict(), **report.as_dict()}
    except (TopologyError, OSError) as exc:
        return {"error": f"topology error: {exc}"}
    finally:
        await engine.dispose()


@server.tool()
async def plan_rebalance(basis: str = "allocated") -> dict[str, Any]:
    """Fleet memory load per host plus up to three candidate rebalancing plans
    across cost classes (VM migrations only; one DIMM move; one DIMM purchase),
    each with steps, tradeoffs, and resulting load. ``basis="usage"`` loads guests
    at their observed 30-day memory p95 instead of their allocation. Proposals
    only — the operator migrates, moves, or buys by hand."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            report = await _plan_rebalance(session, basis=basis)
        return report.as_dict()
    except (TopologyError, OSError) as exc:
        return {"error": f"topology error: {exc}"}
    except ValueError as exc:
        return {"error": str(exc)}
    finally:
        await engine.dispose()


@server.tool()
async def rightsizing(days: int = 30, persist: bool = False) -> dict[str, Any]:
    """Cores and memory recommendations from usage history (Phase 8.4).

    Each names the allocation, the observed p95 and peak, the window and the
    proposed value. Guests with under 7 days of hourly history are skipped and
    listed. VM memory is only ever shrunk (its reported figure includes page
    cache). ``persist`` records them as ``rightsizing`` findings. Nothing changes
    on any guest."""
    engine = make_engine(database_url())
    try:
        async with session_scope(make_sessionmaker(engine)) as session:
            return await rightsizing_report(session, days=days, persist=persist)
    finally:
        await engine.dispose()


@server.tool()
async def analyze_bottlenecks(persist: bool = False) -> dict[str, Any]:
    """Detect known bottleneck patterns over the reconciled fleet (cluster link
    asymmetry, memory pressure, single-uplink bulk storage) with candidate
    mitigations built from the detected facts. ``persist`` records hits as
    findings in the harness DB (reopen/resolve lifecycle); nothing touches the
    lab."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            hits = await _analyze_bottlenecks(session)
        out: dict[str, Any] = {"hits": [h.as_dict() for h in hits]}
        if persist:
            async with session_scope(sm) as session:
                result = await persist_bottlenecks(session, hits, when=datetime.now(UTC))
            out["findings"] = {
                "opened": list(result.opened),
                "reopened": list(result.reopened),
                "updated": list(result.updated),
                "resolved": list(result.resolved),
            }
        return out
    finally:
        await engine.dispose()


@server.tool()
async def analyze_surplus() -> dict[str, Any]:
    """Cluster nodes with capacity to spare and something reconfigurable about
    it (stopped VMs, spare DIMMs), each with the honest options: use it, move
    it, or declare the reserve deliberate. Hosts that are not placement targets
    (a NAS, a Pi) are listed under ``not_placement_targets`` with the reason."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            report = await _analyze_surplus(session)
        return {**report.as_dict(), "rule": PLACEMENT_TARGET_RULE}
    finally:
        await engine.dispose()


@server.tool()
async def network_path(host_a: str, host_b: str, workload: str | None = None) -> dict[str, Any]:
    """The network path between two hosts from the declared topology
    (HOMELAB_HELPER_NETWORK_TOPOLOGY) and what a workload inherits from its
    worst link. With ``workload``, adds an ok/warn/refuse verdict for that
    profile's network class. No topology declared means every host is assumed
    on one LAN."""
    try:
        topology = load_topology()
    except (TopologyError, OSError) as exc:
        return {"error": f"topology error: {exc}"}
    out: dict[str, Any] = {"host_a": host_a, "host_b": host_b}
    if topology is None:
        out["topology_declared"] = False
        out["note"] = (
            "no topology declared — all hosts assumed on one LAN; "
            f"set {TOPOLOGY_ENV_VAR} to a topology file"
        )
        return out
    path = topology.path(host_a, host_b)
    out["topology_declared"] = True
    if path is None:
        out["error"] = f"no route between {host_a} and {host_b} in the topology"
        return out
    out["path"] = path.as_dict()
    if workload is not None:
        library = _workload_library()
        if isinstance(library, dict) and "error" in library:
            return library
        profile = library.get(workload)
        if profile is None:
            out["error"] = f"no workload named {workload!r} in the library"
            return out
        verdict, message = network_verdict(profile, path)
        out["verdict"] = {"workload": workload, "level": verdict, "message": message}
    return out
