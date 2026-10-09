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

import asyncio
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy import func, or_, select

from homelab_helper.adapters.argocd import ArgoCDAdapter, ArgoCDConfigError
from homelab_helper.adapters.cloudflare import CloudflareAdapter
from homelab_helper.adapters.homeassistant import HomeAssistantAdapter
from homelab_helper.adapters.kernel_ssh import KernelSSHAdapter
from homelab_helper.adapters.kubernetes import K8sAdapter
from homelab_helper.adapters.mikrotik import MikroTikAdapter
from homelab_helper.adapters.openmediavault import OpenMediaVaultAdapter
from homelab_helper.adapters.proxmox import ProxmoxAdapter, ProxmoxConfigError
from homelab_helper.adapters.unifi import UniFiAdapter, UniFiConfig, UniFiConfigError
from homelab_helper.config import PROBE_ALLOW_VAR, database_url, load_env, probe_allow_patterns
from homelab_helper.config import config_status as _config_status
from homelab_helper.db.enums import (
    DiscoverySource,
    FindingKind,
    FindingStatus,
    ProposalOutcome,
    ResolutionScope,
)
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
    UsageSample,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.approval import (
    ApprovalConfigError,
    ApprovalError,
    approval_channel_from_env,
)
from homelab_helper.engine.argocd_drift import reconcile_argocd_drift
from homelab_helper.engine.backups import CATEGORIES as BACKUP_CATEGORIES
from homelab_helper.engine.backups import (
    backup_issues,
    capacity_issues,
    orphan_issues,
    reconcile_backup_findings,
)
from homelab_helper.engine.bottlenecks import analyze_bottlenecks as _analyze_bottlenecks
from homelab_helper.engine.bottlenecks import persist_bottlenecks
from homelab_helper.engine.category_findings import reconcile_category_findings
from homelab_helper.engine.dns_reconcile import (
    reconcile_external_endpoints,
    reconcile_internal_endpoints,
)
from homelab_helper.engine.escalation import PROMOTION_STREAK, is_promotable
from homelab_helper.engine.executor import (
    ExecutionRefused,
    ManifestError,
    parse_manifest,
)
from homelab_helper.engine.executor import (
    execute_proposal as _run_proposal,
)
from homelab_helper.engine.hass_import import import_home_assistant
from homelab_helper.engine.host_probe import HostProbeRequest, UnknownProbeError
from homelab_helper.engine.host_probe import probe_host as _probe_host
from homelab_helper.engine.k8s_import import discover_k8s_nodes
from homelab_helper.engine.k8s_workloads import reconcile_workload_health
from homelab_helper.engine.manifest import (
    BLAST_RADII,
    DNS_RECORD_TYPES,
    WORKLOAD_KINDS,
    build_argocd_artifact,
    build_artifact,
    build_dns_artifact,
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
from homelab_helper.engine.rightsizing import evaluate as evaluate_rightsizing
from homelab_helper.engine.rightsizing import reconcile_rightsizing
from homelab_helper.engine.storage import (
    Projection,
    detached_disk_issues,
    headroom_issues,
    project_headroom,
    released_pv_issues,
    snapshot_issues,
    template_clutter_issues,
)
from homelab_helper.engine.stray_config import reconcile_stray_config
from homelab_helper.engine.stray_export import reconcile_stray_exports
from homelab_helper.engine.suggestions import (
    building_block_issues,
    idle_gpu_issues,
    present_names,
)
from homelab_helper.engine.talos_probe import TalosProbeRequest
from homelab_helper.engine.talos_probe import probe_talos as _probe_talos
from homelab_helper.engine.trust import ActionRequest, decide, load_trust_context, open_windows
from homelab_helper.engine.usage import GUEST_FIELDS as USAGE_GUEST_FIELDS
from homelab_helper.engine.usage import NODE_EXTRA as USAGE_NODE_EXTRA
from homelab_helper.engine.usage import NODE_FIELDS as USAGE_NODE_FIELDS
from homelab_helper.engine.usage import SOURCE_TIMEFRAME as USAGE_TIMEFRAME
from homelab_helper.engine.usage import STORAGE_FIELDS as USAGE_STORAGE_FIELDS
from homelab_helper.engine.usage import (
    pool_history,
    prune_usage,
    record_usage,
    summarize,
)
from homelab_helper.engine.usage import rollup as usage_rollup
from homelab_helper.engine.versions import (
    hass_update_issues,
    k8s_issues,
    load_eol_table,
    os_eol_issues,
    proxmox_issues,
    reconcile_version_findings,
)
from homelab_helper.engine.virt_reconcile import reconcile_proxmox_cluster
from homelab_helper.engine.workloads import WorkloadLibraryError, load_workload_library
from homelab_helper.secrets import redact

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.engine.category_findings import CategoryIssue

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


def _load_mikrotik_adapter() -> MikroTikAdapter:
    """Factory (monkeypatched in tests)."""
    return MikroTikAdapter.from_env()


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
            "adopted_from_legacy_standalone": len(vr.vms_adopted),
        },
        "legacy_standalone_row_removed": vr.legacy_cluster_removed,
    }


async def _discover_k8s(session: AsyncSession) -> dict[str, Any]:
    adapter = _load_k8s_adapter()
    now = datetime.now(UTC)
    result = await discover_k8s_nodes(session, adapter, when=now)
    health = await reconcile_workload_health(session, await adapter.list_workloads(), when=now)
    return {
        "nodes_seen": result.nodes_seen,
        "hosts_matched": len(result.hosts_matched),
        "hosts_created": len(result.hosts_created),
        "workloads_seen": health.seen,
        "workloads_unhealthy": health.unhealthy,
        "workload_findings_resolved": health.resolved,
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
    result = await reconcile_stray_exports(
        session, filesystems, shares, [*nfs, *smb], when=datetime.now(UTC)
    )
    return {
        "filesystems": len(filesystems),
        "disks": len(disks),
        "shares": len(shares),
        "nfs_exports": len(nfs),
        "smb_shares": len(smb),
        "stray_exports": [hit.as_dict() for hit in result.hits],
        "stray_export_findings": {
            "opened": len(result.opened),
            "reopened": len(result.reopened),
            "updated": len(result.updated),
            "resolved": len(result.resolved),
        },
        "note": "stray-export findings persisted; other OMV facts are read-only summaries",
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


async def _discover_mikrotik(session: AsyncSession) -> dict[str, Any]:
    adapter = _load_mikrotik_adapter()
    cfg = adapter.config
    try:
        identity = await adapter.identity()
        resource = await adapter.resource()
        addresses = await adapter.list_addresses()
        leases = await adapter.list_leases()
        dns = await adapter.list_dns_records()
    finally:
        await adapter.aclose()
    now = datetime.now(UTC)
    eps = await reconcile_internal_endpoints(
        session, dns, resolver=cfg.resolver, source=DiscoverySource.MIKROTIK, when=now
    )
    stray = await reconcile_stray_config(session, addresses, leases, when=now)
    return {
        "router": identity or cfg.name,
        "resolver": cfg.resolver,
        "version": resource.get("version"),
        "board": resource.get("board"),
        "addresses": len(addresses),
        "leases": len(leases),
        "dns_records": len(dns),
        "endpoints": {
            "created": len(eps.created),
            "updated": len(eps.updated),
            "removed": len(eps.removed),
            "moved": len(eps.moved),
        },
        "superseded_resolvers": list(eps.superseded_resolvers),
        "stray_config": {"opened": len(stray.opened), "resolved": len(stray.resolved)},
    }


async def _discover_versions(session: AsyncSession) -> dict[str, Any]:
    """Phase 8.1: version currency from Proxmox, stored host facts and Home Assistant.

    Each source that fails is reported and contributes no categories, so its
    findings neither open nor resolve this run.
    """
    issues: list[CategoryIssue] = []
    observed: set[str] = set()
    errors: dict[str, str] = {}

    try:
        adapter = _load_proxmox_adapter()
        try:
            status = await adapter.cluster_status()
            nodes = []
            for n in await adapter.list_nodes():
                if n.get("status") != "online":
                    continue
                name = str(n.get("node"))
                version = await adapter.node_version(name)
                nodes.append(
                    {
                        "node": name,
                        "version": version.get("version"),
                        "pending": await adapter.pending_updates(name),
                    }
                )
        finally:
            await adapter.aclose()
        issues += proxmox_issues(str(status.get("name") or "proxmox"), nodes)
        observed |= {"pve-updates", "pve-mixed"}
    except Exception as exc:  # one dead source must not sink the others
        errors["proxmox"] = redact(str(exc))

    retired = await retired_host_ids(session)
    hosts = [
        (str(h.id), h.hostname, dict(h.capabilities or {}))
        for h in (await session.execute(select(Host))).scalars().all()
        if h.id not in retired
    ]
    issues += os_eol_issues(hosts, load_eol_table(), datetime.now(UTC).date())
    observed.add("os-eol")
    skew, skew_observed = k8s_issues(hosts)
    issues += skew
    observed |= skew_observed

    try:
        hass = _load_hass_adapter()
        try:
            states = await hass.list_states()
        finally:
            await hass.aclose()
        issues += hass_update_issues("home-assistant", states)
        observed.add("hass-updates")
    except Exception as exc:
        errors["hass"] = redact(str(exc))

    result = await reconcile_version_findings(session, issues, observed, when=datetime.now(UTC))
    return {
        "observed": result.observed,
        "issues": len(issues),
        "findings": result.counts(),
        "errors": errors,
    }


async def _discover_backups(session: AsyncSession) -> dict[str, Any]:
    """Phase 8.2: backup posture from Proxmox backup jobs and backup storages."""
    adapter = _load_proxmox_adapter()
    try:
        guests = await adapter.list_vms()
        jobs = await adapter.list_backup_jobs()
        storages: list[dict[str, Any]] = []
        backups: list[dict[str, Any]] = []
        orphans: list[CategoryIssue] = []
        seen_shared: set[str] = set()
        for row in await adapter.list_storage():
            if "backup" not in str(row.get("content") or "") or row.get("status") != "available":
                continue
            name = str(row.get("storage"))
            if row.get("shared"):
                if name in seen_shared:
                    continue
                seen_shared.add(name)
                label = name
            else:
                label = f"{name}@{row.get('node')}"
            content = await adapter.storage_content(str(row.get("node")), name)
            backups += content
            orphans += orphan_issues(label, guests, content)
            storages.append({**row, "storage": label})
    finally:
        await adapter.aclose()
    now = datetime.now(UTC)
    issues = backup_issues(guests, jobs, backups, now=now) + orphans + capacity_issues(storages)
    result = await reconcile_backup_findings(session, issues, set(BACKUP_CATEGORIES), when=now)
    return {
        "guests": len(guests),
        "jobs": len(jobs),
        "backup_storages": [s["storage"] for s in storages],
        "backups": len(backups),
        "issues": len(issues),
        "findings": result.counts(),
    }


async def _discover_suggestions(session: AsyncSession) -> dict[str, Any]:
    """Phase 8.7: capability the lab owns but does not use, from stored facts only.

    Reads the harness DB and the workload library — no adapter calls, so it
    cannot fail for a source being down and both categories are always
    observed.
    """
    retired = await retired_host_ids(session)
    hosts = [
        (str(h.id), h.hostname, dict(h.capabilities or {}))
        for h in (await session.execute(select(Host))).scalars().all()
        if h.id not in retired
    ]
    guests = (
        (await session.execute(select(VirtualMachine.name).where(~VirtualMachine.template)))
        .scalars()
        .all()
    )
    services = (await session.execute(select(Service.name))).scalars().all()
    endpoints = (await session.execute(select(ServiceEndpoint.hostname))).scalars().all()
    present = present_names(guests, services, endpoints)

    library = load_workload_library()
    issues = idle_gpu_issues(hosts, library, present) + building_block_issues(library, present)
    result = await reconcile_category_findings(
        session,
        FindingKind.SERVICE_SUGGESTION,
        issues,
        {"capability-idle-gpu", "building-block-missing"},
    )
    return {
        "findings": result.counts(),
        "issues": len(issues),
        "known_names": len(present),
        "errors": {},
    }


async def _proxmox_storage_facts(
    adapter: Any,
) -> tuple[list[dict[str, Any]], dict[str, str], list[dict[str, Any]]]:
    """Guest configs + snapshots, one reader per pool, and the pools' images."""
    guests: list[dict[str, Any]] = []
    for vm in await adapter.list_vms():
        node, vmid, kind = vm.get("node"), vm.get("vmid"), vm.get("type")
        if not (node and vmid and kind):
            continue
        entry: dict[str, Any] = {
            "vmid": int(vmid),
            "node": str(node),
            "kind": str(kind),
            "name": vm.get("name"),
            "template": bool(vm.get("template")),
            "config": await adapter.vm_config(str(node), int(vmid), str(kind)),
        }
        if not entry["template"]:
            entry["snapshots"] = await adapter.list_snapshots(str(node), int(vmid), str(kind))
        guests.append(entry)

    pools: dict[str, str] = {}
    for row in await adapter.cluster_resources("storage"):
        name, node = str(row.get("storage") or ""), str(row.get("node") or "")
        if name and node and row.get("status") == "available":
            pools.setdefault(name, node)

    images: list[dict[str, Any]] = []
    for name, node in pools.items():
        for content in ("iso", "vztmpl"):
            try:
                images += await adapter.storage_content(node, name, content)
            except Exception:  # a pool that holds no such content
                continue
    return guests, pools, images


async def _pool_projections(
    session: AsyncSession, pools: list[str]
) -> list[tuple[str, Projection | None]]:
    """Each pool's time-to-full fit from the 8.3 history, where there is any."""
    projected: list[tuple[str, Projection | None]] = []
    for name in pools:
        rows = await pool_history(session, name)
        if not rows:
            continue
        total = next((r.disk_total for r in reversed(rows) if r.disk_total), 0) or 0
        projected.append(
            (
                name,
                project_headroom(
                    [(r.ts.replace(tzinfo=UTC), int(r.disk_used or 0)) for r in rows], int(total)
                ),
            )
        )
    return projected


async def _discover_storage(session: AsyncSession) -> dict[str, Any]:
    """Phase 8.5: storage efficiency from Proxmox configs, snapshots, pool history, K8s PVs.

    Each source that fails contributes no categories, so its findings neither
    open nor resolve this run (invariant 1).
    """
    issues: list[CategoryIssue] = []
    observed: set[str] = set()
    errors: dict[str, str] = {}

    try:
        adapter = _load_proxmox_adapter()
        try:
            guests, pools, images = await _proxmox_storage_facts(adapter)
        finally:
            await adapter.aclose()

        issues += snapshot_issues(guests)
        issues += detached_disk_issues(guests)
        issues += template_clutter_issues(images, guests)
        observed |= {
            "storage-snapshot-stale",
            "storage-detached-disk",
            "storage-template-clutter",
        }

        projected = await _pool_projections(session, list(pools))
        if projected:
            issues += headroom_issues(projected)
            observed.add("storage-headroom")
    except Exception as exc:  # one dead source must not sink the others
        errors["proxmox"] = redact(str(exc))

    try:
        k8s = _load_k8s_adapter()
        issues += released_pv_issues(await k8s.get_resource("pv"))
        observed.add("storage-released-pv")
    except Exception as exc:
        errors["k8s"] = redact(str(exc))

    result = await reconcile_category_findings(
        session, FindingKind.STORAGE_EFFICIENCY, issues, observed
    )
    return {"findings": result.counts(), "issues": len(issues), "errors": errors}


async def _discover_usage(session: AsyncSession) -> dict[str, Any]:
    """Phase 8.3: hourly and daily usage rollups from Proxmox RRD, then prune."""
    adapter = _load_proxmox_adapter()
    try:
        status = await adapter.cluster_status()
        cluster = str(status.get("name") or "proxmox")
        subjects: list[tuple[str, str, str | None, str, int | None, str | None]] = [
            ("host", str(n["node"]), str(n["node"]), str(n["node"]), None, None)
            for n in await adapter.list_nodes()
            if n.get("status") == "online"
        ]
        subjects += [
            (
                "guest",
                f"{cluster}/{v['vmid']}",
                v.get("name"),
                str(v["node"]),
                int(v["vmid"]),
                str(v.get("type")),
            )
            for v in await adapter.list_vms()
            if not v.get("template") and v.get("node")
        ]
        # One reader per pool: a shared storage is reported by every node, and
        # its RRD is the same series whichever node answers. 8.5 projects
        # time-to-full from this history.
        pools: dict[str, str] = {}
        for row in await adapter.cluster_resources("storage"):
            name, node = str(row.get("storage") or ""), str(row.get("node") or "")
            if name and node and row.get("status") == "available":
                pools.setdefault(name, node)
        subjects += [("storage", name, name, node, None, None) for name, node in pools.items()]
        gate = asyncio.Semaphore(8)

        async def _fetch(
            stype: str, key: str, node: str, vmid: int | None, kind: str | None
        ) -> dict[str, list[Any]]:
            async with gate:
                out: dict[str, list[Any]] = {}
                for resolution, timeframe in USAGE_TIMEFRAME.items():
                    for cf in ("AVERAGE", "MAX"):
                        out[f"{resolution}:{cf}"] = (
                            await adapter.storage_rrd(node, key, timeframe, cf)
                            if stype == "storage"
                            else await adapter.rrd(node, timeframe, cf, vmid=vmid, kind=kind)
                        )
                return out

        fetched = await asyncio.gather(
            *(_fetch(stype, key, node, vmid, kind) for stype, key, _, node, vmid, kind in subjects)
        )
    finally:
        await adapter.aclose()

    inserted = updated = with_data = 0
    for (stype, key, label, _node, _vmid, _kind), points in zip(subjects, fetched, strict=True):
        fields = {
            "host": USAGE_NODE_FIELDS,
            "guest": USAGE_GUEST_FIELDS,
            "storage": USAGE_STORAGE_FIELDS,
        }[stype]
        extra = USAGE_NODE_EXTRA if stype == "host" else ()
        any_data = False
        for resolution in USAGE_TIMEFRAME:
            buckets = usage_rollup(
                points[f"{resolution}:AVERAGE"],
                points[f"{resolution}:MAX"],
                resolution=resolution,
                fields=fields,
                extra=extra,
            )
            any_data = any_data or bool(buckets)
            w = await record_usage(
                session,
                subject_type=stype,
                subject_key=key,
                label=label,
                resolution=resolution,
                buckets=buckets,
            )
            inserted += w.inserted
            updated += w.updated
        with_data += any_data
    pruned = await prune_usage(session)
    return {
        "subjects": len(subjects),
        "with_data": with_data,
        "samples": {"inserted": inserted, "updated": updated},
        "pruned": pruned,
    }


_DISCOVERERS = {
    "unifi": _discover_unifi,
    "cloudflare": _discover_cloudflare,
    "argocd": _discover_argocd,
    "proxmox": _discover_proxmox,
    "k8s": _discover_k8s,
    "omv": _discover_omv,
    "hass": _discover_hass,
    "mikrotik": _discover_mikrotik,
    "versions": _discover_versions,
    "backups": _discover_backups,
    "usage": _discover_usage,
    "suggestions": _discover_suggestions,
    "storage": _discover_storage,
}


@server.tool()
async def run_discovery(source: str) -> dict[str, Any]:
    """Run a management-plane discovery and persist into the harness DB.
    Source must be one of: unifi, cloudflare, argocd, proxmox, k8s, omv, hass, mikrotik,
    versions (Phase 8.1: package lag, mixed versions, OS end of life, HA updates),
    backups (Phase 8.2: uncovered/stale/unverified guests, orphaned backups, capacity),
    usage (Phase 8.3: hourly/daily usage rollups from Proxmox RRD, backfilled, pruned).
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
async def usage_summary(subject: str | None = None, days: int = 30) -> list[dict[str, Any]]:
    """What hosts and guests actually used over the last ``days`` (Phase 8.3).

    Per subject: CPU p95 and peak (fraction of allocated CPUs), memory p95 and
    peak (bytes) against the allocation, and how many hourly buckets back it.
    ``subject`` filters by node name, guest name, or vmid; omit for everything.
    Read-only; history comes from ``run_discovery("usage")``."""
    engine = make_engine(database_url())
    try:
        async with session_scope(make_sessionmaker(engine)) as session:
            keys = (
                await session.execute(
                    select(
                        UsageSample.subject_type, UsageSample.subject_key, UsageSample.label
                    ).distinct()
                )
            ).all()
            out = []
            for stype, key, label in keys:
                if subject and subject not in (key, label, key.rsplit("/", 1)[-1]):
                    continue
                summary = await summarize(
                    session, subject_type=stype, subject_key=key, window=timedelta(days=days)
                )
                if summary.get("samples"):
                    out.append({"type": stype, **summary})
            return sorted(out, key=lambda r: (r["type"], str(r.get("label") or r["subject"])))
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
            window = timedelta(days=days)
            if persist:
                result, issues, skipped = await reconcile_rightsizing(session, window=window)
                counts: dict[str, int] | None = result.counts()
            else:
                issues, _evaluated, skipped = await evaluate_rightsizing(session, window=window)
                counts = None
            return {
                "window_days": days,
                "recommendations": [
                    {
                        "category": i.category,
                        "severity": i.severity.value,
                        "guest": i.target_id,
                        "title": i.title,
                        "detail": i.description,
                        **i.evidence,
                    }
                    for i in issues
                ],
                "skipped": skipped,
                "findings": counts,
            }
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
    domain. Rollback is Argo CD's own history (the current deployed entry).
    Same contract as `propose_action`: validates, writes only to the harness
    DB, never executes on its own."""
    if blast_radius not in BLAST_RADII:
        return {"error": f"blast_radius must be one of {', '.join(BLAST_RADII)}"}
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


def _unifi_adapter_for(controller: str | None) -> UniFiAdapter | None:
    """The UniFi adapter a DNS manifest names (or the only one), ``None`` if unconfigured."""
    try:
        adapters = _load_unifi_adapters()
    except UniFiConfigError:
        return None
    if controller is None:
        return adapters[0] if len(adapters) == 1 else None
    for a in adapters:
        if a.config.name == controller:
            return a
    return None


@dataclass(frozen=True)
class _Adapters:
    proxmox: ProxmoxAdapter
    k8s: K8sAdapter | None
    argocd: ArgoCDAdapter | None
    unifi: UniFiAdapter | None
    ssh: KernelSSHAdapter | None = None

    def __iter__(self) -> Iterator[Any]:
        return iter((self.proxmox, self.k8s, self.argocd, self.unifi, self.ssh))


def _execution_adapters(manifest: Any) -> tuple[_Adapters | None, str | None]:
    """The adapters one manifest needs, or a message naming what is not configured."""
    try:
        proxmox = _load_proxmox_adapter()
    except ProxmoxConfigError as exc:
        return None, f"Proxmox adapter: {exc}"
    k8s = _load_k8s_adapter() if shutil.which("kubectl") else None
    argocd = None
    unifi = None
    if manifest.is_argocd:
        try:
            argocd = _load_argocd_adapter()
        except ArgoCDConfigError as exc:
            return None, f"Argo CD adapter: {exc}"
    if manifest.is_dns:
        unifi = _unifi_adapter_for(manifest.controller)
        if unifi is None:
            return None, (
                f"UniFi adapter: no controller matches {manifest.controller!r} "
                "(set HOMELAB_HELPER_UNIFI_CONTROLLERS)"
            )
    ssh = KernelSSHAdapter() if manifest.is_node else None
    return _Adapters(proxmox, k8s, argocd, unifi, ssh), None


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
            rows = (
                (
                    await session.execute(
                        select(ProposalLog)
                        .where(ProposalLog.outcome == ProposalOutcome.PENDING)
                        .order_by(ProposalLog.proposed_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            matches = [p for p in rows if str(p.id).startswith(proposal_id.lower())]
            if len(matches) != 1:
                return {
                    "error": (
                        f"no pending proposal with id {proposal_id!r}"
                        if not matches
                        else f"id prefix {proposal_id!r} matches {len(matches)} pending proposals"
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

            adapters, problem = _execution_adapters(manifest)
            if problem is not None:
                return {"error": problem}
            assert adapters is not None
            adapter, k8s, argocd, unifi, ssh = adapters
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
