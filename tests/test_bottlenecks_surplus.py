"""Bottleneck analyzer (P5-AC4) + reconfiguration reasoner (P5-AC5) tests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import Architecture, FindingKind, FindingStatus, PartKind
from homelab_helper.db.models import (
    Cluster,
    Host,
    PhysicalPart,
    Placement,
    ReconciliationFinding,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.bottlenecks import analyze_bottlenecks, persist_bottlenecks
from homelab_helper.engine.reconfigure import analyze_surplus
from tests.test_cli_chat import EchoRouter

runner = CliRunner()

_GB = 1024**3
_TB = 1024**4


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


def _host(
    name: str, *, mem_gb: int = 32, disk_tb: float | None = None, cpu: str | None = None
) -> Host:
    caps: dict = {"mem_total_bytes": mem_gb * _GB, "cpu_threads": 8}
    if disk_tb is not None:
        caps["total_disk_bytes"] = int(disk_tb * _TB)
    if cpu:
        caps["cpu_model"] = cpu
    return Host(hostname=name, arch=Architecture.AMD64, capabilities=caps)


async def _add_nic(s, host: Host, serial: str, speed_mbps: int) -> None:
    part = PhysicalPart(kind=PartKind.NIC, serial=serial, speed_mbps=speed_mbps)
    s.add(part)
    await s.flush()
    s.add(Placement(part_id=part.id, host_id=host.id, slot=f"eth-{serial}"))


async def _add_dimm(s, host: Host, serial: str, gb: int) -> None:
    part = PhysicalPart(kind=PartKind.DIMM, serial=serial, capacity_bytes=gb * _GB)
    s.add(part)
    await s.flush()
    s.add(Placement(part_id=part.id, host_id=host.id, slot=f"DIMM-{serial}"))


async def _seed_ceph_asymmetry(s) -> None:
    """Three cluster nodes: two at 2500 Mbps, one (node2) at 1000 Mbps."""
    nodes = [_host(f"node{i}", disk_tb=4) for i in range(3)]
    s.add_all(nodes)
    await s.flush()
    cluster = Cluster(name="pve", kind="proxmox")
    s.add(cluster)
    await s.flush()
    for i, node in enumerate(nodes):
        s.add(
            VirtualMachine(
                cluster_id=cluster.id,
                vmid=100 + i,
                name=f"vm{i}",
                kind="qemu",
                status="running",
                node_name=node.hostname,
                node_host_id=node.id,
                memory_bytes=4 * _GB,
            )
        )
    await _add_nic(s, nodes[0], "N0", 2500)
    await _add_nic(s, nodes[1], "N1", 2500)
    await _add_nic(s, nodes[2], "N2", 1000)


# ---------------------------------------------------------------------------
# AC4 — cluster link asymmetry generates the four Ceph mitigations
# ---------------------------------------------------------------------------


async def test_ceph_asymmetry_generates_four_mitigations(sessionmaker) -> None:
    """AC4: the four day-one mitigations, generated from detected facts."""
    async with session_scope(sessionmaker) as s:
        await _seed_ceph_asymmetry(s)
    async with sessionmaker() as s:
        hits = await analyze_bottlenecks(s)
    asym = [h for h in hits if h.pattern == "cluster-link-asymmetry"]
    assert len(asym) == 1
    hit = asym[0]
    assert hit.kind == FindingKind.CEPH_BOTTLENECK
    assert hit.subject == "pve"
    assert len(hit.mitigations) == 4
    joined = " | ".join(hit.mitigations)
    # The four candidate classes, built from THIS fleet's facts, not canned:
    assert "CRUSH-reweight" in joined  # 1. rebalance data
    assert "USB 2.5 GbE" in joined  # 2. link upgrade to the observed fleet speed
    assert "relocate storage daemons" in joined  # 3. OSD relocate
    assert "accept" in joined  # 4. accept as a tier
    # Facts, not template text:
    assert "node2" in joined  # the actual slow node
    assert "2500" in joined  # the actual fleet speed
    assert hit.evidence["speeds_mbps"]["node2"] == 1000


async def test_symmetric_cluster_is_clean(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_ceph_asymmetry(s)
        # Upgrade node2's NIC: asymmetry disappears.
        node2 = (await s.execute(select(Host).where(Host.hostname == "node2"))).scalar_one()
        await _add_nic(s, node2, "N2B", 2500)
    async with sessionmaker() as s:
        hits = await analyze_bottlenecks(s)
    assert all(h.pattern != "cluster-link-asymmetry" for h in hits)


async def test_memory_pressure_names_largest_vm(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        hot = _host("hot", mem_gb=16)
        cold = _host("cold", mem_gb=32)
        s.add_all([hot, cold])
        await s.flush()
        cluster = Cluster(name="lab", kind="proxmox")
        s.add(cluster)
        await s.flush()
        s.add(
            VirtualMachine(
                cluster_id=cluster.id,
                vmid=1,
                name="bigvm",
                kind="qemu",
                status="running",
                node_name="hot",
                node_host_id=hot.id,
                memory_bytes=14 * _GB,
            )
        )
    async with sessionmaker() as s:
        hits = await analyze_bottlenecks(s)
    pressure = [h for h in hits if h.pattern == "memory-pressure"]
    assert len(pressure) == 1
    assert pressure[0].subject == "hot"
    joined = " ".join(pressure[0].mitigations)
    assert "bigvm" in joined  # the actual largest VM
    assert "cold" in joined  # the actual idle destination


async def test_storage_single_uplink_detected(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        nas = _host("nas", disk_tb=8)
        node = _host("node0")
        s.add_all([nas, node])
        await s.flush()
        cluster = Cluster(name="lab", kind="proxmox")
        s.add(cluster)
        await s.flush()
        for host, vmid in ((nas, 1), (node, 2)):
            s.add(
                VirtualMachine(
                    cluster_id=cluster.id,
                    vmid=vmid,
                    name=f"vm{vmid}",
                    kind="qemu",
                    status="running",
                    node_name=host.hostname,
                    node_host_id=host.id,
                    memory_bytes=2 * _GB,
                )
            )
        await _add_nic(s, nas, "NAS0", 1000)  # single 1 GbE uplink
        await _add_nic(s, node, "N0", 2500)
    async with sessionmaker() as s:
        hits = await analyze_bottlenecks(s)
    uplink = [h for h in hits if h.pattern == "storage-single-uplink"]
    assert len(uplink) == 1
    assert uplink[0].subject == "nas"
    assert "8.0 TiB" in uplink[0].description


async def test_persist_lifecycle_open_then_resolve(sessionmaker) -> None:
    """Findings open on detection and resolve when the condition clears."""
    async with session_scope(sessionmaker) as s:
        await _seed_ceph_asymmetry(s)
    async with session_scope(sessionmaker) as s:
        hits = await analyze_bottlenecks(s)
        result = await persist_bottlenecks(s, hits)
    # The seed legitimately trips two patterns: the cluster asymmetry ("pve")
    # and node2's single 1 GbE uplink on a 4 TiB host.
    assert "pve" in result.opened

    # Condition clears (NIC upgraded) → the finding resolves on the next run.
    async with session_scope(sessionmaker) as s:
        node2 = (await s.execute(select(Host).where(Host.hostname == "node2"))).scalar_one()
        await _add_nic(s, node2, "N2B", 2500)
    async with session_scope(sessionmaker) as s:
        hits = await analyze_bottlenecks(s)
        result = await persist_bottlenecks(s, hits)
    # The NIC upgrade clears both the asymmetry and the single-uplink hit.
    assert any("Link asymmetry" in title for title in result.resolved)
    async with sessionmaker() as s:
        row = (
            await s.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.kind == FindingKind.CEPH_BOTTLENECK
                )
            )
        ).scalar_one()
        assert row.status == FindingStatus.RESOLVED
        # Mitigations landed as proposed actions on the finding.
        assert len(row.proposed_actions) == 4


async def test_persist_never_touches_foreign_findings(sessionmaker) -> None:
    """A CHOKEPOINT finding from another generator must not be auto-resolved."""
    async with session_scope(sessionmaker) as s:
        s.add(
            ReconciliationFinding(
                kind=FindingKind.CHOKEPOINT,
                severity=__import__(
                    "homelab_helper.db.enums", fromlist=["FindingSeverity"]
                ).FindingSeverity.MEDIUM,
                fingerprint="feedfeedfeedfeed",
                title="manually filed chokepoint",
                description="operator-entered",
                evidence_refs=[{"type": "manual", "id": "op"}],
            )
        )
    async with session_scope(sessionmaker) as s:
        result = await persist_bottlenecks(s, [])
    assert result.resolved == []
    async with sessionmaker() as s:
        row = (await s.execute(select(ReconciliationFinding))).scalar_one()
        assert row.status == FindingStatus.OPEN


# ---------------------------------------------------------------------------
# AC5 — the node2 surplus case
# ---------------------------------------------------------------------------


async def _seed_surplus(s) -> None:
    """AC5 verbatim: node2 — 24 GB RAM, two stopped VMs, an Example CPU."""
    node2 = _host("node2", mem_gb=24, cpu="Example CPU E3-1230 v5")
    busy = _host("busy", mem_gb=32)
    s.add_all([node2, busy])
    await s.flush()
    cluster = Cluster(name="pve", kind="proxmox")
    s.add(cluster)
    await s.flush()
    for vmid, name in ((301, "old-plex"), (302, "old-nvr")):
        s.add(
            VirtualMachine(
                cluster_id=cluster.id,
                vmid=vmid,
                name=name,
                kind="qemu",
                status="stopped",
                node_name="node2",
                node_host_id=node2.id,
                memory_bytes=4 * _GB,
            )
        )
    s.add(
        VirtualMachine(
            cluster_id=cluster.id,
            vmid=400,
            name="loaded",
            kind="qemu",
            status="running",
            node_name="busy",
            node_host_id=busy.id,
            memory_bytes=24 * _GB,
        )
    )
    for n in range(3):
        await _add_dimm(s, node2, f"D2-{n}", 8)


async def test_surplus_flags_node2_with_three_options(sessionmaker) -> None:
    """AC5: spin VMs up, move DIMMs to the loaded host, or declare the reserve."""
    async with session_scope(sessionmaker) as s:
        await _seed_surplus(s)
    async with sessionmaker() as s:
        hits = await analyze_surplus(s)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.hostname == "node2"
    assert hit.ram_gb == 24
    assert hit.cpu_model == "Example CPU E3-1230 v5"
    assert hit.stopped_vms == ["old-nvr", "old-plex"]
    assert len(hit.options) == 3
    joined = " | ".join(hit.options)
    assert "spin the stopped VM(s) back up" in joined
    assert "old-plex" in joined  # the actual VMs
    assert "busy" in joined  # DIMMs offered to the actually-loaded host
    assert "stopped-by-design" in joined  # the declare-intent option


async def test_surplus_keeps_at_least_one_dimm(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_surplus(s)
    async with sessionmaker() as s:
        hits = await analyze_surplus(s)
    # 3 DIMMs placed; only 2 offered as spare.
    assert len(hits[0].spare_dimm_gb) == 2


async def test_busy_host_is_not_surplus(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_surplus(s)
    async with sessionmaker() as s:
        hits = await analyze_surplus(s)
    assert all(h.hostname != "busy" for h in hits)


async def test_idle_host_without_slack_is_quiet(sessionmaker) -> None:
    """Low commitment alone isn't a hit — there must be something to reconfigure."""
    async with session_scope(sessionmaker) as s:
        s.add(_host("plain-idle", mem_gb=16))
    async with sessionmaker() as s:
        hits = await analyze_surplus(s)
    assert hits == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: str) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'ac45.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    async def _init() -> None:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as s:
            if seed == "ceph":
                await _seed_ceph_asymmetry(s)
            elif seed == "surplus":
                await _seed_surplus(s)
        await engine.dispose()

    asyncio.run(_init())


def test_cli_bottlenecks_prints_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db(tmp_path, monkeypatch, "ceph")
    result = runner.invoke(app, ["bottlenecks", "--persist"])
    assert result.exit_code == 0
    assert "Link asymmetry in cluster pve" in result.stdout
    assert "CRUSH-reweight" in result.stdout
    assert "opened" in result.stdout
    listed = runner.invoke(app, ["findings", "list", "--kind", "ceph-bottleneck"])
    assert "ceph-bottleneck" in listed.stdout


def test_cli_bottlenecks_clean_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _db(tmp_path, monkeypatch, "none")
    result = runner.invoke(app, ["bottlenecks"])
    assert result.exit_code == 0
    assert "no known bottleneck patterns" in result.stdout


def test_cli_surplus_prints_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _db(tmp_path, monkeypatch, "surplus")
    result = runner.invoke(app, ["plan", "surplus"])
    assert result.exit_code == 0
    assert "node2" in result.stdout
    assert "24 GiB RAM" in result.stdout
    assert "old-plex" in result.stdout
    assert "stopped-by-design" in result.stdout


def test_cli_bottlenecks_narrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import homelab_helper.cli.bottlenecks as cli_bn

    _db(tmp_path, monkeypatch, "ceph")
    router = EchoRouter(reply="Reweight first; buy the adapter if it recurs.")
    monkeypatch.setattr(cli_bn, "_load_router", lambda: router)
    result = runner.invoke(app, ["bottlenecks", "--narrate"])
    assert result.exit_code == 0
    assert "Reweight first" in result.stdout
    assert "option:" in router.messages[0][0]["content"]
