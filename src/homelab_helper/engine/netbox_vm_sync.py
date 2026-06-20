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
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.adapters.netbox import _vm_matches, _vm_to_netbox_payload
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding, VirtualMachine
from homelab_helper.engine.fingerprint import make_fingerprint

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.adapters.netbox import NetBoxAdapter
    from homelab_helper.db.models import Cluster

_BASELINE_KEY = "netbox_baseline"
_SYNCED_FIELDS = ("status", "vcpus", "memory")


@dataclass
class NetBoxVMSyncResult:
    cluster_name: str
    found: bool = True
    reason: str | None = None
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    diverged: list[str] = field(default_factory=list)

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


def _nb_state(row: dict[str, Any]) -> dict[str, Any]:
    """The harness-owned fields of a NetBox VM, normalized for comparison."""
    status = row.get("status")
    if isinstance(status, dict):
        status = status.get("value")
    vcpus = row.get("vcpus")
    return {
        "status": status,
        "vcpus": float(vcpus) if vcpus is not None else None,
        "memory": row.get("memory"),
    }


def _payload_state(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: payload.get(k) for k in _SYNCED_FIELDS}


def _divergence_fp(vm: VirtualMachine) -> str:
    return make_fingerprint(
        FindingKind.NETBOX_DIVERGENCE.value, "vm", str(vm.id), "netbox-vm-hand-edit"
    )


async def _upsert_divergence(
    session: AsyncSession,
    vm: VirtualMachine,
    current: dict[str, Any],
    baseline: dict[str, Any],
    when: datetime,
) -> None:
    """Raise/refresh a NETBOX_DIVERGENCE finding; the write was skipped."""
    fp = _divergence_fp(vm)
    title = f"NetBox VM {vm.name} was hand-edited"
    description = (
        f"NetBox now shows {current} for {vm.name}, but the harness last wrote "
        f"{baseline}. Someone changed it in NetBox; skipping the harness write to "
        "avoid clobbering the operator's edit. Reconcile by hand, then re-sync."
    )
    existing = (
        await session.execute(
            select(ReconciliationFinding).where(ReconciliationFinding.fingerprint == fp)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status is FindingStatus.RESOLVED:
            existing.status = FindingStatus.OPEN
            existing.resolved_at = None
            existing.first_seen = when
        existing.last_seen = when
        existing.title = title
        existing.description = description
        return
    session.add(
        ReconciliationFinding(
            kind=FindingKind.NETBOX_DIVERGENCE,
            severity=FindingSeverity.MEDIUM,
            fingerprint=fp,
            title=title,
            description=description,
            affected=[{"type": "vm", "id": str(vm.id)}],
            evidence_refs=[{"type": "netbox_vm", "id": str(vm.netbox_vm_id)}],
            status=FindingStatus.OPEN,
            first_seen=when,
            last_seen=when,
        )
    )


async def _resolve_divergence(session: AsyncSession, vm: VirtualMachine, when: datetime) -> None:
    """Clear a divergence finding once NetBox is back in agreement."""
    existing = (
        await session.execute(
            select(ReconciliationFinding).where(
                ReconciliationFinding.fingerprint == _divergence_fp(vm)
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status in (FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED):
        existing.status = FindingStatus.RESOLVED
        existing.resolved_at = when


def _set_baseline(vm: VirtualMachine, state: dict[str, Any]) -> None:
    vm.attributes = {**(vm.attributes or {}), _BASELINE_KEY: state}


async def sync_cluster_to_netbox(
    session: AsyncSession,
    netbox: NetBoxAdapter,
    cluster: Cluster,
    *,
    when: datetime,
    dry_run: bool = False,
) -> NetBoxVMSyncResult:
    """Upsert a harness cluster's VMs into NetBox; write NetBox ids back.

    Three-way merge against a per-VM ``netbox_baseline`` (the harness's last
    write): if NetBox's current state diverges from that baseline, an operator
    hand-edited it — the harness skips the write and raises a
    ``NETBOX_DIVERGENCE`` finding rather than clobber the change.
    """
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
        desired = _payload_state(payload)
        match = (by_id.get(vm.netbox_vm_id) if vm.netbox_vm_id else None) or by_name.get(vm.name)

        if match is None:
            if not dry_run:
                created = await netbox.create_virtual_machine(payload)
                vm.netbox_vm_id = created.get("id")
                _set_baseline(vm, desired)
            result.created.append(vm.name)
            continue

        vm.netbox_vm_id = match.get("id")  # write-back even when matched by name
        current = _nb_state(match)
        baseline = (vm.attributes or {}).get(_BASELINE_KEY)

        if baseline is not None and current != baseline:
            # NetBox changed since our last write — operator hand-edited it.
            result.diverged.append(vm.name)
            if not dry_run:
                await _upsert_divergence(session, vm, current, baseline, when)
            continue

        if not dry_run:
            await _resolve_divergence(session, vm, when)  # clear any stale divergence
        if _vm_matches(match, payload):
            result.unchanged.append(vm.name)
        else:
            if not dry_run:
                await netbox.update_virtual_machine(match["id"], payload)
            result.updated.append(vm.name)
        if not dry_run:
            _set_baseline(vm, desired)

    await session.flush()
    return result


__all__ = ["NetBoxVMSyncResult", "sync_cluster_to_netbox"]
