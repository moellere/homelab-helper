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
  sitea:
    hosts: [hv0, hv1, nasbox]
  siteb:
    hosts: [edge2]
links:
  - {a: sitea, b: siteb, kind: vpn, bandwidth_mbps: 100, latency_ms: 6}
"""


async def _seed(url: str) -> None:
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)
    async with session_scope(sm) as session:
        hv0 = Host(hostname="hv0", primary_ip="10.10.6.20")
        hv1 = Host(hostname="hv1", primary_ip="10.10.6.21")
        nasbox = Host(hostname="nasbox", primary_ip="10.10.6.29")
        edge2 = Host(hostname="edge2", primary_ip="10.20.0.42")
        stray = Host(hostname="stray", primary_ip="192.168.9.9")  # not in the topology
        session.add_all([hv0, hv1, nasbox, edge2, stray])
        await session.flush()
        pve = Cluster(name="hv", kind="proxmox", node_count=2, attributes={"nodes": ["hv0", "hv1"]})
        session.add(pve)
        await session.flush()
        session.add_all(
            [
                VirtualMachine(cluster_id=pve.id, name="devvm", kind="qemu", node_host_id=hv0.id),
                VirtualMachine(cluster_id=pve.id, name="ops", kind="qemu", node_host_id=hv0.id),
                VirtualMachine(
                    cluster_id=pve.id,
                    name="tmpl",
                    kind="qemu",
                    node_host_id=hv0.id,
                    template=True,
                ),
            ]
        )
        svc = Service(name="argo.example.org")
        lease = Service(name="nasbox-c0-8e")  # UniFi DHCP lease name: noise
        same = Service(name="nasbox")  # the host's own record: noise
        session.add_all([svc, lease, same])
        await session.flush()
        for noise in (lease, same):
            session.add(
                ServiceEndpoint(
                    service_id=noise.id,
                    scope=ResolutionScope.INTERNAL,
                    hostname=noise.name,
                    ip="10.10.6.29",
                    resolver="unifi",
                )
            )
        session.add_all(
            [
                ServiceEndpoint(
                    service_id=svc.id,
                    scope=ResolutionScope.INTERNAL,
                    hostname="argo.example.org",
                    ip="10.10.6.29",
                    resolver="unifi",
                ),
                ServiceEndpoint(
                    service_id=svc.id,
                    scope=ResolutionScope.EXTERNAL,
                    hostname="argo.example.org",
                    ip="10.10.6.29",
                    resolver="cloudflare",
                ),
                ServiceEndpoint(
                    service_id=svc.id,
                    scope=ResolutionScope.INTERNAL,
                    hostname="old.example.org",
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
                affected=[{"target_type": "host", "target_id": str(nasbox.id)}],
            )
        )
        session.add(
            ReconciliationFinding(
                kind=FindingKind.INVENTORY_GAP,
                severity=FindingSeverity.LOW,
                fingerprint="fp-2",
                title="t2",
                description="d",
                affected=[{"type": "host", "id": str(nasbox.id)}],
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

    assert set(model.sites) == {"sitea", "siteb", "192.168.9.0/24"}
    assert model.sites["sitea"] == ["h_hv0", "h_hv1", "h_nasbox"]
    # guests are listed inside the node, templates are not
    assert "guests: devvm, ops" in model.nodes["h_hv0"].lines
    # both finding shapes count
    assert "2 open findings" in model.nodes["h_nasbox"].lines
    # cluster membership from the node list
    edges = {(e.src, e.dst, e.label, e.style) for e in model.edges}
    assert ("h_hv0", "c_hv", "node of", "solid") in edges
    assert ("h_hv1", "c_hv", "node of", "solid") in edges
    # one service node, two resolvers; the unknown IP, the lease name and the host's own record ignored
    assert [n.label for n in model.nodes.values() if n.kind == "service"] == ["argo.example.org"]
    assert ("s_argo_example_org", "h_nasbox", "internal via unifi", "solid") in edges
    assert ("s_argo_example_org", "h_nasbox", "external via cloudflare", "dashed") in edges
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
            only = await build_model(session, site="siteb", topology=topo)
            bare = await build_model(
                session, include_services=False, include_guests=False, topology=topo
            )
            no_topo = await build_model(session, topology=None, use_topology_file=False)
    finally:
        await engine.dispose()
    assert list(only.sites) == ["siteb"]
    assert only.stats["hosts"] == 1
    assert not any(n.kind == "service" for n in bare.nodes.values())
    assert not any(line.startswith("guests:") for line in bare.nodes["h_hv0"].lines)
    # without a topology the /24 stands in for the site
    assert "10.10.6.0/24" in no_topo.sites
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
    assert 'subgraph site_sitea["sitea"]' in mermaid
    assert 'h_hv0["hv0<br/>10.10.6.20<br/>guests: devvm, ops"]' in mermaid
    assert 'h_hv0 -->|"node of"| c_hv' in mermaid
    assert 's_argo_example_org -.->|"external via cloudflare"| h_nasbox' in mermaid
    page = render_html(model, title="Lab")
    assert page.startswith("<!doctype html>")
    assert "<title>Lab</title>" in page
    assert "mermaid@11.4.1" in page
    assert "flowchart LR" in page
    graph = to_dict(model)
    assert {n["id"] for n in graph["nodes"]} >= {"h_hv0", "c_hv", "s_argo_example_org"}
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
    assert "subgraph site_siteb" in got["text"]
    assert got["stats"]["hosts"] == 5
    got = await diagram_tool(site="siteb", format="json")
    assert list(got["graph"]["sites"]) == ["siteb"]
    assert "error" in await diagram_tool(format="png")
