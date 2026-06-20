"""Tests for the Proxmox -> harness virtualization reconcile."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.models import Cluster, Host, VirtualMachine
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.virt_reconcile import reconcile_proxmox_cluster

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
