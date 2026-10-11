"""Phase 10.3 — per-subject baselines and departures from them."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import Cluster, Host, ReconciliationFinding, UsageSample
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.baselines import (
    BASELINE_DAYS,
    CATEGORIES,
    DEPARTURE_HOURS,
    band_for,
    evaluate,
    reconcile_anomaly_findings,
    subject_issues,
)
from homelab_helper.engine.usage import HOUR, Bucket, record_usage

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
K = 1024.0


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


def _series(
    hours: int, cpu: float = 0.2, net: float = 800 * K, jitter: float = 0.1, seed: int = 1
) -> list[UsageSample]:
    """``hours`` hourly samples ending at NOW, with a little noise around the level."""
    rng = random.Random(seed)
    start = NOW.replace(tzinfo=None) - timedelta(hours=hours - 1)
    out = []
    for h in range(hours):
        f = 1 + rng.uniform(-jitter, jitter)
        out.append(
            UsageSample(
                subject_type="guest",
                subject_key="lab/105",
                label="ha",
                resolution=HOUR,
                ts=start + timedelta(hours=h),
                cpu=cpu * f,
                net_in=net * f / 2,
                net_out=net * f / 2,
                disk_read=0.0,
                disk_write=0.0,
                mem_used=int(4 * 1024**3 * f),
            )
        )
    return out


def _cats(issues) -> dict[str, FindingSeverity]:
    return {i.category: i.severity for i in issues}


def test_band_needs_two_weeks_of_history() -> None:
    assert band_for([0.1] * 100, "cpu") is None
    band = band_for([0.1] * 400 + [0.9] * 10, "cpu")
    assert band is not None
    assert band.p10 == band.p50 == pytest.approx(0.1)
    assert band.p95 == pytest.approx(0.1)  # ten spikes in 410 samples sit above p95
    assert band.samples == 410


def test_steady_subject_raises_nothing_and_is_judged() -> None:
    issues, judged = subject_issues("guest", "lab/105", "ha (105)", _series(30 * 24), now=NOW)
    assert issues == []
    assert judged == set(CATEGORIES)


def test_cpu_spike_in_the_last_hours_is_above_baseline() -> None:
    samples = _series(30 * 24)
    for s in samples[-DEPARTURE_HOURS:]:
        s.cpu = 0.9  # band p95 ≈ 0.22 → line ≈ 0.33
    issues, _ = subject_issues("guest", "lab/105", "ha (105)", samples, now=NOW)
    assert _cats(issues) == {"cpu-above-baseline": FindingSeverity.LOW}
    issue = issues[0]
    assert issue.evidence["direction"] == "above"
    assert issue.evidence["hours"] == DEPARTURE_HOURS
    assert 0.19 <= issue.evidence["band"]["p95"] <= 0.23
    assert "above its usual" in issue.title
    # One spiky hour among the three is not a departure.
    samples[-2].cpu = 0.2
    issues, _ = subject_issues("guest", "lab/105", "ha (105)", samples, now=NOW)
    assert issues == []


def test_sustained_departure_is_medium() -> None:
    samples = _series(30 * 24)
    for s in samples[-8:]:
        s.cpu = 0.9
    issues, _ = subject_issues("guest", "lab/105", "ha (105)", samples, now=NOW)
    assert _cats(issues) == {"cpu-above-baseline": FindingSeverity.MEDIUM}
    assert issues[0].evidence["hours"] == 8


def test_busy_subject_going_quiet_is_below_baseline_but_an_idle_one_cannot() -> None:
    samples = _series(30 * 24, net=800 * K)
    for s in samples[-DEPARTURE_HOURS:]:
        s.net_in = s.net_out = 1 * K
    issues, _ = subject_issues("guest", "lab/105", "ha (105)", samples, now=NOW)
    assert _cats(issues) == {"network-below-baseline": FindingSeverity.LOW}
    assert "far below its usual" in issues[0].title
    # An idle container (p10 under the floor) has no "quiet" to fall from.
    idle = _series(30 * 24, cpu=0.002, net=2 * K)
    for s in idle[-DEPARTURE_HOURS:]:
        s.net_in = s.net_out = 0.0
        s.cpu = 0.0
    issues, judged = subject_issues("guest", "lab/109", "esphome (109)", idle, now=NOW)
    assert issues == []
    assert "network-below-baseline" in judged


def test_small_absolute_moves_on_idle_subjects_are_not_anomalies() -> None:
    # CPU 0.2% → 0.6% is 3x the band but under the 5% floor: noise, not news.
    idle = _series(30 * 24, cpu=0.002, net=2 * K)
    for s in idle[-DEPARTURE_HOURS:]:
        s.cpu = 0.006
        s.net_in = s.net_out = 6 * K
    issues, _ = subject_issues("guest", "lab/109", "esphome (109)", idle, now=NOW)
    assert issues == []


def test_stale_or_thin_history_is_not_judged() -> None:
    thin = _series(5 * 24)
    for s in thin[-DEPARTURE_HOURS:]:
        s.cpu = 0.9
    assert subject_issues("guest", "lab/105", "ha (105)", thin, now=NOW) == ([], set())
    stale = _series(30 * 24)
    for s in stale[-DEPARTURE_HOURS:]:
        s.cpu = 0.9
    assert subject_issues("guest", "lab/105", "ha (105)", stale, now=NOW + timedelta(hours=12)) == (
        [],
        set(),
    )


async def test_evaluate_reconciles_and_resolves_when_back_in_band(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(Cluster(name="lab", kind="proxmox"))
        s.add(Host(hostname="n0", primary_ip="10.0.0.1"))
        await s.flush()
        samples = _series(30 * 24)
        for sm in samples[-DEPARTURE_HOURS:]:
            sm.cpu = 0.9
        buckets = [
            Bucket(
                ts=sm.ts,
                values={
                    "cpu": sm.cpu,
                    "cpus": 2,
                    "net_in": sm.net_in,
                    "net_out": sm.net_out,
                    "disk_read": 0.0,
                    "disk_write": 0.0,
                    "mem_used": sm.mem_used,
                    "mem_total": 8 * 1024**3,
                },
            )
            for sm in samples
        ]
        await record_usage(
            s,
            subject_type="guest",
            subject_key="lab/105",
            label="ha",
            resolution=HOUR,
            buckets=buckets,
        )
        issues, observed, judged = await evaluate(s, now=NOW)
        assert [i.category for i in issues] == ["cpu-above-baseline"]
        assert issues[0].target_id == "lab/105"
        assert ("cluster", "lab") in issues[0].also_affects
        assert judged == {("guest", "lab/105")}
        assert observed == set(CATEGORIES)
        result, _ = await reconcile_anomaly_findings(s, now=NOW)
        assert result.counts()["opened"] == 1

        # Three calm hours later it is back inside its band: resolved.
        calm = [
            Bucket(
                ts=samples[-1].ts + timedelta(hours=h),
                values={
                    "cpu": 0.2,
                    "cpus": 2,
                    "net_in": 400 * K,
                    "net_out": 400 * K,
                    "disk_read": 0.0,
                    "disk_write": 0.0,
                    "mem_used": 4 * 1024**3,
                    "mem_total": 8 * 1024**3,
                },
            )
            for h in range(1, DEPARTURE_HOURS + 1)
        ]
        await record_usage(
            s,
            subject_type="guest",
            subject_key="lab/105",
            label="ha",
            resolution=HOUR,
            buckets=calm,
        )
        later, _ = await reconcile_anomaly_findings(s, now=NOW + timedelta(hours=DEPARTURE_HOURS))
        assert later.counts()["resolved"] == 1
        rows = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert [(r.kind, r.status) for r in rows] == [(FindingKind.ANOMALY, FindingStatus.RESOLVED)]


def test_history_window_is_the_declared_number_of_days() -> None:
    assert BASELINE_DAYS == 28
