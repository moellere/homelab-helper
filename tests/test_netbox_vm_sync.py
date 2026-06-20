"""Tests for harness-row → NetBox VM sync + id write-back.

A stateful in-memory NetBox backend (over MockTransport) lets us assert the full
round-trip: create on first sync, ``netbox_vm_id`` written back, match-by-id and
``unchanged`` on the second sync.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from homelab_helper.adapters.netbox import NetBoxAdapter, NetBoxConfig
from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingStatus
from homelab_helper.db.models import Cluster, ReconciliationFinding, VirtualMachine
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.netbox_vm_sync import sync_cluster_to_netbox

_WHEN = datetime(2026, 6, 20, tzinfo=UTC)


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


class _FakeNetBox:
    """Minimal stateful NetBox VM/cluster backend behind a MockTransport."""

    def __init__(self, *, cluster_exists: bool = True) -> None:
        self.cluster_exists = cluster_exists
        self.vms: dict[int, dict] = {}
        self._next_id = 100

    def adapter(self) -> NetBoxAdapter:
        client = httpx.AsyncClient(
            base_url="http://netbox.example/", transport=httpx.MockTransport(self._handle)
        )
        return NetBoxAdapter(NetBoxConfig(url="http://netbox.example/", token="t"), client=client)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/virtualization/clusters/":
            rows = [{"id": 5, "name": "lab"}] if self.cluster_exists else []
            return httpx.Response(200, json={"results": rows, "next": None})
        if path == "/api/virtualization/virtual-machines/" and request.method == "GET":
            return httpx.Response(200, json={"results": list(self.vms.values()), "next": None})
        if request.method == "POST":
            body = json.loads(request.content)
            vm = {
                "id": self._next_id,
                "name": body["name"],
                "status": {"value": body["status"]},
                "vcpus": body.get("vcpus"),
                "memory": body.get("memory"),
            }
            self.vms[self._next_id] = vm
            self._next_id += 1
            return httpx.Response(201, json=vm)
        if request.method == "PATCH":
            vm_id = int(path.rstrip("/").rsplit("/", 1)[-1])
            body = json.loads(request.content)
            self.vms[vm_id].update(
                {
                    "status": {"value": body["status"]},
                    "vcpus": body.get("vcpus"),
                    "memory": body.get("memory"),
                }
            )
            return httpx.Response(200, json=self.vms[vm_id])
        return httpx.Response(404, json={})


async def _seed_cluster(sm) -> None:
    async with session_scope(sm) as s:
        c = Cluster(name="lab", kind="proxmox", node_count=1)
        s.add(c)
        await s.flush()
        s.add(
            VirtualMachine(
                cluster_id=c.id,
                vmid=100,
                name="web",
                kind="qemu",
                status="running",
                vcpus=2,
                memory_bytes=4294967296,
            )
        )
        s.add(
            VirtualMachine(
                cluster_id=c.id, vmid=200, name="tmpl", kind="qemu", status="stopped", template=True
            )
        )


async def test_sync_creates_and_writes_back_id_then_idempotent(sessionmaker) -> None:
    await _seed_cluster(sessionmaker)
    nb = _FakeNetBox()

    async with session_scope(sessionmaker) as s:
        cluster = (await s.execute(select(Cluster))).scalar_one()
        r1 = await sync_cluster_to_netbox(s, nb.adapter(), cluster, when=_WHEN)

    # Template skipped; only "web" synced.
    assert r1.created == ["web"]
    async with sessionmaker() as s:
        cluster = (await s.execute(select(Cluster))).scalar_one()
        assert cluster.netbox_cluster_id == 5  # written back
        web = (
            await s.execute(select(VirtualMachine).where(VirtualMachine.name == "web"))
        ).scalar_one()
        assert web.netbox_vm_id == 100  # written back from create

    # Second sync: matched by id, no change.
    async with session_scope(sessionmaker) as s:
        cluster = (await s.execute(select(Cluster))).scalar_one()
        r2 = await sync_cluster_to_netbox(s, nb.adapter(), cluster, when=_WHEN)
    assert r2.unchanged == ["web"]
    assert r2.created == []


async def test_sync_missing_cluster_found_false(sessionmaker) -> None:
    await _seed_cluster(sessionmaker)
    nb = _FakeNetBox(cluster_exists=False)
    async with session_scope(sessionmaker) as s:
        cluster = (await s.execute(select(Cluster))).scalar_one()
        result = await sync_cluster_to_netbox(s, nb.adapter(), cluster, when=_WHEN)
    assert result.found is False
    assert "not found" in (result.reason or "")


async def test_dry_run_updates_reported_but_no_post(sessionmaker) -> None:
    await _seed_cluster(sessionmaker)
    nb = _FakeNetBox()
    async with session_scope(sessionmaker) as s:
        cluster = (await s.execute(select(Cluster))).scalar_one()
        result = await sync_cluster_to_netbox(s, nb.adapter(), cluster, when=_WHEN, dry_run=True)
    assert result.created == ["web"]
    assert nb.vms == {}  # nothing actually created in NetBox


async def _first_sync(sessionmaker, nb) -> None:
    async with session_scope(sessionmaker) as s:
        cluster = (await s.execute(select(Cluster))).scalar_one()
        await sync_cluster_to_netbox(s, nb.adapter(), cluster, when=_WHEN)


async def test_hand_edit_diverges_skips_write_and_flags(sessionmaker) -> None:
    await _seed_cluster(sessionmaker)
    nb = _FakeNetBox()
    await _first_sync(sessionmaker, nb)  # creates web, records baseline

    # Operator hand-edits the VM in NetBox after our last sync.
    nb.vms[100]["status"] = {"value": "offline"}

    async with session_scope(sessionmaker) as s:
        cluster = (await s.execute(select(Cluster))).scalar_one()
        result = await sync_cluster_to_netbox(s, nb.adapter(), cluster, when=_WHEN)

    assert result.diverged == ["web"]
    assert result.updated == []  # the write was skipped
    assert nb.vms[100]["status"] == {"value": "offline"}  # NetBox not clobbered
    async with sessionmaker() as s:
        f = (await s.execute(select(ReconciliationFinding))).scalar_one()
        assert f.kind is FindingKind.NETBOX_DIVERGENCE
        assert f.status is FindingStatus.OPEN


async def test_divergence_resolves_when_netbox_back_in_sync(sessionmaker) -> None:
    await _seed_cluster(sessionmaker)
    nb = _FakeNetBox()
    await _first_sync(sessionmaker, nb)
    nb.vms[100]["status"] = {"value": "offline"}  # hand-edit -> diverge
    async with session_scope(sessionmaker) as s:
        cluster = (await s.execute(select(Cluster))).scalar_one()
        await sync_cluster_to_netbox(s, nb.adapter(), cluster, when=_WHEN)

    # Operator reverts NetBox back to the harness baseline.
    nb.vms[100]["status"] = {"value": "active"}
    async with session_scope(sessionmaker) as s:
        cluster = (await s.execute(select(Cluster))).scalar_one()
        result = await sync_cluster_to_netbox(s, nb.adapter(), cluster, when=_WHEN)

    assert result.diverged == []
    async with sessionmaker() as s:
        f = (await s.execute(select(ReconciliationFinding))).scalar_one()
        assert f.status is FindingStatus.RESOLVED
