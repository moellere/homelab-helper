"""MCP server — the harness's query surface as Model Context Protocol tools.

Phase 4's first slice. Exposes what the CLI already answers (hosts, findings,
services, audit rollup) plus the management-plane discovery runs as MCP tools
over stdio, so Claude Desktop / Claude Code / Cursor can drive the harness in
natural language without shelling out.

**Read-only against infrastructure (L1).** Query tools only read the harness
DB. ``run_discovery`` reads live sources (UniFi, Cloudflare, Argo CD, Proxmox,
K8s, OMV — credentials from the same ``HOMELAB_HELPER_*`` env vars the CLI
uses) and persists into the harness DB — never a write to the lab itself.

Tools return plain dicts/lists (the SDK ships them as structured content).
Lookup misses return ``{"error": ...}`` rather than raising, so an LLM caller
gets a message it can act on instead of a protocol error.

Run it: ``helper mcp serve`` (stdio). Register in a client, e.g. Claude Code::

    claude mcp add homelab -- uv run --directory /path/to/homelab-helper helper mcp serve
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy import func, select

from homelab_helper.adapters.argocd import ArgoCDAdapter
from homelab_helper.adapters.cloudflare import CloudflareAdapter
from homelab_helper.adapters.kubernetes import K8sAdapter
from homelab_helper.adapters.openmediavault import OpenMediaVaultAdapter
from homelab_helper.adapters.proxmox import ProxmoxAdapter
from homelab_helper.adapters.unifi import UniFiAdapter, UniFiConfig
from homelab_helper.config import config_status as _config_status
from homelab_helper.config import database_url, load_env
from homelab_helper.db.enums import FindingStatus, ResolutionScope
from homelab_helper.db.models import (
    Cluster,
    Host,
    ReconciliationFinding,
    Service,
    ServiceEndpoint,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.argocd_drift import reconcile_argocd_drift
from homelab_helper.engine.dns_reconcile import (
    reconcile_external_endpoints,
    reconcile_internal_endpoints,
)
from homelab_helper.engine.host_probe import HostProbeRequest, UnknownProbeError
from homelab_helper.engine.host_probe import probe_host as _probe_host
from homelab_helper.engine.k8s_import import discover_k8s_nodes
from homelab_helper.engine.stray_config import reconcile_stray_config
from homelab_helper.engine.virt_reconcile import reconcile_proxmox_cluster

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


def _database_url() -> str:
    return database_url()


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
    engine = make_engine(_database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            hosts = (await session.execute(select(Host).order_by(Host.hostname))).scalars().all()
            return [
                {
                    "hostname": h.hostname,
                    "primary_ip": h.primary_ip,
                    "arch": h.arch.value,
                    "discovery_source": h.discovery_source.value,
                    "last_verified": _iso(h.last_verified),
                }
                for h in hosts
            ]
    finally:
        await engine.dispose()


@server.tool()
async def get_host(hostname: str) -> dict[str, Any]:
    """Full record for one host: identity, capabilities, guests it runs, service
    endpoints resolving to it, and its open findings."""
    engine = make_engine(_database_url())
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
    engine = make_engine(_database_url())
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
    engine = make_engine(_database_url())
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
    engine = make_engine(_database_url())
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
    engine = make_engine(_database_url())
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
    engine = make_engine(_database_url())
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
        },
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
            results.append({"controller": adapter.config.name, "error": str(exc)})
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


_DISCOVERERS = {
    "unifi": _discover_unifi,
    "cloudflare": _discover_cloudflare,
    "argocd": _discover_argocd,
    "proxmox": _discover_proxmox,
    "k8s": _discover_k8s,
    "omv": _discover_omv,
}


@server.tool()
async def run_discovery(source: str) -> dict[str, Any]:
    """Run a management-plane discovery and persist into the harness DB.
    Source must be one of: unifi, cloudflare, argocd, proxmox, k8s, omv.
    Reads the live source (credentials from HOMELAB_HELPER_* env vars); never
    writes to the infrastructure itself."""
    discoverer = _DISCOVERERS.get(source)
    if discoverer is None:
        return {"error": f"unknown source {source!r}; expected one of {sorted(_DISCOVERERS)}"}
    engine = make_engine(_database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            result = await discoverer(session)
        return {"source": source, **result}
    except Exception as exc:  # surface adapter/config errors as data, not protocol faults
        return {"source": source, "error": str(exc)}
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
    engine = make_engine(_database_url())
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
    Creates the Host row if the harness doesn't know it yet; pass primary_ip
    when the name doesn't resolve.
    """
    key_path = ssh_key_path or os.environ.get("HOMELAB_HELPER_SSH_KEY")
    if not key_path:
        return {"error": "an SSH key is required: pass ssh_key_path or set HOMELAB_HELPER_SSH_KEY"}
    engine = make_engine(_database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
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
        return {"hostname": hostname, "error": str(exc)}
    finally:
        await engine.dispose()


def main() -> None:
    """Run the MCP server over stdio (blocking)."""
    server.run(transport="stdio")


__all__ = ["main", "server"]
