"""Phase 8.4 — rightsizing from usage history, and rebalance on observed memory."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import Architecture, FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import Cluster, Host, ReconciliationFinding, VirtualMachine
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.rebalance import plan_rebalance
from homelab_helper.engine.rightsizing import MIN_SAMPLES, guest_issues, reconcile_rightsizing
from homelab_helper.engine.usage import HOUR, Bucket, record_usage

G = 1024**3
NOW = datetime(2026, 10, 6, tzinfo=UTC)


@pytest.fixture
async def sessionmaker():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(eng)
    await eng.dispose()


def _summary(**kw: Any) -> dict[str, Any]:
    base = {
        "samples": 720,
        "cpus": 2.0,
        "cpu_p95": 0.2,
        "cpu_peak": 0.6,
        "mem_total": 4 * G,
        "mem_p95": 3 * G,
        "mem_peak": 3.2 * G,
        "net_mean": 50_000.0,
    }
    return {**base, **kw}


def _cats(issues) -> dict[str, Any]:
    return {i.category: i for i in issues}


def test_cpu_bound_guest_gets_more_cores_with_the_numbers() -> None:
    (grow,) = guest_issues("lab/105", "ha", "qemu", _summary(cpu_p95=0.62, cpu_peak=1.04), 30)
    assert grow.category == "cpu-grow"
    assert grow.severity is FindingSeverity.MEDIUM  # saturated peak
    assert grow.evidence == {
        "allocated_cores": 2.0,
        "cpu_p95": 0.62,
        "cpu_peak": 1.04,
        "window_days": 30,
        "proposed_cores": 3,
    }
    assert "62%" in grow.description
    assert "30 days" in grow.description


def test_bursty_guest_keeps_its_cores_but_quiet_one_shrinks() -> None:
    bursty = guest_issues(
        "lab/250", "builder", "qemu", _summary(cpus=4.0, cpu_p95=0.0, cpu_peak=0.99), 30
    )
    assert "cpu-shrink" not in _cats(bursty)
    quiet = _cats(guest_issues("lab/101", "dc", "qemu", _summary(cpu_p95=0.05, cpu_peak=0.27), 30))
    assert quiet["cpu-shrink"].evidence["proposed_cores"] == 1


def test_memory_shrink_needs_a_real_saving() -> None:
    issues = _cats(
        guest_issues("lab/250", "b", "qemu", _summary(mem_p95=0.5 * G, mem_peak=1.3 * G), 30)
    )
    shrink = issues["mem-shrink"]
    assert shrink.evidence["proposed_bytes"] == 2 * G  # 1.3 x 1.25 = 1.625 -> 2.0 in 512 MiB steps
    assert "page cache" in shrink.description  # the VM caveat
    small = guest_issues(
        "lab/1", "s", "qemu", _summary(mem_total=1 * G, mem_p95=0.2 * G, mem_peak=0.3 * G), 30
    )
    assert "mem-shrink" not in _cats(small)  # would free < 1 GiB


def test_vm_memory_never_grows_but_a_full_container_does() -> None:
    full = _summary(mem_total=12 * G, mem_p95=11.5 * G, mem_peak=11.6 * G)
    assert "mem-grow" not in _cats(guest_issues("lab/105", "ha", "qemu", full, 30))
    lxc = _cats(guest_issues("lab/200", "ct", "lxc", full, 30))
    assert lxc["mem-grow"].severity is FindingSeverity.MEDIUM
    assert lxc["mem-grow"].evidence["proposed_bytes"] > 12 * G


def test_idle_needs_both_quiet_cpu_and_quiet_network() -> None:
    idle = _cats(
        guest_issues(
            "lab/109", "x", "lxc", _summary(cpu_peak=0.04, cpu_p95=0.01, net_mean=300.0), 30
        )
    )
    assert "idle" in idle
    busy_net = guest_issues(
        "lab/109", "x", "lxc", _summary(cpu_peak=0.04, cpu_p95=0.01, net_mean=90_000.0), 30
    )
    assert "idle" not in _cats(busy_net)


# ------------------------------------------------------------- persistence


async def _seed(
    s, *, vmid: int, hours: int, cpu: float, mem_gb: float, alloc_gb: float = 4, node=None
) -> None:
    cluster = (await s.execute(select(Cluster))).scalar_one_or_none()
    if cluster is None:
        cluster = Cluster(name="lab", kind="proxmox")
        s.add(cluster)
        await s.flush()
    s.add(
        VirtualMachine(
            cluster_id=cluster.id,
            vmid=vmid,
            name=f"vm{vmid}",
            kind="qemu",
            status="running",
            node_name=node.hostname if node else "pve0",
            node_host_id=node.id if node else None,
            vcpus=2,
            memory_bytes=int(alloc_gb * G),
        )
    )
    start = (NOW - timedelta(hours=hours)).replace(tzinfo=None)
    buckets = [
        Bucket(
            ts=start + timedelta(hours=h),
            values={
                "cpu": cpu,
                "cpu_max": cpu,
                "cpus": 2.0,
                "mem_used": int(mem_gb * G),
                "mem_used_max": int(mem_gb * G),
                "mem_total": int(alloc_gb * G),
                "net_in": 10_000.0,
                "net_out": 10_000.0,
            },
        )
        for h in range(hours)
    ]
    await record_usage(
        s,
        subject_type="guest",
        subject_key=f"lab/{vmid}",
        label=f"vm{vmid}",
        resolution=HOUR,
        buckets=buckets,
    )


async def test_thin_history_gets_no_verdict_and_never_resolves(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed(s, vmid=100, hours=MIN_SAMPLES + 10, cpu=0.9, mem_gb=3.5)
        await _seed(s, vmid=101, hours=MIN_SAMPLES - 1, cpu=0.9, mem_gb=3.5)
        result, issues, skipped = await reconcile_rightsizing(s, now=NOW)
        assert {i.target_id for i in issues} == {"lab/100"}
        assert len(skipped) == 1
        assert "vm101" in skipped[0]
        assert result.counts()["opened"] == 1

        # A guest that drops below the history bar is not evaluated, so its finding stays open.
        await s.execute(
            ReconciliationFinding.__table__.update().values(
                affected=[{"target_type": "guest", "target_id": "lab/101"}]
            )
        )
        later = await reconcile_rightsizing(s, now=NOW)
        assert later[0].resolved == []
        rows = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert {r.kind for r in rows} == {FindingKind.RIGHTSIZING}
        assert all(r.status is FindingStatus.OPEN for r in rows)


async def test_rebalance_on_usage_loads_guests_at_observed_memory(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        node = Host(
            hostname="pve0", arch=Architecture.AMD64, capabilities={"mem_total_bytes": 32 * G}
        )
        s.add(node)
        await s.flush()
        await _seed(s, vmid=100, hours=MIN_SAMPLES + 1, cpu=0.1, mem_gb=1.0, alloc_gb=16, node=node)
    async with sessionmaker() as s:
        allocated = await plan_rebalance(s, basis="allocated")
        usage = await plan_rebalance(s, basis="usage")
        with pytest.raises(ValueError, match="basis"):
            await plan_rebalance(s, basis="vibes")
    (a,) = [h for h in allocated.hosts if h.hostname == "pve0"]
    (u,) = [h for h in usage.hosts if h.hostname == "pve0"]
    assert a.committed == 16 * G
    assert u.committed == 1 * G
