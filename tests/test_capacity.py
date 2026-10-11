"""Phase 10.2 — capacity trends from the usage history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import Host, ReconciliationFinding, UsageSample
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.capacity import (
    after_last_step,
    evaluate,
    filesystem_issues,
    node_issues,
    reconcile_capacity_findings,
    record_filesystem_samples,
)
from homelab_helper.engine.digest import build_digest, render_markdown
from homelab_helper.engine.usage import DAY, Bucket, record_usage

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
G = 1024**3


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


def _points(days: int, start: int, per_day: int) -> list[tuple[datetime, int]]:
    base = NOW.replace(tzinfo=None) - timedelta(days=days)
    return [(base + timedelta(days=d), start + d * per_day) for d in range(days + 1)]


def test_filesystem_full_and_headroom() -> None:
    total = 100 * G
    quiet = filesystem_issues("n:/", "host", "h", _points(20, 40 * G, 0), total, now=NOW)
    assert quiet == []
    full = filesystem_issues("n:/", "host", "h", _points(20, 90 * G, 0), total, now=NOW)
    assert [i.category for i in full] == ["filesystem-full"]
    assert full[0].severity is FindingSeverity.MEDIUM
    critical = filesystem_issues("n:/", "host", "h", _points(20, 96 * G, 0), total, now=NOW)
    assert critical[0].severity is FindingSeverity.HIGH
    # 40 GiB used, growing 1 GiB/day: 60 days to full → MEDIUM headroom.
    filling = filesystem_issues("n:/", "host", "h", _points(20, 20 * G, G), total, now=NOW)
    assert [i.category for i in filling] == ["filesystem-headroom"]
    assert filling[0].severity is FindingSeverity.MEDIUM
    assert 55 <= filling[0].evidence["days_to_full"] <= 65
    # 5 GiB/day from 80 GiB: ~4 days → HIGH, and it is already past 85%.
    soon = filesystem_issues("n:/", "host", "h", _points(20, 0, 5 * G), total, now=NOW)
    assert {i.category: i.severity for i in soon} == {
        "filesystem-full": FindingSeverity.HIGH,
        "filesystem-headroom": FindingSeverity.HIGH,
    }
    # Too few samples: no trend verdict.
    assert filesystem_issues("n:/", "host", "h", _points(3, 20 * G, G), total, now=NOW) == []


def _samples(
    days: int, mem_start: float, mem_per_day: float, cpu_start: float, cpu_per_day: float
) -> list[UsageSample]:
    base = NOW.replace(tzinfo=None) - timedelta(days=days)
    return [
        UsageSample(
            subject_type="host",
            subject_key="n",
            resolution=DAY,
            ts=base + timedelta(days=d),
            cpu=cpu_start + d * cpu_per_day,
            mem_used=int((mem_start + d * mem_per_day) * G),
            mem_total=32 * G,
        )
        for d in range(days + 1)
    ]


def test_node_memory_and_cpu_trends() -> None:
    assert node_issues("n", "h", _samples(30, 10, 0, 0.2, 0), now=NOW) == []
    # Memory 10 → 25 GiB over 30 days: 0.5 GiB/day, line at 28.8 GiB → ~8 days → HIGH.
    mem = node_issues("n", "h", _samples(30, 10, 0.5, 0.2, 0), now=NOW)
    assert [i.category for i in mem] == ["memory-headroom"]
    assert mem[0].severity is FindingSeverity.HIGH
    assert "crosses 90%" in mem[0].title
    # CPU 0.2 → 0.5 over 30 days (0.01/day), line 0.8 → ~30 days → HIGH boundary.
    cpu = node_issues("n", "h", _samples(30, 10, 0, 0.2, 0.01), now=NOW)
    assert [i.category for i in cpu] == ["cpu-headroom"]
    assert 25 <= cpu[0].evidence["days_to_line"] <= 35
    # Flat and high is not a trend: nothing (that is a rightsizing/surplus matter).
    assert node_issues("n", "h", _samples(30, 28, 0, 0.85, 0), now=NOW) == []


async def test_nas_filesystem_samples_upsert_one_bucket_per_day(sessionmaker) -> None:
    filesystems = [
        {
            "mountpoint": "/srv/dev-disk-by-uuid-1",
            "label": "data",
            "size_bytes": 3000 * G,
            "used_bytes": 2460 * G,
        },
        {"mountpoint": None, "size_bytes": 10, "used_bytes": 1},  # unmounted: skipped
        {"mountpoint": "/boot", "size_bytes": None, "used_bytes": 1},  # no size: skipped
    ]
    async with session_scope(sessionmaker) as s:
        first = await record_filesystem_samples(s, "omv", filesystems, now=NOW)
        again = await record_filesystem_samples(
            s, "omv", [dict(filesystems[0], used_bytes=2461 * G)], now=NOW + timedelta(hours=3)
        )
        rows = (await s.execute(select(UsageSample))).scalars().all()
    assert (first.inserted, first.updated) == (1, 0)
    assert (again.inserted, again.updated) == (0, 1)
    assert len(rows) == 1
    assert rows[0].subject_type == "filesystem"
    assert rows[0].subject_key == "omv:/srv/dev-disk-by-uuid-1"
    assert rows[0].label == "data"
    assert rows[0].disk_used == 2461 * G
    assert rows[0].ts.replace(tzinfo=None) == NOW.replace(
        tzinfo=None, hour=0, minute=0
    )  # aware on Postgres


async def test_evaluate_covers_hosts_and_filesystems_and_resolves(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = Host(hostname="n0", primary_ip="10.0.0.1")
        s.add(host)
        await s.flush()
        base = NOW.replace(tzinfo=None) - timedelta(days=20)
        buckets = [
            Bucket(
                ts=base + timedelta(days=d),
                values={
                    "cpu": 0.1,
                    "cpus": 8,
                    "mem_used": 8 * G,
                    "mem_total": 32 * G,
                    "disk_used": (20 + d * 2) * G,  # 2 GiB/day on a 100 GiB root
                    "disk_total": 100 * G,
                },
            )
            for d in range(21)
        ]
        await record_usage(
            s, subject_type="host", subject_key="n0", label="n0", resolution=DAY, buckets=buckets
        )
        for d in range(21):
            await record_filesystem_samples(
                s,
                "omv",
                [{"mountpoint": "/data", "size_bytes": 1000 * G, "used_bytes": 100 * G}],
                now=NOW - timedelta(days=20 - d),
            )
        issues, observed = await evaluate(s, now=NOW)
        assert observed == {
            "memory-headroom",
            "cpu-headroom",
            "filesystem-full",
            "filesystem-headroom",
        }
        assert [(i.target_type, i.target_id, i.category) for i in issues] == [
            ("host", str(host.id), "filesystem-headroom")
        ]
        result, _ = await reconcile_capacity_findings(s, now=NOW)
        assert result.counts()["opened"] == 1

        # Growth stops for longer than the trend window: the fit flattens and the finding resolves.
        flat = [
            Bucket(
                ts=base + timedelta(days=21 + d),
                values={
                    "cpu": 0.1,
                    "cpus": 8,
                    "mem_used": 8 * G,
                    "mem_total": 32 * G,
                    "disk_used": 60 * G,
                    "disk_total": 100 * G,
                },
            )
            for d in range(100)
        ]
        await record_usage(
            s, subject_type="host", subject_key="n0", label="n0", resolution=DAY, buckets=flat
        )
        later, _ = await reconcile_capacity_findings(s, now=NOW + timedelta(days=100))
        assert later.counts()["resolved"] == 1
        rows = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert [r.kind for r in rows] == [FindingKind.CAPACITY]
        assert rows[0].status is FindingStatus.RESOLVED


async def test_digest_leads_with_what_runs_out_first(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(Host(hostname="n0", primary_ip="10.0.0.1"))
        await s.flush()
        base = NOW.replace(tzinfo=None) - timedelta(days=20)
        await record_usage(
            s,
            subject_type="host",
            subject_key="n0",
            label="n0",
            resolution=DAY,
            buckets=[
                Bucket(
                    ts=base + timedelta(days=d),
                    values={
                        "cpu": 0.1,
                        "cpus": 8,
                        "disk_used": (20 + d * 2) * G,
                        "disk_total": 100 * G,
                    },
                )
                for d in range(21)
            ],
        )
        await reconcile_capacity_findings(s, now=NOW)
        digest = await build_digest(s, days=7, now=NOW)
    assert len(digest.runs_out) == 1
    days, title = digest.runs_out[0]
    assert "n0:/ fills in about" in title
    assert 15 <= days <= 25
    page = render_markdown(digest)
    assert page.index("## What runs out first") < page.index("## What was done")
    assert "n0:/ fills in about" in page


def test_a_step_is_not_a_trend() -> None:
    total = 100 * G
    # 20 GiB for ten days, then a 40 GiB copy lands and it stays at 60: no growth.
    base = NOW.replace(tzinfo=None) - timedelta(days=30)
    stepped = [(base + timedelta(days=d), (20 if d < 10 else 60) * G) for d in range(31)]
    segment, stepped_at = after_last_step(stepped, total)
    assert stepped_at == base + timedelta(days=10)
    assert len(segment) == 21
    assert filesystem_issues("n:/", "host", "h", stepped, total, now=NOW) == []
    # The same step followed by real growth is judged on the growth alone.
    growing = [(ts, used + max(0, d - 10) * G) for d, (ts, used) in enumerate(stepped)]
    issues = filesystem_issues("n:/", "host", "h", growing, total, now=NOW)
    assert [i.category for i in issues] == ["filesystem-headroom"]
    assert issues[0].evidence["since_step"] == (base + timedelta(days=10)).isoformat()
    assert "since the step on" in issues[0].description
    # Too few samples after the step: no verdict yet.
    late = [(base + timedelta(days=d), (20 if d < 28 else 60) * G) for d in range(31)]
    assert filesystem_issues("n:/", "host", "h", late, total, now=NOW) == []


def test_node_memory_step_is_not_a_trend() -> None:
    # A 12 GiB guest moves onto a 32 GiB node and the node then idles at 19 GiB.
    samples = _samples(30, 7, 0, 0.1, 0)
    for s in samples[10:]:
        s.mem_used = 19 * G
    assert node_issues("n", "h", samples, now=NOW) == []
