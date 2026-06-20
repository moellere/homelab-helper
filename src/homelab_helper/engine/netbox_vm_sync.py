"""NetBox VM sync — push harness VirtualMachine rows into NetBox, write IDs back.

Completes the Proxmox → harness → NetBox round-trip. ``discover proxmox
--persist`` populated ``Cluster`` + ``VirtualMachine`` rows; this reads those
rows (the harness's tracked truth, not a transient discovery list) and upserts
them into an *existing* NetBox cluster, then writes the resulting
``netbox_cluster_id`` / ``netbox_vm_id`` back onto the harness rows.

The write-back is what makes re-syncs robust: a VM is matched by its NetBox id
once known (rename-safe), falling back to name only on first contact. Create +
update only — operator-entered VMs are never reaped. NetBox owns cluster
topology, so a missing cluster returns ``found=False`` (operator creates it).
Templates are skipped (NetBox doesn't model them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from homelab_helper.adapters.netbox import _vm_matches, _vm_to_netbox_payload
from homelab_helper.db.models import VirtualMachine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.adapters.netbox import NetBoxAdapter
    from homelab_helper.db.models import Cluster


@dataclass
class NetBoxVMSyncResult:
    cluster_name: str
    found: bool = True
    reason: str | None = None
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.created) + len(self.updated)


def _row_payload(vm: VirtualMachine, cluster_id: int) -> dict[str, object]:
    """Reuse the adapter's discovery→NetBox mapping from a harness row."""
    return _vm_to_netbox_payload(
        {
            "name": vm.name,
            "status": vm.status,
            "maxcpu": vm.vcpus,
            "maxmem_bytes": vm.memory_bytes,
        },
        cluster_id,
    )


async def sync_cluster_to_netbox(
    session: AsyncSession,
    netbox: NetBoxAdapter,
    cluster: Cluster,
    *,
    dry_run: bool = False,
) -> NetBoxVMSyncResult:
    """Upsert a harness cluster's VMs into NetBox; write NetBox ids back."""
    result = NetBoxVMSyncResult(cluster_name=cluster.name)
    nb_cluster = await netbox.get_cluster_by_name(cluster.name)
    if nb_cluster is None:
        result.found = False
        result.reason = f"cluster {cluster.name!r} not found in NetBox — create it, then re-run"
        return result
    cluster_id = nb_cluster["id"]
    cluster.netbox_cluster_id = cluster_id  # write-back

    existing = await netbox.list_virtual_machines(cluster_id=cluster_id)
    by_id = {r["id"]: r for r in existing if isinstance(r.get("id"), int)}
    by_name = {r["name"]: r for r in existing if isinstance(r.get("name"), str)}

    vms = (
        (
            await session.execute(
                select(VirtualMachine).where(VirtualMachine.cluster_id == cluster.id)
            )
        )
        .scalars()
        .all()
    )
    for vm in vms:
        if vm.template:
            continue  # NetBox doesn't model templates
        payload = _row_payload(vm, cluster_id)
        match = (by_id.get(vm.netbox_vm_id) if vm.netbox_vm_id else None) or by_name.get(vm.name)
        if match is None:
            if not dry_run:
                created = await netbox.create_virtual_machine(payload)
                vm.netbox_vm_id = created.get("id")
            result.created.append(vm.name)
            continue
        vm.netbox_vm_id = match.get("id")  # write-back even when matched by name
        if _vm_matches(match, payload):
            result.unchanged.append(vm.name)
        elif not dry_run:
            await netbox.update_virtual_machine(match["id"], payload)
            result.updated.append(vm.name)
        else:
            result.updated.append(vm.name)

    await session.flush()
    return result


__all__ = ["NetBoxVMSyncResult", "sync_cluster_to_netbox"]
