"""Virtualization reconcile — Proxmox discovery → Cluster + VirtualMachine rows.

Upserts the harness-side projection of a hypervisor's cluster + guests. Idempotent:
the cluster is keyed by name (a standalone node by its node name, since each
standalone node is its own VMID namespace), each VM by ``(cluster, vmid)``. A guest's node is
resolved to a ``Host`` row when that node is already known, so VM placement can
be reasoned about against hardware. Discovery is read-only at the source (L1):
this only writes harness rows from what the adapter already read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import DiscoverySource
from homelab_helper.db.models import Cluster, Host, VirtualMachine

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.adapters.proxmox import ProxmoxAdapter

# VM fields compared to decide created/updated/unchanged.
_VM_FIELDS = (
    "name",
    "kind",
    "status",
    "template",
    "node_name",
    "vcpus",
    "memory_bytes",
    "disk_bytes",
)


@dataclass
class VirtReconcileResult:
    cluster_name: str
    cluster_created: bool = False
    vms_created: list[str] = field(default_factory=list)
    vms_updated: list[str] = field(default_factory=list)
    vms_unchanged: list[str] = field(default_factory=list)
    vms_adopted: list[str] = field(default_factory=list)
    legacy_cluster_removed: bool = False


LEGACY_STANDALONE = "(standalone)"


_DISK_KEY = re.compile(r"^(scsi|virtio|ide|sata|efidisk|tpmstate|unused|rootfs|mp)\d*$")


def disk_storages(config: dict[str, Any]) -> list[str]:
    """Storage names a guest's disks (and mounted ISOs) live on, from its config.

    Every ``storage:volume,...`` disk entry counts, a CD-ROM ISO included: a
    guest with any volume on a node-local storage cannot live-migrate.
    """
    names: set[str] = set()
    for key, value in config.items():
        if not _DISK_KEY.match(str(key)) or not isinstance(value, str):
            continue
        volume = value.split(",", 1)[0]
        if volume in ("none", "") or ":" not in volume:
            continue
        names.add(volume.split(":", 1)[0])
    return sorted(names)


async def annotate_guest_storage(adapter: ProxmoxAdapter, vms: list[dict[str, Any]]) -> None:
    """Add ``storages`` and ``shared_storage`` to each discovered guest in place.

    ``shared_storage`` is True when every storage a guest's volumes live on is
    cluster-shared (Ceph, NFS, ...), False when any is node-local, None when
    the guest has no volumes or a config could not be read. The rebalance
    planner refuses to propose migrating a guest that is not on shared storage.
    """
    shared: dict[str, bool] = {}
    for row in await adapter.list_storage():
        name = str(row.get("storage") or "")
        if name:
            shared[name] = shared.get(name, False) or bool(row.get("shared"))
    for vm in vms:
        node, vmid, kind = vm.get("node"), vm.get("vmid"), vm.get("type") or "qemu"
        if not node or vmid is None:
            continue
        try:
            config = await adapter.vm_config(str(node), int(vmid), str(kind))
        except Exception:
            continue
        storages = disk_storages(config)
        vm["storages"] = storages
        vm["shared_storage"] = all(shared.get(s, False) for s in storages) if storages else None


def _vm_attributes(vm: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    attrs = dict(current or {})
    if "storages" in vm:
        attrs["storages"] = list(vm["storages"])
        attrs["shared_storage"] = vm.get("shared_storage")
    return attrs


def _vm_fields_from_discovery(vm: dict[str, Any]) -> dict[str, Any]:
    """Map a Proxmox-shaped VM dict onto VirtualMachine column values."""
    return {
        "name": vm.get("name") or f"vmid-{vm.get('vmid')}",
        "kind": vm.get("type") or "qemu",
        "status": vm.get("status"),
        "template": bool(vm.get("template")),
        "node_name": vm.get("node"),
        "vcpus": vm.get("maxcpu") if isinstance(vm.get("maxcpu"), int) else None,
        "memory_bytes": vm.get("maxmem_bytes") if isinstance(vm.get("maxmem_bytes"), int) else None,
        "disk_bytes": vm.get("maxdisk_bytes") if isinstance(vm.get("maxdisk_bytes"), int) else None,
    }


def standalone_cluster_name(cluster_status: dict[str, Any], vms: list[dict[str, Any]]) -> str:
    """Name for a node with no cluster row. Each standalone node is its own VMID
    namespace, so the node name must be part of the key or two nodes collide."""
    nodes = {n.get("name") for n in cluster_status.get("nodes") or [] if n.get("name")}
    nodes |= {v.get("node") for v in vms if v.get("node")}
    if len(nodes) == 1:
        return f"{LEGACY_STANDALONE} {nodes.pop()}"
    return LEGACY_STANDALONE


async def _adopt_legacy_standalone(
    session: AsyncSession, cluster: Cluster, node: str, result: VirtReconcileResult
) -> None:
    """Move this node's guests out of the pre-fix shared ``(standalone)`` row.

    Databases written before standalone nodes were keyed by node name hold every
    standalone guest under one cluster. Guests whose ``node_name`` is this node are
    re-parented so they update in place instead of reappearing as duplicates; guests
    of other nodes stay for their own discovery to adopt. The legacy row goes away
    once it is empty."""
    legacy = (
        await session.execute(select(Cluster).where(Cluster.name == LEGACY_STANDALONE))
    ).scalar_one_or_none()
    if legacy is None or legacy.id == cluster.id:
        return
    rows = (
        (
            await session.execute(
                select(VirtualMachine).where(VirtualMachine.cluster_id == legacy.id)
            )
        )
        .scalars()
        .all()
    )
    remaining = 0
    for vm in rows:
        if vm.node_name == node:
            vm.cluster_id = cluster.id
            result.vms_adopted.append(vm.name)
        else:
            remaining += 1
    await session.flush()
    if remaining == 0:
        await session.delete(legacy)
        result.legacy_cluster_removed = True
        await session.flush()


async def reconcile_proxmox_cluster(
    session: AsyncSession,
    cluster_status: dict[str, Any],
    vms: list[dict[str, Any]],
    *,
    when: datetime,
    kind: str = "proxmox",
) -> VirtReconcileResult:
    """Upsert a Cluster + its VirtualMachine rows from Proxmox discovery."""
    cluster_name = cluster_status.get("name") or standalone_cluster_name(cluster_status, vms)
    result = VirtReconcileResult(cluster_name=cluster_name)

    cluster = (
        await session.execute(select(Cluster).where(Cluster.name == cluster_name))
    ).scalar_one_or_none()
    if cluster is None:
        cluster = Cluster(name=cluster_name, kind=kind, discovery_source=DiscoverySource.PROXMOX)
        session.add(cluster)
        result.cluster_created = True
    cluster.quorate = cluster_status.get("quorate")
    cluster.node_count = cluster_status.get("node_count")
    members = sorted(
        {str(n["name"]) for n in cluster_status.get("nodes") or [] if n.get("name")}
        | {str(v["node"]) for v in vms if v.get("node")}
    )
    if members:
        cluster.attributes = {**(cluster.attributes or {}), "nodes": members}
    cluster.discovery_last_run = when
    await session.flush()

    if cluster_name.startswith(f"{LEGACY_STANDALONE} "):
        node = cluster_name[len(LEGACY_STANDALONE) + 1 :]
        await _adopt_legacy_standalone(session, cluster, node, result)

    # Node-name → Host resolution, one query for the whole batch.
    node_names = {v.get("node") for v in vms if v.get("node")}
    host_by_name: dict[str, Host] = {}
    if node_names:
        rows = (
            (await session.execute(select(Host).where(Host.hostname.in_(node_names))))
            .scalars()
            .all()
        )
        host_by_name = {h.hostname: h for h in rows}

    existing_rows = (
        (
            await session.execute(
                select(VirtualMachine).where(VirtualMachine.cluster_id == cluster.id)
            )
        )
        .scalars()
        .all()
    )
    existing = {vm.vmid: vm for vm in existing_rows if vm.vmid is not None}

    for vm in vms:
        vmid = vm.get("vmid")
        fields = _vm_fields_from_discovery(vm)
        node_host = host_by_name.get(fields["node_name"]) if fields["node_name"] else None
        fields["node_host_id"] = node_host.id if node_host is not None else None

        row = existing.get(vmid) if vmid is not None else None
        if row is None:
            session.add(
                VirtualMachine(
                    cluster_id=cluster.id,
                    vmid=vmid,
                    discovery_source=DiscoverySource.PROXMOX,
                    attributes=_vm_attributes(vm, None),
                    **fields,
                )
            )
            result.vms_created.append(fields["name"])
            continue

        attrs = _vm_attributes(vm, row.attributes)
        changed = any(getattr(row, f) != fields[f] for f in (*_VM_FIELDS, "node_host_id"))
        if not changed and attrs == dict(row.attributes or {}):
            result.vms_unchanged.append(fields["name"])
            continue
        for f, value in fields.items():
            setattr(row, f, value)
        row.attributes = attrs  # JSON column: reassign, never mutate in place
        result.vms_updated.append(fields["name"])

    await session.flush()
    return result


__all__ = [
    "LEGACY_STANDALONE",
    "VirtReconcileResult",
    "annotate_guest_storage",
    "disk_storages",
    "reconcile_proxmox_cluster",
    "standalone_cluster_name",
]
