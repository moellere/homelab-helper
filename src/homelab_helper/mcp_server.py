"""MCP server — the harness's query surface as Model Context Protocol tools.

Phase 4's first slice. Exposes what the CLI already answers (hosts, findings,
services, audit rollup) plus the management-plane discovery runs as MCP tools
over stdio, so Claude Desktop / Claude Code / Cursor can drive the harness in
natural language without shelling out.

The Phase-6 trust surface (``trust_status``, ``list_receipts``,
``pending_actions``) is **read-only on purpose**: a model may see what policy
allows and what has run, and has no tool to grant, elevate, override, roll
back, or open a window. Authority changes are operator gestures at the CLI.
Phase 7 added one *trigger*: ``execute_proposal`` asks the executor to run a
pending proposal, and the outcome is decided by ``decide()`` plus — at CONFIRM
— a human's tap on an approval channel. The agent never passes an override and
never authorizes anything; a cell at PROPOSE still executes nothing.

**Read-only against infrastructure (L1).** Query tools only read the harness
DB. ``run_discovery`` reads live sources (UniFi, Cloudflare, Argo CD, Proxmox,
K8s, OMV, Home Assistant, MikroTik — credentials from the same ``HOMELAB_HELPER_*`` env
vars the CLI uses) and persists into the harness DB — never a write to the lab
itself. The Phase-5 planners (placement, rebalance, bottlenecks, surplus,
network path) are exposed as deterministic reports; the client's own model
narrates them, so the harness never spends an LLM call on a tool's behalf.

Tools return plain dicts/lists (the SDK ships them as structured content).
Lookup misses return ``{"error": ...}`` rather than raising, so an LLM caller
gets a message it can act on instead of a protocol error.

Run it: ``helper mcp serve`` (stdio). Register in a client, e.g. Claude Code::

    claude mcp add homelab -- uv run --directory /path/to/homelab-helper helper mcp serve
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy import func, or_, select

from homelab_helper import __version__
from homelab_helper.adapters import factories
from homelab_helper.adapters.kernel_ssh import KernelSSHAdapter
from homelab_helper.adapters.proxmox import ProxmoxConfigError
from homelab_helper.adapters.talos import TalosAdapter, TalosError
from homelab_helper.config import PROBE_ALLOW_VAR, database_url, load_env, probe_allow_patterns
from homelab_helper.config import config_status as _config_status
from homelab_helper.db.enums import (
    FindingKind,
    FindingSeverity,
    FindingStatus,
    ProposalOutcome,
    ResolutionScope,
)
from homelab_helper.db.lookup import rows_by_id_prefix
from homelab_helper.db.models import (
    CellTrust,
    Cluster,
    Domain,
    ExecutionReceipt,
    Host,
    MaintenanceRun,
    ProposalLog,
    ReconciliationFinding,
    Service,
    ServiceEndpoint,
    TrustBoundary,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine import diagram as diagram_engine
from homelab_helper.engine.approval import (
    ApprovalConfigError,
    ApprovalError,
    approval_channel_from_env,
)
from homelab_helper.engine.bottlenecks import analyze_bottlenecks as _analyze_bottlenecks
from homelab_helper.engine.bottlenecks import persist_bottlenecks
from homelab_helper.engine.cluster_nodes import PLACEMENT_TARGET_RULE
from homelab_helper.engine.discoverers import run_discovery as _run_discovery
from homelab_helper.engine.escalation import PROMOTION_STREAK, is_promotable
from homelab_helper.engine.executor import (
    ExecutionRefused,
    ManifestError,
    parse_manifest,
)
from homelab_helper.engine.executor import (
    execute_proposal as _run_proposal,
)
from homelab_helper.engine.host_probe import HostProbeRequest, UnknownProbeError
from homelab_helper.engine.host_probe import probe_host as _probe_host
from homelab_helper.engine.jetkvm import (
    attached_from_env,
    capture_console,
    snapshot_dir,
)
from homelab_helper.engine.maintenance import (
    MaintenanceError,
    execute_maintenance,
    plan_maintenance,
    plan_talos_maintenance,
    run_summary,
    talos_health_gate,
)
from homelab_helper.engine.manifest import (
    BLAST_RADII,
    DNS_RECORD_TYPES,
    K8S_BLAST_RADIUS,
    WORKLOAD_KINDS,
    argocd_radius_error,
    build_argocd_artifact,
    build_artifact,
    build_dns_artifact,
    build_k8s_upgrade_artifact,
    build_talos_artifact,
    build_workload_artifact,
)
from homelab_helper.engine.network_path import TOPOLOGY_ENV_VAR, TopologyError, load_topology
from homelab_helper.engine.notify import notifier_from_env
from homelab_helper.engine.placement import network_verdict
from homelab_helper.engine.placement import recommend_placement as _recommend_placement
from homelab_helper.engine.playbooks import PLAYBOOKS, run_playbooks
from homelab_helper.engine.rebalance import plan_rebalance as _plan_rebalance
from homelab_helper.engine.reconfigure import analyze_surplus as _analyze_surplus
from homelab_helper.engine.retire import is_retired, retired_host_ids
from homelab_helper.engine.retire import retire_host as _retire_host
from homelab_helper.engine.rightsizing import rightsizing_report
from homelab_helper.engine.talos_probe import TalosProbeRequest
from homelab_helper.engine.talos_probe import probe_talos as _probe_talos
from homelab_helper.engine.trust import decide, load_trust_context, open_windows
from homelab_helper.engine.usage import usage_overview
from homelab_helper.engine.workloads import WorkloadLibraryError, load_workload_library
from homelab_helper.secrets import redact

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


server = MCPServer(
    "homelab-helper",
    version=__version__,
    instructions=(
        "Inventory, audit, and findings for a homelab. All tools are read-only "
        "against the infrastructure (L1: propose, never apply); run_discovery "
        "reads live sources and persists observations into the harness DB only. "
        "Fingerprints are stable finding identities — use them to reference a "
        "finding across calls."
    ),
)


# An MCP client launches this process with whatever environment it happens to
# have, so the .env is loaded here as well as in the CLI entry point.
load_env()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _finding_dict(f: ReconciliationFinding) -> dict[str, Any]:
    return {
        "fingerprint": f.fingerprint,
        "kind": f.kind.value,
        "severity": f.severity.value,
        "status": f.status.value,
        "title": f.title,
        "description": f.description,
        "affected": f.affected,
        "first_seen": _iso(f.first_seen),
        "last_seen": _iso(f.last_seen),
    }


def _endpoint_dict(ep: ServiceEndpoint) -> dict[str, Any]:
    return {
        "scope": ep.scope.value,
        "resolver": ep.resolver,
        "hostname": ep.hostname,
        "ip": ep.ip,
        "tls_provider": ep.tls_provider,
    }


def _split_brain(endpoints: list[ServiceEndpoint]) -> dict[str, Any] | None:
    internal = sorted({e.ip for e in endpoints if e.scope == ResolutionScope.INTERNAL and e.ip})
    external = sorted({e.ip for e in endpoints if e.scope == ResolutionScope.EXTERNAL and e.ip})
    if internal and external and internal != external:
        return {"internal_ips": internal, "external_ips": external}
    return None


async def _open_findings(session: AsyncSession) -> list[ReconciliationFinding]:
    rows = await session.execute(
        select(ReconciliationFinding).where(
            ReconciliationFinding.status.in_([FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED])
        )
    )
    return list(rows.scalars().all())


# ------------------------------------------------------------------ query tools


@server.tool()
async def list_hosts() -> list[dict[str, Any]]:
    """List every host the harness knows: hostname, IP, arch, discovery source."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            hosts = (await session.execute(select(Host).order_by(Host.hostname))).scalars().all()
            retired = await retired_host_ids(session)
            return [
                {
                    "hostname": h.hostname,
                    "primary_ip": h.primary_ip,
                    "arch": h.arch.value,
                    "discovery_source": h.discovery_source.value,
                    "last_verified": _iso(h.last_verified),
                    "retired": h.id in retired,
                }
                for h in hosts
            ]
    finally:
        await engine.dispose()


@server.tool()
async def get_host(hostname: str) -> dict[str, Any]:
    """Full record for one host: identity, capabilities, guests it runs, service
    endpoints resolving to it, and its open findings."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            host = (
                await session.execute(select(Host).where(Host.hostname == hostname))
            ).scalar_one_or_none()
            if host is None:
                return {"error": f"no host named {hostname!r}"}
            vms = (
                (
                    await session.execute(
                        select(VirtualMachine).where(VirtualMachine.node_host_id == host.id)
                    )
                )
                .scalars()
                .all()
            )
            eps: list[ServiceEndpoint] = []
            if host.primary_ip:
                eps = list(
                    (
                        await session.execute(
                            select(ServiceEndpoint).where(ServiceEndpoint.ip == host.primary_ip)
                        )
                    )
                    .scalars()
                    .all()
                )
            host_id = str(host.id)
            retired = await is_retired(session, host.id)
            findings = [
                _finding_dict(f)
                for f in await _open_findings(session)
                if any(
                    a.get("target_type") == "host" and a.get("target_id") == host_id
                    for a in (f.affected or [])
                )
            ]
            return {
                "hostname": host.hostname,
                "retired": retired,
                "primary_ip": host.primary_ip,
                "arch": host.arch.value,
                "discovery_source": host.discovery_source.value,
                "last_verified": _iso(host.last_verified),
                "capabilities": host.capabilities or {},
                "guests": [
                    {"name": v.name, "kind": v.kind, "vmid": v.vmid, "status": v.status}
                    for v in vms
                ],
                "endpoints_resolving_here": [_endpoint_dict(e) for e in eps],
                "open_findings": findings,
            }
    finally:
        await engine.dispose()


@server.tool()
async def list_findings(
    status: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List findings, most recently seen first, optionally filtered by status
    (open/acknowledged/resolved/suppressed), kind (e.g. stray-config,
    drift-candidate), or severity. Returns at most `limit` (default 200)."""
    try:
        wanted = [
            column == enum(value)
            for column, enum, value in (
                (ReconciliationFinding.status, FindingStatus, status),
                (ReconciliationFinding.kind, FindingKind, kind),
                (ReconciliationFinding.severity, FindingSeverity, severity),
            )
            if value
        ]
    except ValueError:
        return []  # a value no finding can have
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            stmt = (
                select(ReconciliationFinding)
                .where(*wanted)
                .order_by(ReconciliationFinding.last_seen.desc())
                .limit(max(1, limit))
            )
            return [_finding_dict(f) for f in (await session.execute(stmt)).scalars()]
    finally:
        await engine.dispose()


@server.tool()
async def get_finding(fingerprint: str) -> dict[str, Any]:
    """Full detail for one finding by its stable fingerprint."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            f = (
                await session.execute(
                    select(ReconciliationFinding).where(
                        ReconciliationFinding.fingerprint == fingerprint
                    )
                )
            ).scalar_one_or_none()
            if f is None:
                return {"error": f"no finding with fingerprint {fingerprint!r}"}
            d = _finding_dict(f)
            d["evidence_refs"] = f.evidence_refs
            d["proposed_actions"] = f.proposed_actions
            d["notes"] = f.notes
            return d
    finally:
        await engine.dispose()


@server.tool()
async def list_services() -> list[dict[str, Any]]:
    """List services with endpoint counts per scope and a DNS split-brain flag."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            services = (
                (await session.execute(select(Service).order_by(Service.name))).scalars().all()
            )
            out = []
            for svc in services:
                eps = list(
                    (
                        await session.execute(
                            select(ServiceEndpoint).where(ServiceEndpoint.service_id == svc.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                out.append(
                    {
                        "name": svc.name,
                        "internal_endpoints": sum(
                            1 for e in eps if e.scope == ResolutionScope.INTERNAL
                        ),
                        "external_endpoints": sum(
                            1 for e in eps if e.scope == ResolutionScope.EXTERNAL
                        ),
                        "split_brain": _split_brain(eps) is not None,
                    }
                )
            return out
    finally:
        await engine.dispose()


@server.tool()
async def get_service(name: str) -> dict[str, Any]:
    """Synthesized record for one service (by name or endpoint hostname):
    internal/external endpoints, DNS split-brain, and the VM/host carrying it."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            svc = (
                await session.execute(select(Service).where(Service.name == name))
            ).scalar_one_or_none()
            if svc is None:
                ep = (
                    await session.execute(
                        select(ServiceEndpoint).where(ServiceEndpoint.hostname == name.lower())
                    )
                ).scalar_one_or_none()
                if ep is not None:
                    svc = await session.get(Service, ep.service_id)
            if svc is None:
                return {"error": f"no service or endpoint matches {name!r}"}
            eps = list(
                (
                    await session.execute(
                        select(ServiceEndpoint).where(ServiceEndpoint.service_id == svc.id)
                    )
                )
                .scalars()
                .all()
            )
            vm = (
                await session.execute(select(VirtualMachine).where(VirtualMachine.name == svc.name))
            ).scalar_one_or_none()
            hosted = None
            if vm is not None:
                cluster = await session.get(Cluster, vm.cluster_id)
                hosted = {
                    "vm": vm.name,
                    "cluster": cluster.name if cluster else None,
                    "node": vm.node_name,
                    "status": vm.status,
                }
            return {
                "name": svc.name,
                "endpoints": [_endpoint_dict(e) for e in eps],
                "split_brain": _split_brain(eps),
                "hosted": hosted,
            }
    finally:
        await engine.dispose()


@server.tool()
async def audit_summary() -> dict[str, Any]:
    """Rollup of the harness DB: host/cluster/VM/service counts and findings
    grouped by status, kind, and severity."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:

            async def _count(model: Any) -> int:
                return (await session.execute(select(func.count()).select_from(model))).scalar_one()

            findings = (await session.execute(select(ReconciliationFinding))).scalars().all()
            by_status: dict[str, int] = {}
            by_kind: dict[str, int] = {}
            by_severity: dict[str, int] = {}
            for f in findings:
                by_status[f.status.value] = by_status.get(f.status.value, 0) + 1
                by_kind[f.kind.value] = by_kind.get(f.kind.value, 0) + 1
                if f.status in {FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED}:
                    by_severity[f.severity.value] = by_severity.get(f.severity.value, 0) + 1
            return {
                "hosts": await _count(Host),
                "clusters": await _count(Cluster),
                "virtual_machines": await _count(VirtualMachine),
                "services": await _count(Service),
                "findings_by_status": by_status,
                "findings_by_kind": by_kind,
                "open_findings_by_severity": by_severity,
            }
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ discovery


@server.tool()
async def diagram(site: str | None = None, format: str = "mermaid") -> dict[str, Any]:
    """The inventory as a diagram: sites, hosts (with guests and open findings),
    clusters, and the services that resolve to each host. A read over the harness
    DB; nothing is contacted. `format` is "mermaid" (default, paste into docs or a
    page), "html" (a standalone page) or "json" (the graph). `site` keeps one site."""
    if format not in ("mermaid", "html", "json"):
        return {"error": "format must be mermaid, html or json"}
    engine = make_engine(database_url())
    try:
        async with make_sessionmaker(engine)() as session:
            model = await diagram_engine.build_model(session, site=site)
    finally:
        await engine.dispose()
    if format == "json":
        return {"format": format, "graph": diagram_engine.to_dict(model), "stats": model.stats}
    text = (
        diagram_engine.render_html(model)
        if format == "html"
        else diagram_engine.render_mermaid(model)
    )
    return {"format": format, "text": text, "stats": model.stats}


@server.tool()
async def run_discovery(source: str) -> dict[str, Any]:
    """Run a management-plane discovery and persist into the harness DB.
    Source must be one of: unifi, cloudflare, argocd, proxmox, k8s, omv, hass, mikrotik,
    printwarden (print-warden reachability and printer health from its /healthz),
    jetkvm (the KVM's status and its attached host's reachability, with a console
    snapshot as evidence when that host is down),
    versions (Phase 8.1: package lag, mixed versions, OS end of life, HA updates),
    backups (Phase 8.2: uncovered/stale/unverified guests, orphaned backups, capacity),
    usage (Phase 8.3: hourly/daily usage rollups from Proxmox RRD, backfilled, pruned),
    storage, suggestions, capacity (Phase 10.2: filesystem, memory and CPU trends),
    anomalies (Phase 10.3: guests and nodes outside their own usage baseline).
    Reads the live source (credentials from HOMELAB_HELPER_* env vars); never
    writes to the infrastructure itself."""
    return await _run_discovery(source)


@server.tool()
async def console_snapshot() -> dict[str, Any]:
    """Capture one frame of the console behind the configured JetKVM (read-only).

    Saves a PNG under the data directory and returns its path and the host
    the KVM is declared attached to (HOMELAB_HELPER_JETKVM_ATTACHED_TO). Nothing is
    typed or clicked: the KVM is only watched. Needs the optional jetkvm extra; a
    failure is returned as ``{"error": ...}``."""
    try:
        adapter = factories.jetkvm()
    except Exception as exc:
        return {"error": redact(str(exc))}
    attached_to, _ = attached_from_env()
    try:
        path, err = await capture_console(adapter, snapshot_dir(), attached_to or "console")
    finally:
        await adapter.aclose()
    if path is None:
        return {"error": redact(err or "snapshot failed"), "attached_to": attached_to}
    return {"path": path, "attached_to": attached_to}


@server.tool()
async def usage_summary(subject: str | None = None, days: int = 30) -> list[dict[str, Any]]:
    """What hosts and guests actually used over the last ``days`` (Phase 8.3).

    Per subject: CPU p95 and peak (fraction of allocated CPUs), memory p95 and
    peak (bytes) against the allocation, and how many hourly buckets back it.
    ``subject`` filters by node name, guest name, or vmid; omit for everything.
    Read-only; history comes from ``run_discovery("usage")``."""
    engine = make_engine(database_url())
    try:
        async with session_scope(make_sessionmaker(engine)) as session:
            return await usage_overview(session, subject=subject, days=days)
    finally:
        await engine.dispose()


@server.tool()
async def config_status() -> dict[str, Any]:
    """Which discovery sources have credentials, which are missing what, and
    where the harness DB and .env live. Secret values are never returned — only
    whether each variable is set. Check this first when run_discovery reports a
    credentials error."""
    return _config_status()


async def _lookup_by_prefix(
    session: AsyncSession, prefix: str
) -> ReconciliationFinding | dict[str, Any]:
    """One finding whose fingerprint starts with ``prefix``, or an error dict.

    Mirrors the CLI's prefix matching so the same short fingerprint works in
    either surface; ambiguity is reported rather than guessed at.
    """
    if not prefix:
        return {"error": "fingerprint prefix cannot be empty"}
    matches = (
        (
            await session.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.fingerprint.like(f"{prefix}%")
                )
            )
        )
        .scalars()
        .all()
    )
    if not matches:
        return {"error": f"no finding matches fingerprint prefix {prefix!r}"}
    if len(matches) > 1:
        return {
            "error": f"fingerprint prefix {prefix!r} is ambiguous ({len(matches)} matches)",
            "matches": [{"fingerprint": f.fingerprint, "title": f.title} for f in matches[:5]],
        }
    return matches[0]


async def _transition_finding(
    fingerprint: str,
    apply: Any,
) -> dict[str, Any]:
    """Resolve a fingerprint prefix, mutate the finding, return its new state."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            found = await _lookup_by_prefix(session, fingerprint)
            if isinstance(found, dict):
                return found
            apply(found)
            return _finding_dict(found)
    finally:
        await engine.dispose()


@server.tool()
async def ack_finding(
    fingerprint: str, by: str | None = None, notes: str | None = None
) -> dict[str, Any]:
    """Acknowledge a finding (status -> acknowledged): seen, not yet fixed.
    Accepts a full fingerprint or a unique prefix. Harness-DB only — changes
    nothing in the infrastructure."""

    def _apply(f: ReconciliationFinding) -> None:
        f.status = FindingStatus.ACKNOWLEDGED
        f.acknowledged_at = datetime.now(UTC)
        f.acknowledged_by = by
        if notes:
            f.notes = notes

    return await _transition_finding(fingerprint, _apply)


@server.tool()
async def resolve_finding(fingerprint: str, notes: str | None = None) -> dict[str, Any]:
    """Manually mark a finding resolved (status -> resolved). Use when the
    underlying condition is genuinely fixed; the reconciler reopens the same
    fingerprint if it recurs. Accepts a full fingerprint or a unique prefix."""

    def _apply(f: ReconciliationFinding) -> None:
        f.status = FindingStatus.RESOLVED
        f.resolved_at = datetime.now(UTC)
        if notes:
            f.notes = notes

    return await _transition_finding(fingerprint, _apply)


@server.tool()
async def suppress_finding(
    fingerprint: str, until: str | None = None, notes: str | None = None
) -> dict[str, Any]:
    """Suppress a finding from default listings (status -> suppressed). For
    known-and-accepted conditions the reconciler will keep re-detecting, such as
    hardware that forges a WWN. ``until`` is an ISO date; omit to suppress
    indefinitely. Accepts a full fingerprint or a unique prefix."""
    suppressed_until: datetime | None = None
    if until:
        try:
            suppressed_until = datetime.fromisoformat(until).replace(tzinfo=UTC)
        except ValueError:
            return {"error": f"until must be an ISO date (got {until!r})"}

    def _apply(f: ReconciliationFinding) -> None:
        f.status = FindingStatus.SUPPRESSED
        f.suppressed_until = suppressed_until
        if notes:
            f.notes = notes

    return await _transition_finding(fingerprint, _apply)


@server.tool()
async def retire_host(hostname: str, rationale: str | None = None) -> dict[str, Any]:
    """Mark a host decommissioned: records a DECOMMISSIONING intent, closes its
    open part placements explicitly, and resolves its open findings. Harness-DB
    only — nothing touches the host. Idempotent. The planners skip retired
    hosts; `helper host retire` is the same operation from the CLI."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            host = (
                await session.execute(select(Host).where(Host.hostname == hostname))
            ).scalar_one_or_none()
            if host is None:
                return {"error": f"no host named {hostname!r}"}
            result = await _retire_host(session, host, declared_by="agent:mcp", rationale=rationale)
            return result.as_dict()
    finally:
        await engine.dispose()


def _matches_any(value: str | None, patterns: tuple[str, ...]) -> bool:
    return value is not None and any(fnmatchcase(value.lower(), p.lower()) for p in patterns)


def _known_host_refusal(
    hostname: str, primary_ip: str | None, known: Host, patterns: tuple[str, ...]
) -> str | None:
    if primary_ip and known.primary_ip and primary_ip != known.primary_ip:
        return (
            f"{hostname!r} is recorded at {known.primary_ip}; refusing to probe it at "
            f"{primary_ip}. If the host moved, update it from the CLI first."
        )
    if primary_ip and not known.primary_ip and not _matches_any(primary_ip, patterns):
        return (
            f"{hostname!r} has no recorded address and {primary_ip!r} matches no pattern "
            f"in {PROBE_ALLOW_VAR}; omit primary_ip to connect by name, or allow the address."
        )
    return None


def _unknown_host_refusal(
    hostname: str, primary_ip: str | None, patterns: tuple[str, ...]
) -> str | None:
    if not patterns:
        return (
            f"{hostname!r} is not a known host and {PROBE_ALLOW_VAR} is unset: the MCP surface "
            "only probes hosts already in the harness DB. Add it with `helper discover host` "
            f"or `helper onboard`, or set {PROBE_ALLOW_VAR} to comma-separated hostname/IP globs."
        )
    if not _matches_any(hostname, patterns):
        return f"{hostname!r} matches no pattern in {PROBE_ALLOW_VAR}"
    if primary_ip and not _matches_any(primary_ip, patterns):
        return (
            f"{primary_ip!r} matches no pattern in {PROBE_ALLOW_VAR}; an unknown host must "
            "connect by a name or address the allow list covers"
        )
    return None


def probe_target_refusal(
    hostname: str,
    primary_ip: str | None,
    known: Host | None,
    patterns: tuple[str, ...],
) -> str | None:
    """Why an MCP caller may not probe this target, or ``None`` when it may.

    The tool authenticates with the operator's SSH key, so the set of targets
    it can be steered at is the whole attack surface. A known host is always
    probeable, but only at its recorded address — a caller can't aim a trusted
    hostname at some other IP. An unknown host (or a caller-supplied address for
    a host that has none recorded) must match a glob in
    ``HOMELAB_HELPER_MCP_PROBE_ALLOW``; with the variable unset, only known
    hosts probe. Pure, so the policy is unit-testable without a server.
    """
    if known is not None:
        return _known_host_refusal(hostname, primary_ip, known, patterns)
    return _unknown_host_refusal(hostname, primary_ip, patterns)


async def _known_host(session: AsyncSession, hostname: str, primary_ip: str | None) -> Host | None:
    """The row ``resolve_host`` would reuse for this request, if any."""
    conditions = [Host.hostname == hostname]
    if primary_ip:
        conditions.append(Host.primary_ip == primary_ip)
    row: Host | None = (
        (await session.execute(select(Host).where(or_(*conditions)).order_by(Host.created_at)))
        .scalars()
        .first()
    )
    return row


@server.tool()
async def probe_host(
    hostname: str,
    ssh_user: str,
    ssh_key_path: str | None = None,
    primary_ip: str | None = None,
    ssh_port: int = 22,
    probes: list[str] | None = None,
) -> dict[str, Any]:
    """Deep-probe one Linux host over SSH (CPU, memory, DIMMs, storage, NICs,
    PCI, GPU, SMART, services), persist the observations, and reconcile.

    Complements run_discovery, which only reads management planes — this is the
    kernel-level source. Reads the host and writes only to the harness DB.

    Authenticates by key: pass ssh_key_path, or set HOMELAB_HELPER_SSH_KEY.
    Passwords are deliberately not accepted here. A full suite takes ~30s.

    Scoped: a host already in the harness DB is probed at its recorded address
    (a different primary_ip is refused). A host the harness doesn't know is
    refused unless its name — and primary_ip, when given — match a glob in
    HOMELAB_HELPER_MCP_PROBE_ALLOW (e.g. "*.lan,10.0.1.*"). Add new hosts from
    the CLI (`helper discover host`, `helper onboard`) or widen the allow list.
    """
    key_path = ssh_key_path or os.environ.get("HOMELAB_HELPER_SSH_KEY")
    if not key_path:
        return {"error": "an SSH key is required: pass ssh_key_path or set HOMELAB_HELPER_SSH_KEY"}
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            known = await _known_host(session, hostname, primary_ip)
            refusal = probe_target_refusal(hostname, primary_ip, known, probe_allow_patterns())
            if refusal is not None:
                return {"hostname": hostname, "error": refusal}
            result = await _probe_host(
                session,
                HostProbeRequest(
                    name=hostname,
                    ssh_user=ssh_user,
                    ssh_key_path=key_path,
                    primary_ip=primary_ip,
                    ssh_port=ssh_port,
                    probe_names=tuple(probes) if probes else None,
                ),
            )
            return result.as_dict()
    except UnknownProbeError as exc:
        return {"error": f"unknown probe {exc.args[0]!r}"}
    except Exception as exc:
        return {"hostname": hostname, "error": redact(str(exc))}
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Phase 5 planners — deterministic reports; the MCP client's own model narrates
# ---------------------------------------------------------------------------


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


@server.tool()
async def probe_talos(
    hostname: str,
    node: str | None = None,
    talosconfig: str | None = None,
    probes: list[str] | None = None,
) -> dict[str, Any]:
    """Discover a Talos Linux node over its machine API (no SSH) via the
    operator's talosctl credentials, persist the observations, and reconcile.
    ``node`` is the API endpoint when it differs from the host's recorded
    address.

    Scoped like probe_host: a known host is probed at its recorded address (a
    different ``node`` is refused); an unknown host must match a glob in
    HOMELAB_HELPER_MCP_PROBE_ALLOW.
    """
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            known = await _known_host(session, hostname, node)
            refusal = probe_target_refusal(hostname, node, known, probe_allow_patterns())
            if refusal is not None:
                return {"hostname": hostname, "error": refusal}
            result = await _probe_talos(
                session,
                TalosProbeRequest(
                    name=hostname,
                    node=node,
                    talosconfig=talosconfig,
                    probe_names=tuple(probes) if probes else None,
                ),
            )
            return result.as_dict()
    except UnknownProbeError as exc:
        return {"error": f"unknown probe {exc.args[0]!r}"}
    except Exception as exc:
        return {"hostname": hostname, "error": redact(str(exc))}
    finally:
        await engine.dispose()


# --------------------------------------------------------- trust surface (L2)
#
# The trust gradient's whole premise is that an LLM is never in the path that
# authorizes execution, so this surface lets a model *see* the policy — what
# is allowed, what ran, what is pending — and gives it no way to change any
# of it. There is no MCP tool to grant a cell, open a window, override, or
# roll back; those are operator gestures at the CLI. A mechanical test
# enforces the absence. Phase 7's `execute_proposal` is a *trigger*, not an
# authority: it runs the same executor the CLI does, with no override, so
# `decide()` and the approval channel (a human on another device) still
# decide; a second mechanical test pins that it can never pass an override.


@server.tool()
async def trust_status() -> dict[str, Any]:
    """Show the trust gradient: domains, granted cells, boundaries, windows.

    Read-only. Nothing here can change authority — use the CLI for that.
    """
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            domains = (await session.execute(select(Domain))).scalars().all()
            cells = (await session.execute(select(CellTrust))).scalars().all()
            boundaries = (
                (
                    await session.execute(
                        select(TrustBoundary, Host.hostname).join(
                            Host, Host.id == TrustBoundary.host_id, isouter=True
                        )
                    )
                )
                .tuples()
                .all()
            )
            live = await open_windows(session)
            return {
                "domains": [
                    {
                        "name": d.name.value,
                        "default_level": d.default_level.value,
                        "max_level": d.max_level.value,
                        "absolute": d.is_absolute,
                    }
                    for d in domains
                ],
                "cells": [
                    {
                        "cell": f"{c.domain.value}/{c.action_kind}/{c.blast_radius}",
                        "level": c.level.value,
                        "granted_by": c.granted_by,
                        "clean_streak": c.clean_streak,
                        "promotion_streak": PROMOTION_STREAK,
                        "on_probation": c.on_probation,
                        "auto_promotable": is_promotable(c.action_kind, c.blast_radius),
                    }
                    for c in cells
                ],
                "boundaries": [
                    {
                        "hostname": hostname,
                        "max_agent_authority": b.max_agent_authority.value,
                        "absolute": b.absolute,
                    }
                    for b, hostname in boundaries
                ],
                "open_windows": [
                    {
                        "id": str(w.id),
                        "reason": w.reason,
                        "opened_by": w.opened_by,
                        "expires_at": _iso(w.expires_at),
                        "scope": w.scope,
                    }
                    for w in live
                ],
                "note": (
                    "read-only view; grants, windows, overrides and execution "
                    "are operator gestures at the CLI"
                ),
            }
    finally:
        await engine.dispose()


@server.tool()
async def list_receipts(limit: int = 20) -> list[dict[str, Any]]:
    """Recent execution receipts — what actually ran, at what level, and how it ended."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            rows = (
                (
                    await session.execute(
                        select(ExecutionReceipt)
                        .order_by(ExecutionReceipt.executed_at.desc())
                        .limit(max(1, min(limit, 200)))
                    )
                )
                .scalars()
                .all()
            )
            return [
                {
                    "id": str(r.id),
                    "executed_at": _iso(r.executed_at),
                    "actor": r.actor,
                    "decision_level": r.decision_level.value,
                    "decision_reasons": r.decision_reasons,
                    "action": r.action,
                    "outcome": r.outcome,
                    "error": r.error,
                    "duration_ms": r.duration_ms,
                    "window_id": str(r.window_id) if r.window_id else None,
                    "rollback_state": r.rollback_state,
                    "approval": r.approval,
                    "rolled_back_at": _iso(r.rolled_back_at),
                    "rollback_receipt_id": (
                        str(r.rollback_receipt_id) if r.rollback_receipt_id else None
                    ),
                }
                for r in rows
            ]
    finally:
        await engine.dispose()


@server.tool()
async def pending_actions() -> list[dict[str, Any]]:
    """Pending action proposals and what policy would say about each.

    The decision shown is computed **pessimistically** — as if reversibility
    could not be verified — because verifying it means probing the target, and
    a read-only query tool has no business touching infrastructure. A real run
    may therefore land one level higher. Nothing here executes anything.
    """
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            proposals = (
                (
                    await session.execute(
                        select(ProposalLog)
                        .where(ProposalLog.outcome == ProposalOutcome.PENDING)
                        .order_by(ProposalLog.proposed_at)
                    )
                )
                .scalars()
                .all()
            )
            out: list[dict[str, Any]] = []
            for proposal in proposals:
                if (proposal.artifact or {}).get("kind") != "action":
                    continue
                entry: dict[str, Any] = {
                    "id": str(proposal.id),
                    "title": proposal.title,
                    "proposed_by": proposal.proposed_by,
                    "blast_radius": proposal.blast_radius,
                }
                try:
                    manifest = parse_manifest(proposal)
                except ManifestError as exc:
                    entry["error"] = str(exc)
                    out.append(entry)
                    continue
                action = manifest.request(proposal.proposed_by)
                decision = decide(action, await load_trust_context(session, action))
                entry.update(
                    {
                        "cell": manifest.cell_key,
                        "target": manifest.target_label,
                        "decision_if_run_now": decision.level.value,
                        "decision_reasons": list(decision.reasons),
                        "decision_basis": "pessimistic: rollback treated as unverified",
                    }
                )
                out.append(entry)
            return out
    finally:
        await engine.dispose()


# ------------------------------------------------------ proposals (agent-side)
#
# An agent may *draft* an action; it may never authorize one. propose_action
# writes a PENDING ProposalLog row and reports what the gradient would say
# about it — the operator then runs `helper exec run <id>` (or rejects it).
# Nothing here dispatches, grants, or lifts a floor.


def _proposal_dict(p: ProposalLog) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "proposed_at": _iso(p.proposed_at),
        "proposed_by": p.proposed_by,
        "title": p.title,
        "description": p.description,
        "kind": (p.artifact or {}).get("kind"),
        "blast_radius": p.blast_radius,
        "affected": list(p.affected or []),
        "outcome": p.outcome.value,
        "outcome_at": _iso(p.outcome_at),
        "outcome_by": p.outcome_by,
        "outcome_notes": p.outcome_notes,
    }


async def _pessimistic_preview(session: AsyncSession, proposal: ProposalLog) -> dict[str, Any]:
    manifest = parse_manifest(proposal)
    action = manifest.request(proposal.proposed_by)
    decision = decide(action, await load_trust_context(session, action))
    return {
        "cell": manifest.cell_key,
        "target": manifest.target_label,
        "decision_if_run_now": decision.level.value,
        "decision_reasons": list(decision.reasons),
        "decision_basis": "pessimistic: rollback treated as unverified",
    }


@server.tool()
async def propose_action(
    action_kind: str,
    node: str,
    vmid: int,
    vm_kind: str,
    title: str,
    description: str | None = None,
    blast_radius: str = "single-host",
    hostnames: list[str] | None = None,
    target_node: str | None = None,
    online: bool = True,
    cpu_type: str | None = None,
    cores: int | None = None,
    memory_mib: int | None = None,
) -> dict[str, Any]:
    """Draft a Proxmox guest action — start/stop/shutdown/restart, migrate
    (give `target_node`; `online=False` for an offline move), cpu-type
    (give `cpu_type`, e.g. "x86-64-v3"; QEMU only, applied at the guest's
    next stop/start), or resize (give `cores` and/or `memory_mib`; a QEMU
    guest without hotplug applies it at its next stop/start, a container
    live) of a VM or container — as a PENDING proposal. Validates the manifest, writes only to
    the harness DB, and returns what policy would decide right now. Never
    executes on its own: follow with `execute_proposal` (policy + the
    operator's tap decide) or leave it for `helper exec`. An agent cannot
    grant, override, or open a window."""
    if blast_radius not in BLAST_RADII:
        return {"error": f"blast_radius must be one of {', '.join(BLAST_RADII)}"}
    try:
        artifact = build_artifact(
            action_kind=action_kind,
            node=node,
            vmid=vmid,
            vm_kind=vm_kind,
            hostnames=tuple(hostnames) if hostnames else None,
            target_node=target_node,
            online=online,
            cpu_type=cpu_type,
            cores=cores,
            memory_mib=memory_mib,
        )
    except ManifestError as exc:
        return {"error": str(exc)}
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            proposal = ProposalLog(
                title=title.strip()[:512],
                description=description,
                artifact=artifact,
                affected=[
                    {"target_type": "host", "target_id": h} for h in artifact["action"]["hostnames"]
                ],
                blast_radius=blast_radius,
                proposed_by="agent:mcp",
            )
            session.add(proposal)
            await session.flush()
            preview = await _pessimistic_preview(session, proposal)
            return {
                **_proposal_dict(proposal),
                **preview,
                "next": f"`execute_proposal({proposal.id})`, or an operator runs "
                f"`helper exec run {proposal.id}` / `helper exec reject {proposal.id}`",
            }
    finally:
        await engine.dispose()


@server.tool()
async def propose_workload_action(
    action_kind: str,
    namespace: str,
    kind: str,
    name: str,
    title: str,
    replicas: int | None = None,
    description: str | None = None,
    blast_radius: str = "single-service",
) -> dict[str, Any]:
    """Draft a Kubernetes workload action — `workload-restart` (rollout
    restart) or `workload-scale` (give `replicas`) of a deployment,
    statefulset or daemonset — as a PENDING proposal in the `containers`
    domain. Same contract as `propose_action`: validates, writes only to the
    harness DB, never executes on its own."""
    if blast_radius not in BLAST_RADII:
        return {"error": f"blast_radius must be one of {', '.join(BLAST_RADII)}"}
    if kind not in WORKLOAD_KINDS:
        return {"error": f"kind must be one of {', '.join(WORKLOAD_KINDS)}"}
    try:
        artifact = build_workload_artifact(
            action_kind=action_kind,
            namespace=namespace,
            kind=kind,
            name=name,
            replicas=replicas,
        )
    except ManifestError as exc:
        return {"error": str(exc)}
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            proposal = ProposalLog(
                title=title.strip()[:512],
                description=description,
                artifact=artifact,
                affected=[{"target_type": "workload", "target_id": f"{namespace}/{kind}/{name}"}],
                blast_radius=blast_radius,
                proposed_by="agent:mcp",
            )
            session.add(proposal)
            await session.flush()
            preview = await _pessimistic_preview(session, proposal)
            return {
                **_proposal_dict(proposal),
                **preview,
                "next": f"`execute_proposal({proposal.id})`, or an operator runs "
                f"`helper exec run {proposal.id}` / `helper exec reject {proposal.id}`",
            }
    finally:
        await engine.dispose()


async def _persist_proposal(
    artifact: dict[str, Any],
    *,
    title: str,
    description: str | None,
    blast_radius: str,
    affected: list[dict[str, str]],
) -> dict[str, Any]:
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            proposal = ProposalLog(
                title=title.strip()[:512],
                description=description,
                artifact=artifact,
                affected=affected,
                blast_radius=blast_radius,
                proposed_by="agent:mcp",
            )
            session.add(proposal)
            await session.flush()
            preview = await _pessimistic_preview(session, proposal)
            return {
                **_proposal_dict(proposal),
                **preview,
                "next": f"`execute_proposal({proposal.id})`, or an operator runs "
                f"`helper exec run {proposal.id}` / `helper exec reject {proposal.id}`",
            }
    finally:
        await engine.dispose()


@server.tool()
async def propose_argocd_sync(
    application: str,
    title: str,
    revision: str | None = None,
    prune: bool = False,
    description: str | None = None,
    blast_radius: str = "single-service",
) -> dict[str, Any]:
    """Draft an Argo CD sync of one application (optionally pinned to a git
    `revision`, optionally pruning) as a PENDING proposal in the `containers`
    domain. A sync with `revision` or `prune` must be `blast_radius="cluster"`:
    it gets its own cell, never the plain resync's. Rollback is Argo CD's own
    history (the current deployed entry).
    Same contract as `propose_action`: validates, writes only to the harness
    DB, never executes on its own."""
    if blast_radius not in BLAST_RADII:
        return {"error": f"blast_radius must be one of {', '.join(BLAST_RADII)}"}
    refusal = argocd_radius_error(revision=revision, prune=prune, blast_radius=blast_radius)
    if refusal:
        return {"error": refusal}
    try:
        artifact = build_argocd_artifact(application=application, revision=revision, prune=prune)
    except ManifestError as exc:
        return {"error": str(exc)}
    return await _persist_proposal(
        artifact,
        title=title,
        description=description,
        blast_radius=blast_radius,
        affected=[{"target_type": "argocd-app", "target_id": application}],
    )


@server.tool()
async def propose_dns_record(
    hostname: str,
    value: str,
    title: str,
    record_type: str = "A",
    ttl: int = 0,
    controller: str | None = None,
    description: str | None = None,
    blast_radius: str = "single-service",
) -> dict[str, Any]:
    """Draft a static DNS upsert (create or update one `hostname` -> `value`
    record of `record_type` on a UniFi controller; `controller` names one of
    HOMELAB_HELPER_UNIFI_CONTROLLERS, default the single configured one) as a
    PENDING proposal in the `dns` domain. Rollback restores the prior record
    or deletes the created one. Never executes on its own."""
    if blast_radius not in BLAST_RADII:
        return {"error": f"blast_radius must be one of {', '.join(BLAST_RADII)}"}
    if record_type not in DNS_RECORD_TYPES:
        return {"error": f"record_type must be one of {', '.join(DNS_RECORD_TYPES)}"}
    try:
        artifact = build_dns_artifact(
            hostname=hostname, value=value, record_type=record_type, ttl=ttl, controller=controller
        )
    except ManifestError as exc:
        return {"error": str(exc)}
    return await _persist_proposal(
        artifact,
        title=title,
        description=description,
        blast_radius=blast_radius,
        affected=[{"target_type": "dns-record", "target_id": f"{record_type} {hostname}"}],
    )


@server.tool()
async def propose_talos_upgrade(
    node: str,
    version: str,
    title: str,
    hostname: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Draft a Talos upgrade of one node (`node` as `talosctl -n` knows it,
    `version` a release tag like "v1.14.2"; `hostname` names the inventory host
    when it differs) as a PENDING proposal in the `host-os` domain, blast
    radius single-host. The installer image is derived from the node's own
    `machine.install.image` at execution time — a proposal names a version,
    never an image. Rollback is `talosctl rollback` (the previous install stays
    in the other boot partition), verified against the live node before each
    run. Never executes on its own."""
    try:
        artifact = build_talos_artifact(
            node=node, version=version, hostnames=(hostname,) if hostname else None
        )
    except ManifestError as exc:
        return {"error": str(exc)}
    return await _persist_proposal(
        artifact,
        title=title,
        description=description,
        blast_radius="single-host",
        affected=[{"target_type": "host", "target_id": hostname or node}],
    )


@server.tool()
async def propose_k8s_upgrade(
    control_plane: str,
    version: str,
    title: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Draft a Kubernetes upgrade (`talosctl upgrade-k8s --to <version>` run
    from `control_plane`, a control-plane node as `talosctl -n` knows it;
    `version` like "1.37.2") as a PENDING proposal in the `containers` domain
    at blast radius `cluster`. Preflight at run time: cluster healthy, every
    member on one Talos release, target newer and at most one minor ahead. A
    Kubernetes upgrade has no inverse, so the cell can never reach AUTONOMOUS.
    Never executes on its own."""
    try:
        artifact = build_k8s_upgrade_artifact(control_plane=control_plane, version=version)
    except ManifestError as exc:
        return {"error": str(exc)}
    return await _persist_proposal(
        artifact,
        title=title,
        description=description,
        blast_radius=K8S_BLAST_RADIUS,
        affected=[{"target_type": "cluster", "target_id": control_plane}],
    )


_RUN_ASK_STEPS = 4
"""How many step names the one phone prompt for a maintenance run lists."""


async def _one_run(session: AsyncSession, ref: str) -> tuple[MaintenanceRun | None, str]:
    """A maintenance run by id or unique id prefix, or the reason there is none."""
    hits = await rows_by_id_prefix(session, MaintenanceRun, ref)
    if len(hits) == 1:
        return hits[0], ""
    if hits:
        return None, f"{ref!r} matches more than one maintenance run; give more of the id"
    return None, f"no maintenance run {ref!r}"


@server.tool()
async def plan_maintenance_run(
    node: str, target_node: str | None = None, resume_from: str | None = None
) -> dict[str, Any]:
    """Draft a maintenance run for one Proxmox node (Phase 10.4): one proposal per
    step — migrate (or shut down, for node-local disks) every running guest off it,
    apt dist-upgrade, reboot and wait for its return, then bring the guests back.
    `resume_from` names a stopped run on the same node whose drained guests this
    run brings back too. Harness-DB write only; nothing executes. Returns the run
    with its steps."""
    engine = make_engine(database_url())
    try:
        adapter = factories.proxmox()
    except ProxmoxConfigError as exc:
        return {"error": f"Proxmox adapter: {exc}"}
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            earlier = None
            if resume_from:
                earlier, why = await _one_run(session, resume_from)
                if earlier is None:
                    return {"error": why}
            try:
                run = await plan_maintenance(
                    session,
                    adapter,
                    node,
                    actor="agent:mcp",
                    target_node=target_node,
                    resume_from=earlier,
                )
            except MaintenanceError as exc:
                return {"error": str(exc)}
            return run_summary(run)
    finally:
        await adapter.aclose()
        await engine.dispose()


@server.tool()
async def plan_talos_maintenance_run(
    control_plane: str,
    version: str,
    kubernetes: str | None = None,
    talosconfig: str | None = None,
) -> dict[str, Any]:
    """Draft a Talos cluster upgrade as a maintenance run (Phase 11.3): one
    `talos-upgrade` per member read through `control_plane` — control planes
    first, then workers, members already on `version` (a tag like "v1.14.2")
    skipped — and, with `kubernetes` (like "1.37.2"), a closing `k8s-upgrade`.
    Between steps the run requires `talosctl health` to pass. Harness-DB write
    only; nothing executes."""
    engine = make_engine(database_url())
    talos = TalosAdapter(talosconfig=talosconfig)
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            try:
                run = await plan_talos_maintenance(
                    session, talos, control_plane, version, actor="agent:mcp", kubernetes=kubernetes
                )
            except (MaintenanceError, TalosError) as exc:
                return {"error": str(exc)}
            return run_summary(run)
    finally:
        await engine.dispose()


@server.tool()
async def list_maintenance_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Maintenance runs, newest first, each with its steps and their states."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            rows = (
                await session.execute(
                    select(MaintenanceRun).order_by(MaintenanceRun.created_at.desc()).limit(limit)
                )
            ).scalars()
            return [run_summary(r) for r in rows]
    finally:
        await engine.dispose()


@server.tool()
async def execute_maintenance_run(run_id: str) -> dict[str, Any]:
    """Run a planned maintenance run step by step through the executor (Phase 10.4).
    A trigger, not an authority: each step passes `decide()` on its own cell; the
    operator is asked ONCE on the phone for the whole run and that answer is
    relayed to every step at CONFIRM. The run stops at the first failed or refused
    step. node-reboot has no inverse, so a run never executes unattended."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            found, why = await _one_run(session, run_id)
            if found is None:
                return {"error": why}
            run: MaintenanceRun = found
            pending = [st for st in run.steps if st["state"] == "planned"]
            if not pending:
                return {"error": "nothing left to run", **run_summary(run)}
            try:
                channel = approval_channel_from_env()
            except ApprovalConfigError:
                channel = None
            answers: dict[str, Any] = {}

            async def _confirm(m: Any, decision: Any) -> Any:
                if channel is None:
                    raise ExecutionRefused(
                        "policy says CONFIRM and no approval channel is configured "
                        "(HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE) — an operator runs "
                        f"`helper maintenance run {str(run.id)[:8]}`",
                        decision,
                    )
                if "run" not in answers:
                    answers["run"] = await channel.request(
                        m,
                        decision,
                        proposal_id=str(run.id),
                        title=f"Maintenance {run.node}: {len(pending)} step(s)",
                        why=(
                            f"Run all {len(pending)} step(s) on {run.node} in order — "
                            + "; ".join(st["name"] for st in pending[:_RUN_ASK_STEPS])
                            + (" …" if len(pending) > _RUN_ASK_STEPS else "")
                            + ". Stops at the first failure"
                        ),
                    )
                return answers["run"]

            try:
                adapter = factories.proxmox()
            except ProxmoxConfigError as exc:
                return {"error": f"Proxmox adapter: {exc}"}
            k8s = factories.k8s() if shutil.which("kubectl") else None
            talos = TalosAdapter() if run.plane == "talos" else None
            try:
                run = await execute_maintenance(
                    session,
                    run,
                    adapter,
                    actor="agent:mcp",
                    confirm_cb=_confirm,
                    ssh_adapter=KernelSSHAdapter(),
                    k8s_adapter=k8s,
                    talos_adapter=talos,
                    health_cb=talos_health_gate(talos, run.node) if talos else None,
                    notifier=notifier_from_env(),
                )
            except MaintenanceError as exc:
                return {"error": str(exc)}
            except ApprovalError as exc:
                return {"refused": f"approval channel failed: {exc}"}
            finally:
                await adapter.aclose()
            return run_summary(run)
    finally:
        await engine.dispose()


@server.tool()
async def execute_proposal(proposal_id: str) -> dict[str, Any]:
    """Ask the executor to run one PENDING action proposal. This is a trigger,
    not an authority: the same deterministic `decide()` gate the CLI uses
    runs first. AUTONOMOUS executes; CONFIRM sends the operator an approval
    notification (Approve / Deny on their phone) and waits for the tap;
    PROPOSE and BLOCK execute nothing and return the policy reason plus the
    `helper exec run` command. No override is ever passed from here. Returns
    the receipt id and outcome, or `{"refused": ...}`."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            matches = await rows_by_id_prefix(
                session,
                ProposalLog,
                proposal_id,
                ProposalLog.outcome == ProposalOutcome.PENDING,
            )
            if len(matches) != 1:
                return {
                    "error": (
                        f"no pending proposal with id {proposal_id!r}"
                        if not matches
                        else f"id prefix {proposal_id!r} matches more than one pending proposal"
                    )
                }
            proposal = matches[0]
            try:
                manifest = parse_manifest(proposal)
            except ManifestError as exc:
                return {"error": f"invalid manifest: {exc}"}

            channel_note: str | None = None
            try:
                channel = approval_channel_from_env()
            except ApprovalConfigError as exc:
                channel = None
                channel_note = str(exc)

            async def _confirm(m: Any, decision: Any) -> Any:
                if channel is None:
                    raise ExecutionRefused(
                        "policy says CONFIRM and no approval channel is configured "
                        f"({channel_note}); an operator runs `helper exec run {proposal.id}`",
                        decision,
                    )
                return await channel.request(m, decision, proposal_id=str(proposal.id))

            adapters, problem = factories.for_action(manifest)
            if problem is not None:
                return {"error": problem}
            assert adapters is not None
            adapter, k8s, argocd, unifi, ssh, talos = adapters
            try:
                result = await _run_proposal(
                    session,
                    proposal,
                    adapter,
                    actor="agent:mcp",
                    confirm_cb=_confirm,
                    override=None,
                    k8s_adapter=k8s,
                    argocd_adapter=argocd,
                    unifi_adapter=unifi,
                    ssh_adapter=ssh,
                    talos_adapter=talos,
                    notifier=notifier_from_env(),
                )
            except ExecutionRefused as exc:
                return {
                    "refused": str(exc),
                    "decision": exc.decision.level.value if exc.decision else None,
                    "reasons": list(exc.decision.reasons) if exc.decision else [],
                    "cell": manifest.cell_key,
                    "next": f"an operator runs `helper exec run {proposal.id}`",
                }
            except ApprovalError as exc:
                return {"refused": f"approval channel failed: {exc}", "cell": manifest.cell_key}
            finally:
                await adapter.aclose()
                if argocd is not None:
                    await argocd.aclose()
                if unifi is not None:
                    await unifi.aclose()
            return {
                "proposal_id": str(proposal.id),
                "cell": manifest.cell_key,
                "target": manifest.target_label,
                "decision": result.decision.level.value,
                "reasons": list(result.decision.reasons),
                "outcome": result.outcome,
                "error": result.error,
                "receipt_id": str(result.receipt_id),
                "duration_ms": result.duration_ms,
                "notification": result.notification,
                "escalation": (
                    None
                    if result.escalation is None
                    else {
                        "event": result.escalation.event,
                        "level": result.escalation.level.value,
                        "clean_streak": result.escalation.clean_streak,
                    }
                ),
            }
    finally:
        await engine.dispose()


@server.tool()
async def draft_remediations() -> dict[str, Any]:
    """Run the remediation playbooks once: every OPEN finding a playbook covers
    (Argo CD drift → argocd-sync, unhealthy workload → workload-restart) gets a
    PENDING proposal, unless it is younger than 15 minutes (platform self-heal
    gets first go), one is already pending, or one was decided within the
    cooldown; a pending draft whose finding resolved is withdrawn.
    Deterministic — the finding's own fields pick the action. Nothing
    executes; follow with `execute_proposal` or `helper daemon run --ask`."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            result = await run_playbooks(session)
            return {
                "drafted": result.drafted,
                "skipped_pending": len(result.skipped_live),
                "skipped_cooldown": len(result.skipped_cooldown),
                "skipped_young": len(result.skipped_young),
                "withdrawn": result.withdrawn,
                "skipped_done": len(result.skipped_done),
                "findings_without_playbook": result.no_playbook,
                "playbooks": [f"{pb.name}: {pb.description}" for pb in PLAYBOOKS],
            }
    finally:
        await engine.dispose()


@server.tool()
async def list_proposals(
    outcome: str | None = None, limit: int = 50
) -> list[dict[str, Any]] | dict[str, Any]:
    """Proposals in the harness DB, newest first. ``outcome`` filters by
    pending / user-accepted / user-rejected / user-deferred / superseded /
    expired; omit for all."""
    wanted: ProposalOutcome | None = None
    if outcome is not None:
        try:
            wanted = ProposalOutcome(outcome)
        except ValueError:
            return {
                "error": f"unknown outcome {outcome!r}; expected one of {[o.value for o in ProposalOutcome]}"
            }
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            stmt = (
                select(ProposalLog)
                .order_by(ProposalLog.proposed_at.desc())
                .limit(max(1, min(limit, 500)))
            )
            if wanted is not None:
                stmt = stmt.where(ProposalLog.outcome == wanted)
            rows = (await session.execute(stmt)).scalars().all()
            return [_proposal_dict(p) for p in rows]
    finally:
        await engine.dispose()


@server.tool()
async def get_proposal(proposal_id: str) -> dict[str, Any]:
    """One proposal by id (or a unique id prefix), with its artifact and, for
    an action, the pessimistic policy preview."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            matches = await rows_by_id_prefix(session, ProposalLog, proposal_id)
            if not matches:
                return {"error": f"no proposal with id {proposal_id!r}"}
            if len(matches) > 1:
                return {"error": f"prefix {proposal_id!r} is ambiguous (more than one match)"}
            proposal = matches[0]
            out = _proposal_dict(proposal)
            out["artifact"] = proposal.artifact
            if (proposal.artifact or {}).get("kind") == "action":
                try:
                    out.update(await _pessimistic_preview(session, proposal))
                except ManifestError as exc:
                    out["error"] = str(exc)
            return out
    finally:
        await engine.dispose()


def main() -> None:
    """Run the MCP server over stdio (blocking)."""
    server.run(transport="stdio")


__all__ = ["main", "probe_target_refusal", "server"]
