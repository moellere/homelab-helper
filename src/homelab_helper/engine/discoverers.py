"""Discovery runners: one per source, shared by the CLI, the daemon and the MCP server.

Each runner reads one live source through ``adapters.factories`` and persists
what it saw into the harness DB; ``run_discovery`` runs one by name, records a
``SourceRun`` for it and turns an adapter or config error into ``{"error": ...}``.
This module used to live inside ``mcp_server``, which made every CLI command
import the MCP stack and let the CLI's and the server's copies drift.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select

from homelab_helper.adapters.kubernetes import KubeError, workload_usage
from homelab_helper.adapters.printwarden import PrintWardenAPIError
from homelab_helper.config import database_url
from homelab_helper.db.enums import (
    DiscoverySource,
    FindingKind,
)
from homelab_helper.db.models import (
    Host,
    Service,
    ServiceEndpoint,
    SourceRun,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.argocd_drift import reconcile_argocd_drift
from homelab_helper.engine.backups import CATEGORIES as BACKUP_CATEGORIES
from homelab_helper.engine.backups import (
    backup_issues,
    backup_target_timestamps,
    capacity_issues,
    declared_target_issues,
    hass_backup_issues,
    load_backup_targets,
    orphan_issues,
    reconcile_backup_findings,
    restore_drill_timestamps,
)
from homelab_helper.engine.baselines import reconcile_anomaly_findings
from homelab_helper.engine.capacity import (
    reconcile_capacity_findings,
    record_filesystem_samples,
)
from homelab_helper.engine.category_findings import reconcile_category_findings
from homelab_helper.engine.dns_reconcile import (
    reconcile_external_endpoints,
    reconcile_internal_endpoints,
)
from homelab_helper.engine.hass_import import import_home_assistant
from homelab_helper.engine.jetkvm import (
    attached_from_env,
    observe_jetkvm,
    snapshot_dir,
)
from homelab_helper.engine.k8s_import import discover_k8s_nodes
from homelab_helper.engine.k8s_workloads import (
    persisted_k8s_workloads,
    reconcile_workload_health,
)
from homelab_helper.engine.printwarden import reconcile_printwarden
from homelab_helper.engine.retention import prune_history
from homelab_helper.engine.retire import retired_host_ids
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
    image_names,
    missing_dependency_issues,
    present_names,
    unit_names,
)
from homelab_helper.engine.usage import GUEST_FIELDS as USAGE_GUEST_FIELDS
from homelab_helper.engine.usage import NODE_EXTRA as USAGE_NODE_EXTRA
from homelab_helper.engine.usage import NODE_FIELDS as USAGE_NODE_FIELDS
from homelab_helper.engine.usage import SOURCE_TIMEFRAME as USAGE_TIMEFRAME
from homelab_helper.engine.usage import STORAGE_FIELDS as USAGE_STORAGE_FIELDS
from homelab_helper.engine.usage import (
    pool_history,
    prune_usage,
    record_usage,
    record_workload_usage,
)
from homelab_helper.engine.usage import rollup as usage_rollup
from homelab_helper.engine.versions import (
    ceph_issues,
    hass_update_issues,
    k8s_eol_issues,
    k8s_issues,
    load_eol_table,
    os_eol_issues,
    proxmox_issues,
    reconcile_version_findings,
    workload_image_issues,
)
from homelab_helper.engine.virt_reconcile import (
    PROXMOX_CONCURRENCY,
    annotate_guest_storage,
    reconcile_proxmox_cluster,
)
from homelab_helper.engine.workloads import load_workload_library
from homelab_helper.secrets import redact

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.adapters.unifi import UniFiAdapter
    from homelab_helper.engine.category_findings import CategoryIssue


from homelab_helper.adapters import factories


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
    for adapter in factories.unifi_all():
        try:
            results.append(await _discover_one_unifi(session, adapter))
        except Exception as exc:
            results.append({"controller": adapter.config.name, "error": redact(str(exc))})
    if len(results) == 1:
        return results[0]
    return {"controllers": results}


async def _discover_cloudflare(session: AsyncSession) -> dict[str, Any]:
    adapter = factories.cloudflare()
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
    adapter = factories.argocd()
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
    adapter = factories.proxmox()
    try:
        ok, err = await adapter.health_check()
        if not ok:
            # As the CLI does: an unreachable API is an error, never an empty cluster.
            return {"error": f"proxmox unreachable: {err}"}
        status = await adapter.cluster_status()
        vms = await adapter.list_vms()
        await annotate_guest_storage(adapter, vms)
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
            "removed": len(vr.vms_removed),
            "removed_names": vr.vms_removed,
            "removal_skipped": vr.removal_skipped,
        },
        "legacy_standalone_row_removed": vr.legacy_cluster_removed,
    }


async def _discover_k8s(session: AsyncSession) -> dict[str, Any]:
    adapter = factories.k8s()
    now = datetime.now(UTC)
    result = await discover_k8s_nodes(session, adapter, when=now)
    health = await reconcile_workload_health(
        session, await adapter.list_workloads(), when=now, cluster_name=result.cluster
    )
    return {
        "nodes_seen": result.nodes_seen,
        "hosts_matched": len(result.hosts_matched),
        "hosts_created": len(result.hosts_created),
        "cluster": result.cluster,
        "workloads_seen": health.seen,
        "workloads_unhealthy": health.unhealthy,
        "workload_findings_resolved": health.resolved,
    }


async def _discover_omv(session: AsyncSession) -> dict[str, Any]:
    adapter = factories.omv()
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
    samples = await record_filesystem_samples(session, "omv", filesystems)
    return {
        "filesystems": len(filesystems),
        "filesystem_samples": {"inserted": samples.inserted, "updated": samples.updated},
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


async def _discover_jetkvm(session: AsyncSession) -> dict[str, Any]:
    adapter = factories.jetkvm()
    attached_to, port = attached_from_env()
    try:
        run = await observe_jetkvm(
            session, adapter, attached_to=attached_to, port=port, snapshot_dir=snapshot_dir()
        )
    finally:
        await adapter.aclose()
    out = run.as_dict()
    if out["error"]:
        out["error"] = redact(out["error"])
    return out


async def _discover_printwarden(session: AsyncSession) -> dict[str, Any]:
    adapter = factories.printwarden()
    status: dict[str, Any] | None = None
    error: str | None = None
    try:
        status = await adapter.status()
    except (httpx.HTTPError, PrintWardenAPIError) as exc:
        error = redact(str(exc)) or type(exc).__name__
    finally:
        await adapter.aclose()
    issues, counts = await reconcile_printwarden(session, adapter.url, status, error)
    return {
        "reachable": status is not None,
        "version": (status or {}).get("version"),
        "printers": sorted((status or {}).get("printers") or {}),
        "issues": [
            {"category": i.category, "severity": i.severity.value, "title": i.title} for i in issues
        ],
        "findings": counts,
    }


async def _discover_hass(session: AsyncSession) -> dict[str, Any]:
    adapters = factories.hass_all()
    results = []
    now = datetime.now(UTC)
    for adapter in adapters:
        try:
            config = await adapter.get_config()
            states = await adapter.list_states()
            service_domains = await adapter.list_service_domains()
        finally:
            await adapter.aclose()
        results.append(
            await import_home_assistant(
                session,
                config=config,
                states=states,
                service_domains=service_domains,
                url=adapter.config.url,
                when=now,
                service_name=adapter.config.service_name,
                resolver=adapter.config.resolver,
            )
        )
    if len(results) == 1:
        return results[0].as_dict()
    return {
        **results[0].as_dict(),
        "instances": [r.as_dict() for r in results],
    }


async def _discover_mikrotik(session: AsyncSession) -> dict[str, Any]:
    adapter = factories.mikrotik()
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


async def _ceph_version_issues(cluster: str) -> tuple[list[CategoryIssue], set[str], str | None]:
    """Ceph EOL / mixed-version checks; a cluster without Ceph observes nothing."""
    try:
        adapter = factories.proxmox()
        try:
            metadata = await adapter.ceph_metadata()
        finally:
            await adapter.aclose()
    except Exception as exc:
        return [], set(), redact(str(exc))
    issues, observed = ceph_issues(cluster, metadata, load_eol_table(), datetime.now(UTC).date())
    return issues, observed, None


def _host_version_issues(
    hosts: list[tuple[str, str, dict[str, Any]]],
) -> tuple[list[CategoryIssue], set[str]]:
    """OS end of support, kubelet/Talos skew, Kubernetes/Talos end of support."""
    eol_table, today = load_eol_table(), datetime.now(UTC).date()
    issues: list[CategoryIssue] = list(os_eol_issues(hosts, eol_table, today))
    observed = {"os-eol"}
    for extra, seen in (k8s_issues(hosts), k8s_eol_issues(hosts, eol_table, today)):
        issues += extra
        observed |= seen
    return issues, observed


async def _discover_versions(session: AsyncSession) -> dict[str, Any]:
    """Phase 8.1: version currency from Proxmox, stored host facts and Home Assistant.

    Each source that fails is reported and contributes no categories, so its
    findings neither open nor resolve this run.
    """
    issues: list[CategoryIssue] = []
    observed: set[str] = set()
    errors: dict[str, str] = {}
    cluster_name = "proxmox"

    try:
        adapter = factories.proxmox()
        try:
            status = await adapter.cluster_status()
            cluster_name = str(status.get("name") or cluster_name)
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
        issues += proxmox_issues(cluster_name, nodes)
        observed |= {"pve-updates", "pve-mixed"}
    except Exception as exc:  # one dead source must not sink the others
        errors["proxmox"] = redact(str(exc))

    ceph, ceph_observed, ceph_error = await _ceph_version_issues(cluster_name)
    issues += ceph
    observed |= ceph_observed
    if ceph_error:
        errors["ceph"] = ceph_error

    retired = await retired_host_ids(session)
    hosts = [
        (str(h.id), h.hostname, dict(h.capabilities or {}))
        for h in (await session.execute(select(Host))).scalars().all()
        if h.id not in retired
    ]
    host_issues, host_observed = _host_version_issues(hosts)
    issues += host_issues
    observed |= host_observed

    img_issues, img_observed = workload_image_issues(await persisted_k8s_workloads(session))
    issues += img_issues
    observed |= img_observed

    try:
        for hass in factories.hass_all():
            try:
                states = await hass.list_states()
            finally:
                await hass.aclose()
            issues += hass_update_issues(hass.config.service_name, states)
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
    """Phase 8.2 / 13.1: backup posture from Proxmox, Home Assistant off-site sensors, and declared targets."""
    adapter = factories.proxmox()
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
    drills = await restore_drill_timestamps(session)
    observed = set(BACKUP_CATEGORIES)
    issues = (
        backup_issues(guests, jobs, backups, now=now, verified_drills=drills)
        + orphans
        + capacity_issues(storages)
    )
    target_issues, target_observed = declared_target_issues(
        load_backup_targets(),
        now=now,
        observed_runs=await backup_target_timestamps(session),
        verified_drills=drills,
    )
    issues += target_issues
    observed |= target_observed
    with contextlib.suppress(Exception):
        for hass in factories.hass_all():
            try:
                states = await hass.list_states()
            finally:
                await hass.aclose()
            h_issues, h_observed = hass_backup_issues(hass.config.service_name, states, now=now)
            issues += h_issues
            observed |= h_observed
    result = await reconcile_backup_findings(session, issues, observed, when=now)
    return {
        "guests": len(guests),
        "jobs": len(jobs),
        "backup_storages": [s["storage"] for s in storages],
        "backups": len(backups),
        "issues": len(issues),
        "findings": result.counts(),
    }


async def _discover_suggestions(session: AsyncSession) -> dict[str, Any]:
    """Phase 8.7 / 12.1: capability the lab owns but does not use, from stored facts only.

    Reads the harness DB and the workload library — no adapter calls, so it
    cannot fail for a source being down and all categories are always
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
    k8s_workloads = await persisted_k8s_workloads(session)
    w_names = [str(w["name"]) for w in k8s_workloads if w.get("name")]
    w_images = image_names(
        img for w in k8s_workloads for img in (w.get("images") or []) if isinstance(img, str)
    )
    h_units = unit_names(
        u for _hid, _hname, caps in hosts for u in (caps.get("running_services") or [])
    )
    present = present_names(guests, services, endpoints, w_names, w_images, h_units)

    library = load_workload_library()
    issues = (
        idle_gpu_issues(hosts, library, present)
        + building_block_issues(library, present)
        + missing_dependency_issues(library, present)
    )
    result = await reconcile_category_findings(
        session,
        FindingKind.SERVICE_SUGGESTION,
        issues,
        {"capability-idle-gpu", "building-block-missing", "dependency-missing"},
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
    gate = asyncio.Semaphore(PROXMOX_CONCURRENCY)

    async def facts(vm: dict[str, Any]) -> dict[str, Any] | None:
        node, vmid, kind = vm.get("node"), vm.get("vmid"), vm.get("type")
        if not (node and vmid and kind):
            return None
        async with gate:
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
        return entry

    found = await asyncio.gather(*(facts(vm) for vm in await adapter.list_vms()))
    guests = [g for g in found if g is not None]

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
        adapter = factories.proxmox()
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
        k8s = factories.k8s()
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
    adapter = factories.proxmox()
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
    workloads = await _discover_workload_usage(session)
    pruned = await prune_usage(session) | await prune_history(session)
    return {
        "subjects": len(subjects) + workloads.get("subjects", 0),
        "with_data": with_data + workloads.get("subjects", 0),
        "samples": {"inserted": inserted, "updated": updated},
        "workloads": workloads,
        "pruned": pruned,
    }


async def _discover_workload_usage(session: AsyncSession) -> dict[str, Any]:
    """Phase 11.4: one hourly sample per Kubernetes workload from metrics-server.
    Skipped without kubectl; a cluster without metrics-server reports why."""
    if not shutil.which("kubectl"):
        return {"skipped": "kubectl not on PATH"}
    k8s = factories.k8s()
    try:
        rows = workload_usage(await k8s.list_pods(), await k8s.pod_metrics())
    except KubeError as exc:
        return {"error": str(exc)[:300]}
    w = await record_workload_usage(session, rows)
    return {"subjects": len(rows), "samples": {"inserted": w.inserted, "updated": w.updated}}


async def _discover_capacity(session: AsyncSession) -> dict[str, Any]:
    """Phase 10.2: filesystem, memory and CPU trends from the usage history. No source
    is read — the history is the source — so an empty history simply observes nothing."""
    result, issues = await reconcile_capacity_findings(session, now=datetime.now(UTC))
    return {
        "issues": [
            {"category": i.category, "severity": i.severity.value, "title": i.title} for i in issues
        ],
        "findings": result.counts(),
    }


async def _discover_anomalies(session: AsyncSession) -> dict[str, Any]:
    """Phase 10.3: guests and nodes outside their own usage baseline. The history is
    the source; a subject with too little or too stale history is not judged."""
    result, issues = await reconcile_anomaly_findings(session, now=datetime.now(UTC))
    return {
        "issues": [
            {"category": i.category, "severity": i.severity.value, "title": i.title} for i in issues
        ],
        "findings": result.counts(),
    }


DISCOVERERS: dict[str, Callable[[AsyncSession], Awaitable[dict[str, Any]]]] = {
    "unifi": _discover_unifi,
    "cloudflare": _discover_cloudflare,
    "argocd": _discover_argocd,
    "proxmox": _discover_proxmox,
    "k8s": _discover_k8s,
    "omv": _discover_omv,
    "hass": _discover_hass,
    "mikrotik": _discover_mikrotik,
    "printwarden": _discover_printwarden,
    "jetkvm": _discover_jetkvm,
    "versions": _discover_versions,
    "backups": _discover_backups,
    "usage": _discover_usage,
    "suggestions": _discover_suggestions,
    "storage": _discover_storage,
    "capacity": _discover_capacity,
    "anomalies": _discover_anomalies,
}


def _describe(exc: BaseException) -> str:
    """str(exc), or the class name when that is empty (httpx timeouts are)."""
    return str(exc) or type(exc).__name__


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
    discoverer = DISCOVERERS.get(source)
    if discoverer is None:
        return {"error": f"unknown source {source!r}; expected one of {sorted(DISCOVERERS)}"}
    engine = make_engine(database_url())
    started = datetime.now(UTC)
    try:
        sm = make_sessionmaker(engine)
        try:
            async with session_scope(sm) as session:
                result = await discoverer(session)
            out = {"source": source, **result}
        except Exception as exc:  # surface adapter/config errors as data, not protocol faults
            out = {"source": source, "error": redact(_describe(exc))}
        # Its own transaction: a discovery that rolled back is still a run that
        # happened. Best effort: a record that cannot be written (a database not
        # yet migrated) must not turn the discovery's own answer into an error.
        error = out.get("error")
        with contextlib.suppress(Exception):
            async with session_scope(sm) as session:
                session.add(
                    SourceRun(
                        source=source,
                        started_at=started,
                        ended_at=datetime.now(UTC),
                        ok=error is None,
                        error=str(error)[:2000] if error is not None else None,
                    )
                )
        return out
    finally:
        await engine.dispose()


__all__ = ["DISCOVERERS", "run_discovery"]
