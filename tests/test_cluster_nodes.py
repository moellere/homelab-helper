"""The one rule for "is this host a placement target" (Phase 9.2)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import Architecture
from homelab_helper.db.models import Cluster, Host, VirtualMachine
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.cluster_nodes import NOT_A_NODE, cluster_nodes


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


def _host(name: str) -> Host:
    return Host(hostname=name, arch=Architecture.AMD64, capabilities={})


async def test_membership_from_node_list_then_guests_and_nothing_else(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        listed, by_guest, nas = _host("listed"), _host("by-guest"), _host("nas")
        s.add_all([listed, by_guest, nas])
        await s.flush()
        # The node list names one host; a guest places the other; the NAS has neither.
        cluster = Cluster(name="pve", kind="proxmox", attributes={"nodes": ["listed"]})
        s.add(cluster)
        await s.flush()
        s.add(
            VirtualMachine(
                cluster_id=cluster.id,
                vmid=1,
                name="vm",
                kind="qemu",
                status="stopped",
                node_name="by-guest",
                node_host_id=by_guest.id,
            )
        )
    async with sessionmaker() as s:
        hosts = list((await s.execute(select(Host))).scalars().all())
        nodes = await cluster_nodes(s, hosts)
        cluster_id = (await s.execute(select(Cluster))).scalar_one().id

    by_name = {h.hostname: h.id for h in hosts}
    assert nodes.of(by_name["listed"]) == {cluster_id}
    assert nodes.of(by_name["by-guest"]) == {cluster_id}
    assert not nodes.is_node(by_name["nas"])
    assert nodes.by_cluster() == {cluster_id: {by_name["listed"], by_name["by-guest"]}}


async def test_a_node_list_name_with_no_host_row_is_ignored(sessionmaker) -> None:
    """Discovery can name a node the harness has never probed; that is not an error."""
    async with session_scope(sessionmaker) as s:
        s.add(_host("known"))
        s.add(Cluster(name="pve", kind="proxmox", attributes={"nodes": ["known", "unprobed"]}))
    async with sessionmaker() as s:
        hosts = list((await s.execute(select(Host))).scalars().all())
        nodes = await cluster_nodes(s, hosts)
    assert nodes.is_node(hosts[0].id)
    assert len(nodes.members) == 1


def test_the_reason_is_one_sentence() -> None:
    assert NOT_A_NODE == "runs no guest and is on no cluster's node list"


async def test_kubernetes_nodes_count_only_when_every_kind_is_asked_for(sessionmaker) -> None:
    from homelab_helper.engine.reconfigure import analyze_surplus

    async with session_scope(sessionmaker) as s:
        pve, worker = _host("pve1"), _host("talos-w1")
        pve.capabilities = {"mem_total_bytes": 64 * 1024**3}
        worker.capabilities = {"mem_total_bytes": 32 * 1024**3}
        s.add_all([pve, worker])
        await s.flush()
        s.add(Cluster(name="pve", kind="proxmox", attributes={"nodes": ["pve1"]}))
        s.add(Cluster(name="admin@talos", kind="kubernetes", attributes={"nodes": ["talos-w1"]}))
    async with sessionmaker() as s:
        hosts = list((await s.execute(select(Host))).scalars().all())
        by_name = {h.hostname: h.id for h in hosts}
        default = await cluster_nodes(s, hosts)
        every = await cluster_nodes(s, hosts, kinds=None)
        report = await analyze_surplus(s)

    assert default.is_node(by_name["pve1"])
    assert not default.is_node(by_name["talos-w1"])  # not an empty hypervisor
    assert every.is_node(by_name["talos-w1"])
    assert ("talos-w1", NOT_A_NODE) in report.not_nodes


async def test_the_diagram_draws_kubernetes_membership(sessionmaker) -> None:
    from homelab_helper.engine.diagram import build_model

    async with session_scope(sessionmaker) as s:
        s.add_all([_host("cp1"), _host("w1")])
        s.add(
            Cluster(
                name="admin@talos",
                kind="kubernetes",
                node_count=2,
                attributes={"nodes": ["cp1", "w1"]},
            )
        )
    async with sessionmaker() as s:
        model = await build_model(s, use_topology_file=False)
    edges = {(e.src, e.dst, e.label) for e in model.edges}
    assert {("h_cp1", "c_admin_talos", "node of"), ("h_w1", "c_admin_talos", "node of")} <= edges
    assert model.nodes["c_admin_talos"].lines == ("kubernetes, 2 nodes",)
