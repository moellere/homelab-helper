"""Virtualization reconcile — Proxmox discovery → Cluster + VirtualMachine rows.

Upserts the harness-side projection of a hypervisor's cluster + guests. Idempotent:
the cluster is keyed by name, each VM by ``(cluster, vmid)``. A guest's node is
resolved to a ``Host`` row when that node is already known, so VM placement can
be reasoned about against hardware. Discovery is read-only at the source (L1):
this only writes harness rows from what the adapter already read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import DiscoverySource
from homelab_helper.db.models import Cluster, Host, VirtualMachine

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

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


async def reconcile_proxmox_cluster(
    session: AsyncSession,
    cluster_status: dict[str, Any],
    vms: list[dict[str, Any]],
    *,
    when: datetime,
    kind: str = "proxmox",
) -> VirtReconcileResult:
    """Upsert a Cluster + its VirtualMachine rows from Proxmox discovery."""
    cluster_name = cluster_status.get("name") or "(standalone)"
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
    cluster.discovery_last_run = when
    await session.flush()

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
                    **fields,
                )
            )
            result.vms_created.append(fields["name"])
            continue

        changed = any(getattr(row, f) != fields[f] for f in (*_VM_FIELDS, "node_host_id"))
        if not changed:
            result.vms_unchanged.append(fields["name"])
            continue
        for f, value in fields.items():
            setattr(row, f, value)
        result.vms_updated.append(fields["name"])

    await session.flush()
    return result


__all__ = ["VirtReconcileResult", "reconcile_proxmox_cluster"]
