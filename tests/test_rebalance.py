"""Rebalance solver tests (P5-AC3) — three plan classes, constraints, CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

import asyncio

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import Architecture, PartKind
from homelab_helper.db.models import Cluster, Host, PhysicalPart, Placement, VirtualMachine
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.network_path import Link, Topology
from homelab_helper.engine.rebalance import plan_rebalance
from tests.test_cli_chat import EchoRouter

runner = CliRunner()

_GB = 1024**3


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


def _host(name: str, mem_gb: int) -> Host:
    return Host(
        hostname=name,
        arch=Architecture.AMD64,
        capabilities={"mem_total_bytes": mem_gb * _GB, "cpu_threads": 16},
    )


async def _seed_imbalanced(s, *, dimms_on_node2: int = 2) -> None:
    """node1 heavily committed; node2 nearly idle with spare DIMMs."""
    node1 = _host("node1", 32)
    node2 = _host("node2", 24)
    s.add_all([node1, node2])
    await s.flush()
    cluster = Cluster(name="lab", kind="proxmox")
    s.add(cluster)
    await s.flush()
    for i, mem_gb in enumerate((12, 8, 6), start=100):
        s.add(
            VirtualMachine(
                cluster_id=cluster.id,
                vmid=i,
                name=f"vm{i}",
                kind="qemu",
                status="running",
                node_name="node1",
                node_host_id=node1.id,
                vcpus=2,
                memory_bytes=mem_gb * _GB,
            )
        )
    s.add(
        VirtualMachine(
            cluster_id=cluster.id,
            vmid=200,
            name="idler",
            kind="qemu",
            status="running",
            node_name="node2",
            node_host_id=node2.id,
            vcpus=1,
            memory_bytes=2 * _GB,
        )
    )
    for n in range(dimms_on_node2):
        part = PhysicalPart(
            kind=PartKind.DIMM, model=f"DIMM-{n}", serial=f"D2-{n}", capacity_bytes=8 * _GB
        )
        s.add(part)
        await s.flush()
        s.add(Placement(part_id=part.id, host_id=node2.id, slot=f"DIMM{n}"))


async def test_imbalanced_fleet_yields_three_plan_classes(sessionmaker) -> None:
    """AC3: at least three candidate plans, spanning the three cost classes."""
    async with session_scope(sessionmaker) as s:
        await _seed_imbalanced(s)
    async with sessionmaker() as s:
        report = await plan_rebalance(s, topology=None)
    assert report.balanced is False
    names = [p.name for p in report.plans]
    assert names == ["current-hardware", "one-dimm-move", "one-part-purchase"]
    # Every plan carries steps, tradeoffs, and the resulting load picture.
    for plan in report.plans:
        assert plan.steps
        assert plan.tradeoffs
        assert plan.resulting_ratios


async def test_moves_only_plan_migrates_largest_first(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_imbalanced(s)
    async with sessionmaker() as s:
        report = await plan_rebalance(s, topology=None)
    moves = report.plans[0]
    assert "vm100" in moves.steps[0].description  # 12 GiB VM moves first
    assert "node1" in moves.steps[0].description
    assert "node2" in moves.steps[0].description


async def test_dimm_move_names_donor_and_size(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_imbalanced(s)
    async with sessionmaker() as s:
        report = await plan_rebalance(s, topology=None)
    dimm_plan = next(p for p in report.plans if p.name == "one-dimm-move")
    first = dimm_plan.steps[0]
    assert first.action == "move-dimm"
    assert "8 GiB" in first.description
    assert "node2" in first.description  # donor
    assert "node1" in first.description  # receiver


async def test_dimm_move_needs_spare_dimms(sessionmaker) -> None:
    """A donor with a single DIMM has nothing to give."""
    async with session_scope(sessionmaker) as s:
        await _seed_imbalanced(s, dimms_on_node2=1)
    async with sessionmaker() as s:
        report = await plan_rebalance(s, topology=None)
    assert all(p.name != "one-dimm-move" for p in report.plans)


async def test_purchase_plan_sizes_smallest_sufficient_dimm(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_imbalanced(s)
    async with sessionmaker() as s:
        report = await plan_rebalance(s, topology=None)
    buy = next(p for p in report.plans if p.name == "one-part-purchase")
    assert buy.steps[0].action == "buy-dimm"
    assert "node1" in buy.steps[0].description
    # node1: 26 GiB committed; +8 GiB -> 26/39 = 67% <= 75% target.
    assert "8 GiB" in buy.steps[0].description


async def test_cross_vpn_migration_never_planned(sessionmaker) -> None:
    """The moves plan respects LAN-grade paths — no live migration over a VPN."""
    async with session_scope(sessionmaker) as s:
        await _seed_imbalanced(s, dimms_on_node2=0)
    topology = Topology(
        host_sites={"node1": "covington", "node2": "wyola"},
        links=(Link("covington", "wyola", "vpn", 40, 28.0, "best-effort"),),
    )
    async with sessionmaker() as s:
        report = await plan_rebalance(s, topology=topology)
    for plan in report.plans:
        for step in plan.steps:
            assert step.action != "migrate-vm", f"planned a cross-VPN migration: {step}"


async def test_balanced_fleet_produces_no_plans(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        h1, h2 = _host("a", 32), _host("b", 32)
        s.add_all([h1, h2])
        await s.flush()
        cluster = Cluster(name="lab", kind="proxmox")
        s.add(cluster)
        await s.flush()
        for host, vmid in ((h1, 1), (h2, 2)):
            s.add(
                VirtualMachine(
                    cluster_id=cluster.id,
                    vmid=vmid,
                    name=f"vm{vmid}",
                    kind="qemu",
                    status="running",
                    node_name=host.hostname,
                    node_host_id=host.id,
                    memory_bytes=8 * _GB,
                )
            )
    async with sessionmaker() as s:
        report = await plan_rebalance(s, topology=None)
    assert report.balanced is True
    assert report.plans == []


async def test_unknown_ram_hosts_listed_not_planned(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_imbalanced(s)
        s.add(Host(hostname="mystery", arch=Architecture.AMD64, capabilities={}))
    async with sessionmaker() as s:
        report = await plan_rebalance(s, topology=None)
    assert "mystery" in report.unknown_hosts
    assert all(h.hostname != "mystery" for h in report.hosts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, imbalanced: bool = True) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'rebalance.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    monkeypatch.delenv("HOMELAB_HELPER_NETWORK_TOPOLOGY", raising=False)

    async def _init() -> None:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as s:
            if imbalanced:
                await _seed_imbalanced(s)
        await engine.dispose()

    asyncio.run(_init())


def test_cli_rebalance_prints_three_plans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _db(tmp_path, monkeypatch)
    result = runner.invoke(app, ["plan", "rebalance"])
    assert result.exit_code == 0
    assert "plan 1 — current-hardware" in result.stdout
    assert "plan 2 — one-dimm-move" in result.stdout
    assert "plan 3 — one-part-purchase" in result.stdout
    assert "tradeoff" in result.stdout


def test_cli_rebalance_balanced_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _db(tmp_path, monkeypatch, imbalanced=False)
    result = runner.invoke(app, ["plan", "rebalance"])
    assert result.exit_code == 0
    assert "balanced" in result.stdout


def test_cli_rebalance_narrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import homelab_helper.cli.plan as cli_plan

    _db(tmp_path, monkeypatch)
    router = EchoRouter(reply="Start with the free plan: migrate vm100.")
    monkeypatch.setattr(cli_plan, "_load_router", lambda: router)
    result = runner.invoke(app, ["plan", "rebalance", "--narrate"])
    assert result.exit_code == 0
    assert "Start with the free plan" in result.stdout
    prompt = router.messages[0][0]["content"]
    assert "PLAN 1" in prompt
    assert "FLEET LOAD" in prompt
