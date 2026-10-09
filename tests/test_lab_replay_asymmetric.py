"""The asymmetric fixture closes P4-AC2 / P5-AC4 without a mismatched fleet.

Both criteria sat at ``n/a`` in ``docs/live-validation.md`` because the author's
own cluster is symmetric: the analyser is correctly silent, so there is nothing
to narrate and no mitigation to check. A criterion that can only ever fail open
is not validated. These tests supply the asymmetry and assert the derivation,
including the runbook's own anti-hardcoding check — change a link speed and the
recommendation changes with it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.db.models import Cluster, ReconciliationFinding, VirtualMachine
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.bottlenecks import analyze_bottlenecks
from homelab_helper.engine.lab_replay import (
    LabFixtureError,
    load_lab_fixture,
    parse_lab_fixture,
)

if TYPE_CHECKING:
    from homelab_helper.engine.bottlenecks import BottleneckHit

_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "asymmetric-lab.yaml"
_SLOW_NODE = "ceph-c"

runner = CliRunner()


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


def _fixture_data() -> dict[str, Any]:
    return parse_lab_fixture(_FIXTURE.read_text())


def _set_nic_speed(data: dict[str, Any], hostname: str, mbps: int) -> None:
    """Rewrite one host's NIC speed in the parsed fixture."""
    for host in data["hosts"]:
        if host["hostname"] != hostname:
            continue
        for obs in host["observations"]:
            if obs["key"] == "host.network.interfaces":
                for nic in obs["value"]:
                    nic["speed_mbps"] = mbps
                return
    raise AssertionError(f"no NIC observation for {hostname}")


async def _asymmetry_hits(sm, data: dict[str, Any]) -> list[BottleneckHit]:
    async with session_scope(sm) as s:
        await load_lab_fixture(s, data)
    async with sm() as s:
        hits = await analyze_bottlenecks(s)
    return [h for h in hits if h.pattern == "cluster-link-asymmetry"]


async def test_fixture_seeds_the_cluster_its_guests_imply(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        result = await load_lab_fixture(s, _fixture_data())

    assert (result.clusters_loaded, result.guests_loaded) == (1, 3)
    async with sessionmaker() as s:
        cluster = (await s.execute(select(Cluster))).scalar_one()
        assert cluster.name == "ceph-lab"
        assert cluster.node_count == 3
        # Persisted the way Proxmox discovery persists it, so every planner
        # that reads the node list sees a replayed cluster the same way.
        assert cluster.attributes["nodes"] == ["ceph-a", "ceph-b", "ceph-c"]
        guests = (await s.execute(select(VirtualMachine))).scalars().all()
        # Membership is read off the guests, so each must resolve to a real host.
        assert all(g.node_host_id is not None for g in guests)


async def test_cluster_replay_is_idempotent(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await load_lab_fixture(s, _fixture_data())
    async with session_scope(sessionmaker) as s:
        again = await load_lab_fixture(s, _fixture_data())

    assert again.guests_loaded == 0
    async with sessionmaker() as s:
        assert (await s.execute(select(func.count(Cluster.id)))).scalar_one() == 1
        assert (await s.execute(select(func.count(VirtualMachine.id)))).scalar_one() == 3


async def test_asymmetric_fixture_derives_the_four_mitigations(sessionmaker) -> None:
    hits = await _asymmetry_hits(sessionmaker, _fixture_data())

    assert len(hits) == 1
    hit = hits[0]
    assert hit.kind is FindingKind.CEPH_BOTTLENECK
    # 2500 >= 2 x 1000, so the asymmetry is material, not marginal.
    assert hit.severity is FindingSeverity.HIGH
    assert hit.subject == "ceph-lab"
    assert hit.evidence["speeds_mbps"] == {"ceph-a": 2500, "ceph-b": 2500, "ceph-c": 1000}

    # P5-AC4: the day-one report's four mitigations, each derived from these facts.
    reweight, uplink, relocate, accept = hit.mitigations
    assert all(_SLOW_NODE in m for m in hit.mitigations)
    assert "CRUSH-reweight" in reweight
    assert f"{_SLOW_NODE} to 2500 Mbps" in uplink
    assert "2.5 GbE" in uplink
    assert relocate.endswith("to ceph-a")
    assert accept.startswith("accept:")


async def test_a_listed_node_counts_even_when_it_runs_nothing(sessionmaker) -> None:
    """The node list, not the guests, decides membership: drain ceph-c and the
    asymmetry it causes is still reported, because it is still an OSD host."""
    data = _fixture_data()
    data["clusters"][0]["guests"] = [
        g for g in data["clusters"][0]["guests"] if g["node"] != _SLOW_NODE
    ]

    hits = await _asymmetry_hits(sessionmaker, data)

    assert len(hits) == 1
    assert _SLOW_NODE in hits[0].title


async def test_a_declared_node_must_be_a_host_in_the_fixture(sessionmaker) -> None:
    data = _fixture_data()
    data["clusters"][0]["nodes"].append("ceph-z")
    async with session_scope(sessionmaker) as s:
        with pytest.raises(LabFixtureError, match="ceph-z"):
            await load_lab_fixture(s, data)


async def test_a_symmetric_fleet_silences_the_pattern(sessionmaker) -> None:
    """The author's own fleet, in fixture form: nothing to report, nothing to narrate."""
    data = _fixture_data()
    _set_nic_speed(data, _SLOW_NODE, 2500)

    assert await _asymmetry_hits(sessionmaker, data) == []


async def test_the_recommendation_follows_the_topology(sessionmaker) -> None:
    """The runbook's 'fail if they look hardcoded' check, as a test."""
    data = _fixture_data()
    _set_nic_speed(data, _SLOW_NODE, 100)

    hits = await _asymmetry_hits(sessionmaker, data)

    assert len(hits) == 1
    mitigations = hits[0].mitigations
    assert "100 Mbps" in hits[0].title
    # The target speed is the fleet's, the gap is this node's: both move with the facts.
    assert "bring ceph-c to 2500 Mbps" in mitigations[1]
    assert hits[0].evidence["speeds_mbps"][_SLOW_NODE] == 100


@pytest.fixture
async def replay_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """An empty, file-backed DB — each CLI invocation builds its own engine."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'asymmetric.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    eng = make_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()
    return url


async def _persisted_mitigations(url: str) -> list[list[dict[str, Any]]]:
    """The proposed actions on every persisted CEPH_BOTTLENECK finding."""
    engine = make_engine(url)
    try:
        async with make_sessionmaker(engine)() as s:
            rows = (
                (
                    await s.execute(
                        select(ReconciliationFinding).where(
                            ReconciliationFinding.kind == FindingKind.CEPH_BOTTLENECK
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [list(r.proposed_actions) for r in rows]
    finally:
        await engine.dispose()


def test_ceph_bottleneck_is_demonstrable_through_the_cli(replay_db_url: str) -> None:
    """P4-AC2's deterministic half: the finding a narrator would cite, from two verbs."""
    replay = runner.invoke(app, ["discover", "replay", str(_FIXTURE)])
    assert replay.exit_code == 0, replay.output
    assert "1 cluster(s), 3 guest(s)" in replay.output

    analyze = runner.invoke(app, ["bottlenecks", "--persist"])
    assert analyze.exit_code == 0, analyze.output
    assert "1 opened" in analyze.output

    proposed = asyncio.run(_persisted_mitigations(replay_db_url))

    assert len(proposed) == 1
    summaries = [p["summary"] for p in proposed[0]]
    assert len(summaries) == 4
    assert all(_SLOW_NODE in s for s in summaries)
