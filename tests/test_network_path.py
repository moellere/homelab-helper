"""NetworkPath tests — topology load, worst-link inheritance, AC6 verdicts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import Architecture
from homelab_helper.db.models import Host
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.network_path import Link, Topology, TopologyError, load_topology
from homelab_helper.engine.placement import network_verdict, recommend_placement
from homelab_helper.engine.workloads import load_workload_library

runner = CliRunner()

_GB = 1024**3

_TOPOLOGY_YAML = """
sites:
  covington:
    hosts: [node0, nas]
  wyola:
    hosts: [wyhome, wynode2]
links:
  - a: covington
    b: wyola
    kind: vpn
    bandwidth_mbps: 40
    latency_ms: 28
    reliability: best-effort
"""


def _topology(tmp_path: Path) -> Topology:
    f = tmp_path / "topo.yaml"
    f.write_text(_TOPOLOGY_YAML)
    topology = load_topology(f)
    assert topology is not None
    return topology


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


# ---------------------------------------------------------------------------
# topology + path inheritance
# ---------------------------------------------------------------------------


def test_same_site_path_is_lan(tmp_path: Path) -> None:
    path = _topology(tmp_path).path("node0", "nas")
    assert path is not None
    assert path.same_site is True
    assert path.lan_grade is True


def test_undeclared_host_defaults_to_lan(tmp_path: Path) -> None:
    path = _topology(tmp_path).path("node0", "mystery-box")
    assert path is not None
    assert path.lan_grade is True


def test_cross_site_inherits_worst_link(tmp_path: Path) -> None:
    """AC6's core: the VPN hop sets the whole path's character."""
    path = _topology(tmp_path).path("node0", "wyhome")
    assert path is not None
    assert path.same_site is False
    assert path.lan_grade is False
    assert path.bandwidth_mbps == 40
    assert path.latency_ms == 28
    assert path.reliability == "best-effort"
    assert path.worst_link is not None
    assert path.worst_link.kind == "vpn"


def test_multihop_sums_latency_min_bandwidth(tmp_path: Path) -> None:
    topology = Topology(
        host_sites={"a1": "a", "c1": "c"},
        links=(
            Link("a", "b", "fiber", 10000, 2.0, "high"),
            Link("b", "c", "vpn", 100, 20.0, "normal"),
        ),
    )
    path = topology.path("a1", "c1")
    assert path is not None
    assert path.bandwidth_mbps == 100  # min
    assert path.latency_ms == 22.0  # sum
    assert path.reliability == "normal"  # worst
    assert path.hops == ("a", "b", "c")


def test_disconnected_sites_have_no_path() -> None:
    topology = Topology(host_sites={"a1": "a", "b1": "island"}, links=())
    assert topology.path("a1", "b1") is None


def test_no_topology_env_means_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_NETWORK_TOPOLOGY", raising=False)
    assert load_topology() is None


def test_duplicate_host_across_sites_rejected(tmp_path: Path) -> None:
    f = tmp_path / "bad.yaml"
    f.write_text("sites:\n  a:\n    hosts: [x]\n  b:\n    hosts: [x]\n")
    with pytest.raises(TopologyError, match="two sites"):
        load_topology(f)


def test_link_to_unknown_site_rejected(tmp_path: Path) -> None:
    f = tmp_path / "bad.yaml"
    f.write_text("sites:\n  a:\n    hosts: [x]\nlinks:\n  - a: a\n    b: nowhere\n    kind: vpn\n")
    with pytest.raises(TopologyError, match="unknown site"):
        load_topology(f)


def test_example_topology_file_parses() -> None:
    from pathlib import Path as P

    topology = load_topology(P("fixtures/network-topology.example.yaml"))
    assert topology is not None
    assert topology.site_of("wyhome") == "wyola"


# ---------------------------------------------------------------------------
# AC6 verdicts
# ---------------------------------------------------------------------------


def test_ceph_refused_across_vpn(tmp_path: Path) -> None:
    """AC6 verbatim: refuse + explain latency/reliability worst-link inheritance."""
    ceph = load_workload_library()["ceph-osd"]
    path = _topology(tmp_path).path("node0", "wyhome")
    assert path is not None
    verdict, message = network_verdict(ceph, path)
    assert verdict == "refuse"
    assert message is not None
    assert "vpn" in message
    assert "28" in message  # latency of the worst link
    assert "best-effort" in message  # reliability inheritance
    assert "worst link" in message


def test_lan_preferred_warns_not_refuses(tmp_path: Path) -> None:
    plex = load_workload_library()["plex"]
    path = _topology(tmp_path).path("node0", "wyhome")
    assert path is not None
    verdict, message = network_verdict(plex, path)
    assert verdict == "warn"
    assert message is not None


def test_same_site_is_ok_even_for_ceph(tmp_path: Path) -> None:
    ceph = load_workload_library()["ceph-osd"]
    path = _topology(tmp_path).path("node0", "nas")
    assert path is not None
    assert network_verdict(ceph, path) == ("ok", None)


# ---------------------------------------------------------------------------
# placement integration
# ---------------------------------------------------------------------------


def _host(name: str, *, mem_gb: int = 32, disk_tb: float | None = None) -> Host:
    caps: dict = {"mem_total_bytes": mem_gb * _GB, "cpu_threads": 8}
    if disk_tb is not None:
        caps["total_disk_bytes"] = int(disk_tb * 1024**4)
    return Host(hostname=name, arch=Architecture.AMD64, capabilities=caps)


async def test_placement_rejects_cross_vpn_for_lan_required(sessionmaker, tmp_path: Path) -> None:
    """The anchor (gravity carrier) is in covington; wyola candidates refuse."""
    ceph = load_workload_library()["ceph-osd"]
    async with session_scope(sessionmaker) as s:
        s.add(_host("nas", disk_tb=8))  # gravity anchor, covington
        s.add(_host("node0"))  # covington
        s.add(_host("wyhome", mem_gb=64))  # wyola — better RAM, wrong site
    async with sessionmaker() as s:
        report = await recommend_placement(s, ceph, topology=_topology(tmp_path))
    names = [c.hostname for c in report.candidates]
    assert "wyhome" not in names
    assert "node0" in names
    rejection = dict(report.rejected)["wyhome"]
    assert "worst link" in rejection
    assert "vpn" in rejection


async def test_placement_penalizes_cross_vpn_for_lan_preferred(
    sessionmaker, tmp_path: Path
) -> None:
    plex = load_workload_library()["plex"]
    async with session_scope(sessionmaker) as s:
        s.add(_host("nas", disk_tb=8))
        s.add(_host("node0"))
        s.add(_host("wyhome", mem_gb=33))  # marginally more RAM than node0
    async with sessionmaker() as s:
        report = await recommend_placement(s, plex, topology=_topology(tmp_path))
    names = [c.hostname for c in report.candidates]
    assert "wyhome" in names  # warned, not refused
    wyhome = next(c for c in report.candidates if c.hostname == "wyhome")
    assert any("degraded" in c for c in wyhome.caveats)
    assert report.best is not None
    assert report.best.hostname != "wyhome"  # the penalty outweighs 1 GiB of RAM


async def test_placement_without_topology_unchanged(sessionmaker) -> None:
    ceph = load_workload_library()["ceph-osd"]
    async with session_scope(sessionmaker) as s:
        s.add(_host("nas", disk_tb=8))
        s.add(_host("wyhome"))
    async with sessionmaker() as s:
        report = await recommend_placement(s, ceph, topology=None)
    # No declared topology: single-site assumption, nothing network-rejected.
    assert len(report.candidates) == 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_path_shows_inheritance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "topo.yaml"
    f.write_text(_TOPOLOGY_YAML)
    monkeypatch.setenv("HOMELAB_HELPER_NETWORK_TOPOLOGY", str(f))
    result = runner.invoke(app, ["plan", "path", "node0", "wyhome"])
    assert result.exit_code == 0
    assert "40 Mbps" in result.stdout
    assert "28.0 ms" in result.stdout
    assert "best-effort" in result.stdout
    assert "LAN-grade: no" in result.stdout


def test_cli_path_workload_refusal_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "topo.yaml"
    f.write_text(_TOPOLOGY_YAML)
    monkeypatch.setenv("HOMELAB_HELPER_NETWORK_TOPOLOGY", str(f))
    result = runner.invoke(app, ["plan", "path", "node0", "wyhome", "--workload", "ceph-osd"])
    assert result.exit_code == 1
    assert "refused" in result.stdout
    assert "worst link" in result.stdout


def test_cli_path_no_topology_is_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_NETWORK_TOPOLOGY", raising=False)
    result = runner.invoke(app, ["plan", "path", "a", "b"])
    assert result.exit_code == 0
    assert "no topology declared" in result.stdout
