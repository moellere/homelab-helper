"""Tests for the Proxmox -> harness virtualization reconcile."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import DiscoverySource
from homelab_helper.db.models import Cluster, Host, VirtualMachine
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.virt_reconcile import (
    reconcile_proxmox_cluster,
    standalone_cluster_name,
)

_WHEN = datetime(2026, 6, 20, tzinfo=UTC)
_STATUS = {"name": "lab", "quorate": True, "node_count": 2, "nodes": []}
_VMS = [
    {
        "vmid": 100,
        "name": "web",
        "node": "pve1",
        "type": "qemu",
        "status": "running",
        "template": False,
        "maxcpu": 2,
        "maxmem_bytes": 4294967296,
        "maxdisk_bytes": 68719476736,
    },
    {
        "vmid": 200,
        "name": "ct",
        "node": "pve2",
        "type": "lxc",
        "status": "stopped",
        "template": False,
        "maxcpu": 1,
        "maxmem_bytes": 1073741824,
        "maxdisk_bytes": 8589934592,
    },
]


@pytest.fixture
async def engine():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine):
    return make_sessionmaker(engine)


async def test_reconcile_creates_cluster_and_vms_and_resolves_node(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(Host(hostname="pve1", primary_ip="192.0.2.1"))  # node pve1 is a known Host
    async with session_scope(sessionmaker) as s:
        result = await reconcile_proxmox_cluster(s, _STATUS, _VMS, when=_WHEN)

    assert result.cluster_created is True
    assert sorted(result.vms_created) == ["ct", "web"]

    async with sessionmaker() as s:
        cluster = (await s.execute(select(Cluster))).scalar_one()
        assert cluster.kind == "proxmox"
        assert cluster.node_count == 2
        vms = {v.name: v for v in (await s.execute(select(VirtualMachine))).scalars()}
        assert vms["web"].kind == "qemu"
        assert vms["web"].memory_bytes == 4294967296
        assert vms["web"].node_host_id is not None  # pve1 resolved to the Host
        assert vms["ct"].node_host_id is None  # pve2 unknown -> unresolved


async def test_reconcile_is_idempotent(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_proxmox_cluster(s, _STATUS, _VMS, when=_WHEN)
    async with session_scope(sessionmaker) as s:
        second = await reconcile_proxmox_cluster(s, _STATUS, _VMS, when=_WHEN)

    assert second.cluster_created is False
    assert sorted(second.vms_unchanged) == ["ct", "web"]
    assert second.vms_created == []
    async with sessionmaker() as s:
        assert len((await s.execute(select(VirtualMachine))).scalars().all()) == 2


async def test_reconcile_updates_changed_vm(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_proxmox_cluster(s, _STATUS, _VMS, when=_WHEN)

    moved = [dict(_VMS[0], status="stopped"), _VMS[1]]  # web: running -> stopped
    async with session_scope(sessionmaker) as s:
        result = await reconcile_proxmox_cluster(s, _STATUS, moved, when=_WHEN)

    assert result.vms_updated == ["web"]
    assert result.vms_unchanged == ["ct"]
    async with sessionmaker() as s:
        web = (
            await s.execute(select(VirtualMachine).where(VirtualMachine.name == "web"))
        ).scalar_one()
        assert web.status == "stopped"


def _standalone(node: str) -> dict[str, Any]:
    # Single-node installs have no `type == cluster` row, so the name is None.
    return {"name": None, "quorate": None, "node_count": 1, "nodes": [{"name": node}]}


def _vm(vmid: int, name: str, node: str) -> dict[str, Any]:
    return dict(_VMS[0], vmid=vmid, name=name, node=node)


async def test_standalone_nodes_do_not_collide_on_vmid(sessionmaker) -> None:
    node_a = [_vm(101, "nas", "pve-a"), _vm(102, "docker-b", "pve-a")]
    node_b = [_vm(101, "docker-a", "pve-b"), _vm(102, "photos", "pve-b")]

    async with session_scope(sessionmaker) as s:
        first = await reconcile_proxmox_cluster(s, _standalone("pve-a"), node_a, when=_WHEN)
    async with session_scope(sessionmaker) as s:
        second = await reconcile_proxmox_cluster(s, _standalone("pve-b"), node_b, when=_WHEN)

    assert first.cluster_name == "(standalone) pve-a"
    assert second.cluster_created is True
    assert sorted(second.vms_created) == ["docker-a", "photos"]
    async with sessionmaker() as s:
        vms = {
            (v.node_name, v.vmid): v.name
            for v in (await s.execute(select(VirtualMachine))).scalars()
        }
    assert vms == {
        ("pve-a", 101): "nas",
        ("pve-a", 102): "docker-b",
        ("pve-b", 101): "docker-a",
        ("pve-b", 102): "photos",
    }


def test_standalone_cluster_name_falls_back_without_a_single_node() -> None:
    assert standalone_cluster_name({"nodes": []}, []) == "(standalone)"
    assert standalone_cluster_name({"nodes": []}, [_vm(100, "x", "pve-a")]) == "(standalone) pve-a"


async def test_legacy_standalone_guests_are_adopted_per_node(sessionmaker) -> None:
    """A database written before per-node keys has one shared ``(standalone)`` row.
    Each node's next discovery must take its own guests out of it (updating them in
    place, not duplicating), leave the other node's guests alone, and drop the legacy
    row once it is empty."""
    async with session_scope(sessionmaker) as s:
        legacy = Cluster(
            name="(standalone)", kind="proxmox", discovery_source=DiscoverySource.PROXMOX
        )
        s.add(legacy)
        await s.flush()
        for vmid, name, node in ((101, "nas", "pve-a"), (103, "docker-a", "pve-b")):
            s.add(
                VirtualMachine(
                    cluster_id=legacy.id,
                    vmid=vmid,
                    name=name,
                    kind="qemu",
                    status="stopped",
                    node_name=node,
                    discovery_source=DiscoverySource.PROXMOX,
                )
            )

    async with session_scope(sessionmaker) as s:
        first = await reconcile_proxmox_cluster(
            s, _standalone("pve-a"), [_vm(101, "nas", "pve-a")], when=_WHEN
        )
    assert first.cluster_created is True
    assert first.vms_adopted == ["nas"]
    assert first.vms_updated == ["nas"]  # status flips from the seeded "stopped"
    assert first.vms_created == []
    assert first.legacy_cluster_removed is False  # pve-b's guest is still there

    async with session_scope(sessionmaker) as s:
        second = await reconcile_proxmox_cluster(
            s, _standalone("pve-b"), [_vm(103, "docker-a", "pve-b")], when=_WHEN
        )
    assert second.vms_adopted == ["docker-a"]
    assert second.legacy_cluster_removed is True

    async with sessionmaker() as s:
        clusters = sorted(c.name for c in (await s.execute(select(Cluster))).scalars())
        vms = sorted(
            (v.node_name, v.vmid, v.name)
            for v in (await s.execute(select(VirtualMachine))).scalars()
        )
    assert clusters == ["(standalone) pve-a", "(standalone) pve-b"]
    assert vms == [("pve-a", 101, "nas"), ("pve-b", 103, "docker-a")]


def test_disk_storages_reads_every_volume_including_the_iso() -> None:
    from homelab_helper.engine.virt_reconcile import disk_storages

    config = {
        "scsi0": "Pool0:vm-100-disk-0,iothread=1,size=32G",
        "ide2": "local:iso/talos.iso,media=cdrom",
        "efidisk0": "Pool0:vm-100-disk-1,efitype=4m",
        "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0",
        "scsi1": "none,media=cdrom",
        "rootfs": "local-lvm:vm-200-disk-0,size=8G",
        "mp0": "/mnt/host/path,mp=/data",
    }
    assert disk_storages(config) == ["Pool0", "local", "local-lvm"]


class _FakeProxmox:
    def __init__(self, configs: dict[int, dict[str, Any]], failing: set[int] = frozenset()):
        self.configs, self.failing = configs, failing

    async def list_storage(self) -> list[dict[str, Any]]:
        return [
            {"storage": "Pool0", "node": "pve-a", "shared": 1},
            {"storage": "Pool0", "node": "pve-b", "shared": 1},
            {"storage": "local", "node": "pve-a", "shared": 0},
        ]

    async def vm_config(self, node: str, vmid: int, kind: str) -> dict[str, Any]:
        if vmid in self.failing:
            raise RuntimeError("boom")
        return self.configs[vmid]


async def test_annotate_guest_storage_marks_shared_and_local(sessionmaker) -> None:
    from homelab_helper.engine.virt_reconcile import annotate_guest_storage

    vms = [
        _vm(101, "ceph-only", "pve-a"),
        _vm(102, "has-iso", "pve-a"),
        _vm(103, "unreadable", "pve-a"),
        _vm(104, "diskless", "pve-a"),
    ]
    fake = _FakeProxmox(
        {
            101: {"scsi0": "Pool0:vm-101-disk-0,size=8G"},
            102: {"scsi0": "Pool0:vm-102-disk-0,size=8G", "ide2": "local:iso/x.iso,media=cdrom"},
            104: {"net0": "virtio=..,bridge=vmbr0"},
        },
        failing={103},
    )
    await annotate_guest_storage(fake, vms)  # type: ignore[arg-type]
    assert vms[0]["shared_storage"] is True
    assert vms[0]["storages"] == ["Pool0"]
    assert vms[1]["shared_storage"] is False
    assert vms[1]["storages"] == ["Pool0", "local"]
    assert "storages" not in vms[2]  # unreadable config: left alone, discovery goes on
    assert vms[3]["shared_storage"] is None
    assert vms[3]["storages"] == []

    async with session_scope(sessionmaker) as s:
        await reconcile_proxmox_cluster(s, _STATUS, vms, when=_WHEN)
    async with sessionmaker() as s:
        rows = {
            r.name: r.attributes for r in (await s.execute(select(VirtualMachine))).scalars().all()
        }
    assert rows["has-iso"] == {"storages": ["Pool0", "local"], "shared_storage": False}
    assert rows["unreadable"] == {}
    # A second pass with the same answers changes nothing; a storage move is an update.
    async with session_scope(sessionmaker) as s:
        again = await reconcile_proxmox_cluster(s, _STATUS, vms, when=_WHEN)
        assert "has-iso" in again.vms_unchanged
        vms[1]["storages"], vms[1]["shared_storage"] = ["Pool0"], True
        moved = await reconcile_proxmox_cluster(s, _STATUS, vms, when=_WHEN)
        assert "has-iso" in moved.vms_updated
