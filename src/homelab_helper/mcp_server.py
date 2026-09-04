"""MCP server — the harness's query surface as Model Context Protocol tools.

Phase 4's first slice. Exposes what the CLI already answers (hosts, findings,
services, audit rollup) plus the management-plane discovery runs as MCP tools
over stdio, so Claude Desktop / Claude Code / Cursor can drive the harness in
natural language without shelling out.

The Phase-6 trust surface (``trust_status``, ``list_receipts``,
``pending_actions``) is **read-only on purpose**: a model may see what policy
allows and what has run, and has no tool to grant, elevate, override, or
execute. Authority changes are operator gestures at the CLI.

**Read-only against infrastructure (L1).** Query tools only read the harness
DB. ``run_discovery`` reads live sources (UniFi, Cloudflare, Argo CD, Proxmox,
K8s, OMV, Home Assistant — credentials from the same ``HOMELAB_HELPER_*`` env
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
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy import func, or_, select

from homelab_helper.adapters.argocd import ArgoCDAdapter
from homelab_helper.adapters.cloudflare import CloudflareAdapter
from homelab_helper.adapters.homeassistant import HomeAssistantAdapter
from homelab_helper.adapters.kubernetes import K8sAdapter
from homelab_helper.adapters.openmediavault import OpenMediaVaultAdapter
from homelab_helper.adapters.proxmox import ProxmoxAdapter
from homelab_helper.adapters.unifi import UniFiAdapter, UniFiConfig
from homelab_helper.config import PROBE_ALLOW_VAR, database_url, load_env, probe_allow_patterns
from homelab_helper.config import config_status as _config_status
from homelab_helper.db.enums import FindingStatus, ProposalOutcome, ResolutionScope
from homelab_helper.db.models import (
    CellTrust,
    Cluster,
    Domain,
    ExecutionReceipt,
    Host,
    ProposalLog,
    ReconciliationFinding,
    Service,
    ServiceEndpoint,
    TrustBoundary,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.argocd_drift import reconcile_argocd_drift
from homelab_helper.engine.bottlenecks import analyze_bottlenecks as _analyze_bottlenecks
from homelab_helper.engine.bottlenecks import persist_bottlenecks
from homelab_helper.engine.dns_reconcile import (
    reconcile_external_endpoints,
    reconcile_internal_endpoints,
)
from homelab_helper.engine.escalation import PROMOTION_STREAK, is_promotable
from homelab_helper.engine.executor import ManifestError, parse_manifest
from homelab_helper.engine.hass_import import import_home_assistant
from homelab_helper.engine.host_probe import HostProbeRequest, UnknownProbeError
from homelab_helper.engine.host_probe import probe_host as _probe_host
from homelab_helper.engine.k8s_import import discover_k8s_nodes
from homelab_helper.engine.manifest import BLAST_RADII, build_artifact
from homelab_helper.engine.network_path import TOPOLOGY_ENV_VAR, TopologyError, load_topology
from homelab_helper.engine.placement import network_verdict
from homelab_helper.engine.placement import recommend_placement as _recommend_placement
from homelab_helper.engine.rebalance import plan_rebalance as _plan_rebalance
from homelab_helper.engine.reconfigure import analyze_surplus as _analyze_surplus
from homelab_helper.engine.retire import is_retired, retired_host_ids
from homelab_helper.engine.retire import retire_host as _retire_host
from homelab_helper.engine.stray_config import reconcile_stray_config
from homelab_helper.engine.talos_probe import TalosProbeRequest
from homelab_helper.engine.talos_probe import probe_talos as _probe_talos
from homelab_helper.engine.trust import ActionRequest, decide, load_trust_context, open_windows
from homelab_helper.engine.virt_reconcile import reconcile_proxmox_cluster
from homelab_helper.engine.workloads import WorkloadLibraryError, load_workload_library
from homelab_helper.secrets import redact

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

server = MCPServer(
    "homelab-helper",
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
    status: str | None = None, kind: str | None = None, severity: str | None = None
) -> list[dict[str, Any]]:
    """List findings, optionally filtered by status (open/acknowledged/resolved/
    suppressed), kind (e.g. stray-config, drift-candidate), or severity."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            stmt = select(ReconciliationFinding).order_by(ReconciliationFinding.last_seen.desc())
            rows = (await session.execute(stmt)).scalars().all()
            out = []
            for f in rows:
                if status and f.status.value != status:
                    continue
                if kind and f.kind.value != kind:
                    continue
                if severity and f.severity.value != severity:
                    continue
                out.append(_finding_dict(f))
            return out
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


def _load_unifi_adapter() -> UniFiAdapter:
    """Factory (monkeypatched in tests)."""
    return UniFiAdapter.from_env()


def _load_unifi_adapters() -> list[UniFiAdapter]:
    """Every configured controller; defers to the single-controller factory
    unless HOMELAB_HELPER_UNIFI_CONTROLLERS names more than one."""
    raw = os.environ.get("HOMELAB_HELPER_UNIFI_CONTROLLERS") or ""
    if not [n for n in raw.split(",") if n.strip()]:
        return [_load_unifi_adapter()]
    return [UniFiAdapter(cfg) for cfg in UniFiConfig.all_from_env()]


def _load_cloudflare_adapter() -> CloudflareAdapter:
    """Factory (monkeypatched in tests)."""
    return CloudflareAdapter.from_env()


def _load_argocd_adapter() -> ArgoCDAdapter:
    """Factory (monkeypatched in tests)."""
    return ArgoCDAdapter.from_env()


def _load_proxmox_adapter() -> ProxmoxAdapter:
    """Factory (monkeypatched in tests)."""
    return ProxmoxAdapter.from_env()


def _load_k8s_adapter() -> K8sAdapter:
    """Factory (monkeypatched in tests)."""
    return K8sAdapter.from_env()


def _load_omv_adapter() -> OpenMediaVaultAdapter:
    """Factory (monkeypatched in tests)."""
    return OpenMediaVaultAdapter.from_env()


def _load_hass_adapter() -> HomeAssistantAdapter:
    """Factory (monkeypatched in tests)."""
    return HomeAssistantAdapter.from_env()


async def _discover_one_unifi(session: AsyncSession, adapter: UniFiAdapter) -> dict[str, Any]:
    cfg = adapter.config
    try:
        dns = await adapter.list_dns_records()
        clients = await adapter.list_clients()
        networks = await adapter.list_networks()
    finally:
        await adapter.aclose()
    now = datetime.now(UTC)
    # Each controller owns its own (scope, resolver) slice, so one gateway's
    # sync never reaps another's endpoints.
    eps = await reconcile_internal_endpoints(session, dns, resolver=cfg.resolver, when=now)
    stray = await reconcile_stray_config(session, networks, clients, when=now)
    return {
        "controller": cfg.name,
        "resolver": cfg.resolver,
        "dns_records": len(dns),
        "clients": len(clients),
        "networks": len(networks),
        "endpoints": {
            "created": len(eps.created),
            "updated": len(eps.updated),
            "removed": len(eps.removed),
            "moved": len(eps.moved),
        },
        "superseded_resolvers": list(eps.superseded_resolvers),
        "stray_config": {"opened": len(stray.opened), "resolved": len(stray.resolved)},
    }


async def _discover_unifi(session: AsyncSession) -> dict[str, Any]:
    """Read every configured controller. A lab with one gateway gets a
    one-element list; a multi-site lab gets one entry per controller, and an
    unreachable controller is reported without failing the others."""
    results: list[dict[str, Any]] = []
    for adapter in _load_unifi_adapters():
        try:
            results.append(await _discover_one_unifi(session, adapter))
        except Exception as exc:
            results.append({"controller": adapter.config.name, "error": redact(str(exc))})
    if len(results) == 1:
        return results[0]
    return {"controllers": results}


async def _discover_cloudflare(session: AsyncSession) -> dict[str, Any]:
    adapter = _load_cloudflare_adapter()
    try:
        dns = await adapter.list_dns_records()
    finally:
        await adapter.aclose()
    eps = await reconcile_external_endpoints(session, dns, when=datetime.now(UTC))
    return {
        "dns_records": len(dns),
        "endpoints": {
            "created": len(eps.created),
            "updated": len(eps.updated),
            "removed": len(eps.removed),
        },
    }


async def _discover_argocd(session: AsyncSession) -> dict[str, Any]:
    adapter = _load_argocd_adapter()
    try:
        apps = await adapter.list_applications()
    finally:
        await adapter.aclose()
    drift = await reconcile_argocd_drift(session, apps, when=datetime.now(UTC))
    return {
        "applications": len(apps),
        "drift_findings": {
            "opened": len(drift.opened),
            "reopened": len(drift.reopened),
            "resolved": len(drift.resolved),
        },
    }


async def _discover_proxmox(session: AsyncSession) -> dict[str, Any]:
    adapter = _load_proxmox_adapter()
    try:
        status = await adapter.cluster_status()
        vms = await adapter.list_vms()
    finally:
        await adapter.aclose()
    vr = await reconcile_proxmox_cluster(session, status, vms, when=datetime.now(UTC))
    return {
        "cluster": vr.cluster_name,
        "vms": {
            "created": len(vr.vms_created),
            "updated": len(vr.vms_updated),
            "unchanged": len(vr.vms_unchanged),
        },
    }


async def _discover_k8s(session: AsyncSession) -> dict[str, Any]:
    adapter = _load_k8s_adapter()
    result = await discover_k8s_nodes(session, adapter, when=datetime.now(UTC))
    return {
        "nodes_seen": result.nodes_seen,
        "hosts_matched": len(result.hosts_matched),
        "hosts_created": len(result.hosts_created),
    }


async def _discover_omv(session: AsyncSession) -> dict[str, Any]:
    adapter = _load_omv_adapter()
    try:
        filesystems = await adapter.list_filesystems()
        disks = await adapter.list_smart_devices()
        shares = await adapter.list_shared_folders()
        nfs = await adapter.list_nfs_exports()
        smb = await adapter.list_smb_shares()
    finally:
        await adapter.aclose()
    return {
        "filesystems": len(filesystems),
        "disks": len(disks),
        "shares": len(shares),
        "nfs_exports": len(nfs),
        "smb_shares": len(smb),
        "note": "read-only summary; OMV facts are not yet persisted to the harness DB",
    }


async def _discover_hass(session: AsyncSession) -> dict[str, Any]:
    adapter = _load_hass_adapter()
    try:
        config = await adapter.get_config()
        states = await adapter.list_states()
        service_domains = await adapter.list_service_domains()
    finally:
        await adapter.aclose()
    result = await import_home_assistant(
        session,
        config=config,
        states=states,
        service_domains=service_domains,
        url=adapter.config.url,
        when=datetime.now(UTC),
    )
    return result.as_dict()


_DISCOVERERS = {
    "unifi": _discover_unifi,
    "cloudflare": _discover_cloudflare,
    "argocd": _discover_argocd,
    "proxmox": _discover_proxmox,
    "k8s": _discover_k8s,
    "omv": _discover_omv,
    "hass": _discover_hass,
}


@server.tool()
async def run_discovery(source: str) -> dict[str, Any]:
    """Run a management-plane discovery and persist into the harness DB.
    Source must be one of: unifi, cloudflare, argocd, proxmox, k8s, omv, hass.
    Reads the live source (credentials from HOMELAB_HELPER_* env vars); never
    writes to the infrastructure itself."""
    discoverer = _DISCOVERERS.get(source)
    if discoverer is None:
        return {"error": f"unknown source {source!r}; expected one of {sorted(_DISCOVERERS)}"}
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            result = await discoverer(session)
        return {"source": source, **result}
    except Exception as exc:  # surface adapter/config errors as data, not protocol faults
        return {"source": source, "error": redact(str(exc))}
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
async def plan_rebalance() -> dict[str, Any]:
    """Fleet memory load per host plus up to three candidate rebalancing plans
    across cost classes (VM migrations only; one DIMM move; one DIMM purchase),
    each with steps, tradeoffs, and resulting load. Proposals only — the
    operator migrates, moves, or buys by hand."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            report = await _plan_rebalance(session)
        return report.as_dict()
    except (TopologyError, OSError) as exc:
        return {"error": f"topology error: {exc}"}
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
    """Hosts with capacity to spare and something reconfigurable about it
    (stopped VMs, spare DIMMs), each with the honest options: use it, move it,
    or declare the reserve deliberate."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            hits = await _analyze_surplus(session)
        return {"hits": [h.as_dict() for h in hits]}
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
# READ-ONLY, deliberately and permanently. The trust gradient's whole premise
# is that an LLM is never in the path that authorizes execution, so this
# surface lets a model *see* the policy — what is allowed, what ran, what is
# pending — and gives it no way to change any of it. There is no MCP tool to
# grant a cell, open a window, override, or execute a proposal; those are
# operator gestures at the CLI. A mechanical test enforces the absence.


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
                    "rolled_back_at": _iso(r.rolled_back_at),
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
                action = ActionRequest(
                    domain=manifest.domain,
                    action_kind=manifest.action_kind,
                    blast_radius=manifest.blast_radius,
                    hostnames=manifest.hostnames,
                    rollback_verified=False,
                    provenance=proposal.proposed_by,
                )
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
    action = ActionRequest(
        domain=manifest.domain,
        action_kind=manifest.action_kind,
        blast_radius=manifest.blast_radius,
        hostnames=manifest.hostnames,
        rollback_verified=False,
        provenance=proposal.proposed_by,
    )
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
) -> dict[str, Any]:
    """Draft a guest power action (start/stop/shutdown/restart of a Proxmox
    VM or container) as a PENDING proposal for the operator to run or reject
    with `helper exec`. Validates the manifest, writes only to the harness DB,
    and returns what policy would decide right now. Never executes; an agent
    cannot grant, override, or open a window."""
    if blast_radius not in BLAST_RADII:
        return {"error": f"blast_radius must be one of {', '.join(BLAST_RADII)}"}
    try:
        artifact = build_artifact(
            action_kind=action_kind,
            node=node,
            vmid=vmid,
            vm_kind=vm_kind,
            hostnames=tuple(hostnames) if hostnames else None,
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
                "next": f"an operator runs `helper exec run {proposal.id}` or `helper exec reject {proposal.id}`",
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
            rows = (
                (
                    await session.execute(
                        select(ProposalLog).order_by(ProposalLog.proposed_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            matches = [p for p in rows if str(p.id).startswith(proposal_id.lower())]
            if not matches:
                return {"error": f"no proposal with id {proposal_id!r}"}
            if len(matches) > 1:
                return {"error": f"prefix {proposal_id!r} is ambiguous ({len(matches)} matches)"}
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
