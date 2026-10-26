"""``engine/diagram``: the inventory as a graph, and the three renderings of it."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity, ResolutionScope
from homelab_helper.db.models import (
    Cluster,
    Host,
    ReconciliationFinding,
    Service,
    ServiceEndpoint,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.diagram import (
    UNPLACED,
    build_model,
    render_html,
    render_mermaid,
    to_dict,
)
from homelab_helper.engine.network_path import load_topology
from homelab_helper.mcp_server import diagram as diagram_tool

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

TOPOLOGY = """
sites:
  covington:
    hosts: [bmax0, bmax1, covomv]
  wyola:
    hosts: [wynode2]
links:
  - {a: covington, b: wyola, kind: vpn, bandwidth_mbps: 100, latency_ms: 6}
"""


async def _seed(url: str) -> None:
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)
    async with session_scope(sm) as session:
        bmax0 = Host(hostname="bmax0", primary_ip="10.250.6.20")
        bmax1 = Host(hostname="bmax1", primary_ip="10.250.6.21")
        covomv = Host(hostname="covomv", primary_ip="10.250.6.29")
        wynode2 = Host(hostname="wynode2", primary_ip="10.254.0.42")
        stray = Host(hostname="stray", primary_ip="192.168.9.9")  # not in the topology
        session.add_all([bmax0, bmax1, covomv, wynode2, stray])
        await session.flush()
        pve = Cluster(
            name="bmax", kind="proxmox", node_count=2, attributes={"nodes": ["bmax0", "bmax1"]}
        )
        session.add(pve)
        await session.flush()
        session.add_all(
            [
                VirtualMachine(
                    cluster_id=pve.id, name="ubuntu-dev", kind="qemu", node_host_id=bmax0.id
                ),
                VirtualMachine(cluster_id=pve.id, name="ops", kind="qemu", node_host_id=bmax0.id),
                VirtualMachine(
                    cluster_id=pve.id,
                    name="tmpl",
                    kind="qemu",
                    node_host_id=bmax0.id,
                    template=True,
                ),
            ]
        )
        svc = Service(name="argo.dorktool.com")
        lease = Service(name="covomv-c0-8e")  # UniFi DHCP lease name: noise
        same = Service(name="covomv")  # the host's own record: noise
        session.add_all([svc, lease, same])
        await session.flush()
        for noise in (lease, same):
            session.add(
                ServiceEndpoint(
                    service_id=noise.id,
                    scope=ResolutionScope.INTERNAL,
                    hostname=noise.name,
                    ip="10.250.6.29",
                    resolver="unifi",
                )
            )
        session.add_all(
            [
                ServiceEndpoint(
                    service_id=svc.id,
                    scope=ResolutionScope.INTERNAL,
                    hostname="argo.dorktool.com",
                    ip="10.250.6.29",
                    resolver="unifi",
                ),
                ServiceEndpoint(
                    service_id=svc.id,
                    scope=ResolutionScope.EXTERNAL,
                    hostname="argo.dorktool.com",
                    ip="10.250.6.29",
                    resolver="cloudflare",
                ),
                ServiceEndpoint(
                    service_id=svc.id,
                    scope=ResolutionScope.INTERNAL,
                    hostname="old.dorktool.com",
                    ip="10.99.99.99",
                    resolver="unifi",  # resolves to nothing we know
                ),
            ]
        )
        session.add(
            ReconciliationFinding(
                kind=FindingKind.INVENTORY_GAP,
                severity=FindingSeverity.HIGH,
                fingerprint="fp-1",
                title="t",
                description="d",
                affected=[{"target_type": "host", "target_id": str(covomv.id)}],
            )
        )
        session.add(
            ReconciliationFinding(
                kind=FindingKind.INVENTORY_GAP,
                severity=FindingSeverity.LOW,
                fingerprint="fp-2",
                title="t2",
                description="d",
                affected=[{"type": "host", "id": str(covomv.id)}],
            )
        )
    await engine.dispose()


@pytest.fixture
async def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'lab.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    topo = tmp_path / "topology.yaml"
    topo.write_text(TOPOLOGY)
    monkeypatch.setenv("HOMELAB_HELPER_NETWORK_TOPOLOGY", str(topo))
    await _seed(url)
    return url


async def test_model_groups_hosts_by_site_and_draws_clusters_services_findings(lab: str) -> None:
    engine = make_engine(lab)
    try:
        async with make_sessionmaker(engine)() as session:
            model = await build_model(session, topology=load_topology())
    finally:
        await engine.dispose()

    assert set(model.sites) == {"covington", "wyola", "192.168.9.0/24"}
    assert model.sites["covington"] == ["h_bmax0", "h_bmax1", "h_covomv"]
    # guests are listed inside the node, templates are not
    assert "guests: ops, ubuntu-dev" in model.nodes["h_bmax0"].lines
    # both finding shapes count
    assert "2 open findings" in model.nodes["h_covomv"].lines
    # cluster membership from the node list
    edges = {(e.src, e.dst, e.label, e.style) for e in model.edges}
    assert ("h_bmax0", "c_bmax", "node of", "solid") in edges
    assert ("h_bmax1", "c_bmax", "node of", "solid") in edges
    # one service node, two resolvers; the unknown IP, the lease name and the host's own record ignored
    assert [n.label for n in model.nodes.values() if n.kind == "service"] == ["argo.dorktool.com"]
    assert ("s_argo_dorktool_com", "h_covomv", "internal via unifi", "solid") in edges
    assert ("s_argo_dorktool_com", "h_covomv", "external via cloudflare", "dashed") in edges
    assert model.stats == {
        "sites": 3,
        "hosts": 5,
        "clusters": 1,
        "guests": 2,
        "services": 1,
        "open_findings": 2,
    }


async def test_site_filter_and_flags(lab: str) -> None:
    engine = make_engine(lab)
    try:
        async with make_sessionmaker(engine)() as session:
            topo = load_topology()
            only = await build_model(session, site="wyola", topology=topo)
            bare = await build_model(
                session, include_services=False, include_guests=False, topology=topo
            )
            no_topo = await build_model(session, topology=None, use_topology_file=False)
    finally:
        await engine.dispose()
    assert list(only.sites) == ["wyola"]
    assert only.stats["hosts"] == 1
    assert not any(n.kind == "service" for n in bare.nodes.values())
    assert not any(line.startswith("guests:") for line in bare.nodes["h_bmax0"].lines)
    # without a topology the /24 stands in for the site
    assert "10.250.6.0/24" in no_topo.sites
    assert UNPLACED not in no_topo.sites


async def test_renderings(lab: str) -> None:
    engine = make_engine(lab)
    try:
        async with make_sessionmaker(engine)() as session:
            model = await build_model(session, topology=load_topology())
    finally:
        await engine.dispose()
    mermaid = render_mermaid(model)
    assert mermaid.startswith("flowchart LR\n")
    assert 'subgraph site_covington["covington"]' in mermaid
    assert 'h_bmax0["bmax0<br/>10.250.6.20<br/>guests: ops, ubuntu-dev"]' in mermaid
    assert 'h_bmax0 -->|"node of"| c_bmax' in mermaid
    assert 's_argo_dorktool_com -.->|"external via cloudflare"| h_covomv' in mermaid
    page = render_html(model, title="Lab")
    assert page.startswith("<!doctype html>")
    assert "<title>Lab</title>" in page
    assert "mermaid@11.4.1" in page
    assert "flowchart LR" in page
    graph = to_dict(model)
    assert {n["id"] for n in graph["nodes"]} >= {"h_bmax0", "c_bmax", "s_argo_dorktool_com"}
    assert graph["stats"]["hosts"] == 5


async def test_empty_lab_draws_a_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with make_sessionmaker(engine)() as session:
            model = await build_model(session, use_topology_file=False)
    finally:
        await engine.dispose()
    assert model.stats["hosts"] == 0
    assert "nothing discovered yet" in render_mermaid(model)


def test_cli_formats_and_out(lab: str, tmp_path: Path) -> None:
    result = runner.invoke(app, ["diagram"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("flowchart LR")
    result = runner.invoke(app, ["diagram", "--format", "json", "--no-services"])
    assert result.exit_code == 0, result.output
    graph = json.loads(result.output)
    assert graph["stats"]["services"] == 0
    assert graph["stats"]["hosts"] == 5
    out = tmp_path / "lab.html"
    result = runner.invoke(
        app, ["diagram", "--format", "html", "--out", str(out), "--title", "My lab"]
    )
    assert result.exit_code == 0, result.output
    page = out.read_text()
    assert page.startswith("<!doctype html>")
    assert "<title>My lab</title>" in page
    result = runner.invoke(app, ["diagram", "--format", "svg"])
    assert result.exit_code == 2


async def test_mcp_tool(lab: str) -> None:
    got = await diagram_tool()
    assert got["format"] == "mermaid"
    assert "subgraph site_wyola" in got["text"]
    assert got["stats"]["hosts"] == 5
    got = await diagram_tool(site="wyola", format="json")
    assert list(got["graph"]["sites"]) == ["wyola"]
    assert "error" in await diagram_tool(format="png")
