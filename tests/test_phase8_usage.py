"""Phase 8.3 — usage history: rollup, idempotent upsert, bounded retention, p95 summary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from homelab_helper.db.base import Base
from homelab_helper.db.models import UsageSample
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.usage import (
    DAY,
    GUEST_FIELDS,
    HOUR,
    NODE_EXTRA,
    NODE_FIELDS,
    STORAGE_FIELDS,
    percentile,
    prune_usage,
    record_usage,
    rollup,
    summarize,
)
from tests.test_proxmox_adapter import _adapter

T0 = datetime(2026, 10, 1, tzinfo=UTC)
G = 1024**3


@pytest.fixture
async def sessionmaker():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(eng)
    await eng.dispose()


def _guest_point(minutes: int, cpu: float | None, mem: float, **kw: Any) -> dict[str, Any]:
    return {
        "time": int((T0 + timedelta(minutes=minutes)).timestamp()),
        "cpu": cpu,
        "maxcpu": 2,
        "mem": mem,
        "maxmem": 4 * G,
        "netin": 100.0,
        "netout": 50.0,
        **kw,
    }


def test_rollup_means_averages_and_takes_peaks_from_max_points() -> None:
    avg = [
        _guest_point(0, 0.10, 1 * G),
        _guest_point(30, 0.30, 3 * G),
        _guest_point(60, 0.5, 2 * G),
    ]
    peak = [_guest_point(0, 0.90, 3.5 * G), _guest_point(30, 0.40, 3 * G)]
    first, second = rollup(avg, peak, resolution=HOUR, fields=GUEST_FIELDS)
    assert first.ts == datetime(2026, 10, 1, 0, 0)
    assert first.values["cpu"] == pytest.approx(0.20)
    assert first.values["mem_used"] == 2 * G
    assert first.values["cpu_max"] == pytest.approx(0.90)
    assert first.values["mem_used_max"] == round(3.5 * G)
    assert first.values["cpus"] == 2
    # A bucket with no MAX points falls back to its own averages for peaks.
    assert second.values["cpu_max"] == pytest.approx(0.5)


def test_rollup_skips_points_without_data() -> None:
    assert rollup([_guest_point(0, None, 0)], [], resolution=HOUR, fields=GUEST_FIELDS) == []


def test_node_rollup_keeps_pressure_in_extra() -> None:
    p = {
        "time": int(T0.timestamp()),
        "cpu": 0.2,
        "maxcpu": 8,
        "memused": 10 * G,
        "memtotal": 64 * G,
        "iowait": 0.01,
        "pressurememorysome": 0.5,
    }
    (b,) = rollup([p], [p], resolution=DAY, fields=NODE_FIELDS, extra=NODE_EXTRA)
    assert b.ts == datetime(2026, 10, 1)
    assert b.values["mem_total"] == 64 * G
    assert b.extra == {"iowait": 0.01, "pressurememorysome": 0.5}


async def test_record_is_idempotent_and_updates_in_place(sessionmaker) -> None:
    buckets = rollup(
        [_guest_point(m, 0.1, G) for m in range(0, 180, 30)],
        [],
        resolution=HOUR,
        fields=GUEST_FIELDS,
    )
    async with session_scope(sessionmaker) as s:
        first = await record_usage(
            s,
            subject_type="guest",
            subject_key="lab/100",
            label="web",
            resolution=HOUR,
            buckets=buckets,
        )
        again = await record_usage(
            s,
            subject_type="guest",
            subject_key="lab/100",
            label="web",
            resolution=HOUR,
            buckets=buckets,
        )
        assert (first.inserted, first.updated) == (3, 0)
        assert (again.inserted, again.updated) == (0, 3)
        assert (await s.execute(select(func.count()).select_from(UsageSample))).scalar_one() == 3


async def test_prune_keeps_the_table_bounded(sessionmaker, monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_USAGE_HOURLY_DAYS", "10")
    now = T0 + timedelta(days=30)
    old = rollup([_guest_point(0, 0.1, G)], [], resolution=HOUR, fields=GUEST_FIELDS)
    recent = rollup(
        [_guest_point(int(timedelta(days=29).total_seconds() // 60), 0.1, G)],
        [],
        resolution=HOUR,
        fields=GUEST_FIELDS,
    )
    daily = rollup([_guest_point(0, 0.1, G)], [], resolution=DAY, fields=GUEST_FIELDS)
    async with session_scope(sessionmaker) as s:
        for resolution, buckets in ((HOUR, old + recent), (DAY, daily)):
            await record_usage(
                s,
                subject_type="guest",
                subject_key="lab/1",
                label=None,
                resolution=resolution,
                buckets=buckets,
            )
        removed = await prune_usage(s, now=now)
        assert removed == {HOUR: 1, DAY: 0}  # day horizon defaults to two years
        assert (await s.execute(select(func.count()).select_from(UsageSample))).scalar_one() == 2


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 0.95) is None
    assert percentile([float(x) for x in range(1, 101)], 0.95) == 95.0
    assert percentile([3.0], 0.95) == 3.0


async def test_summarize_reports_p95_peak_and_allocation(sessionmaker) -> None:
    avg = [_guest_point(60 * h, 0.01 * h, (1 + h % 3) * G) for h in range(100)]
    peak = [_guest_point(60 * h, 0.02 * h, (1.5 + h % 3) * G) for h in range(100)]
    buckets = rollup(avg, peak, resolution=HOUR, fields=GUEST_FIELDS)
    async with session_scope(sessionmaker) as s:
        await record_usage(
            s,
            subject_type="guest",
            subject_key="lab/100",
            label="web",
            resolution=HOUR,
            buckets=buckets,
        )
        summary = await summarize(
            s,
            subject_type="guest",
            subject_key="lab/100",
            window=timedelta(days=30),
            now=T0 + timedelta(days=5),
        )
        empty = await summarize(s, subject_type="guest", subject_key="lab/999", now=T0)
    assert summary["samples"] == 100
    assert summary["cpu_p95"] == pytest.approx(0.94)
    assert summary["cpu_peak"] == pytest.approx(1.98)
    assert summary["mem_total"] == 4 * G
    assert summary["mem_peak"] == round(3.5 * G)
    assert summary["cpus"] == 2
    assert empty == {"subject": "lab/999", "samples": 0}


async def test_rrd_reads_are_plain_gets() -> None:
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode()))
        return httpx.Response(200, json={"data": []})

    adapter = _adapter(handler)
    try:
        await adapter.rrd("pve0", "month")
        await adapter.rrd("pve0", "year", "MAX", vmid=100, kind="qemu")
    finally:
        await adapter.aclose()
    assert seen == [
        ("GET", "/api2/json/nodes/pve0/rrddata", "timeframe=month&cf=AVERAGE"),
        ("GET", "/api2/json/nodes/pve0/qemu/100/rrddata", "timeframe=year&cf=MAX"),
    ]


def test_rollup_of_a_storage_pool_has_no_cpu_or_memory() -> None:
    # A pool's RRD carries used/total only; the old presence test on ``cpu``
    # dropped every point and the mem lookup raised StopIteration.
    pts = [
        {"time": int((T0 + timedelta(minutes=m)).timestamp()), "used": 10.0 * G, "total": 100.0 * G}
        for m in (0, 30, 60)
    ]
    buckets = rollup(pts, [], resolution=HOUR, fields=STORAGE_FIELDS)
    assert [b.values for b in buckets] == [
        {"disk_used": round(10.0 * G), "disk_total": round(100.0 * G)},
        {"disk_used": round(10.0 * G), "disk_total": round(100.0 * G)},
    ]


async def test_summarize_restarts_at_an_allocation_change(sessionmaker) -> None:
    before = [_guest_point(60 * h, 0.9, 1 * G) for h in range(50)]  # 2 cores, hot
    after = [_guest_point(60 * h, 0.2, 1 * G, maxcpu=3) for h in range(50, 80)]  # 3 cores, calm
    buckets = rollup(before + after, [], resolution=HOUR, fields=GUEST_FIELDS)
    async with session_scope(sessionmaker) as s:
        await record_usage(
            s,
            subject_type="guest",
            subject_key="lab/100",
            label="web",
            resolution=HOUR,
            buckets=buckets,
        )
        summary = await summarize(
            s, subject_type="guest", subject_key="lab/100", now=T0 + timedelta(days=5)
        )
    assert summary["cpus"] == 3
    assert summary["samples"] == 30
    assert summary["samples_in_window"] == 80
    assert summary["allocation_changed_at"].startswith("2026-10-03T02:00:00")  # T0 + 50 h
    assert summary["cpu_p95"] == pytest.approx(0.2)
    assert summary["cpu_peak"] == pytest.approx(0.2)
