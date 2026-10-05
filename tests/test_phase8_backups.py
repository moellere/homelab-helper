"""Phase 8.2 — backup posture: coverage, staleness, verification, orphans, capacity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.backups import (
    CATEGORIES,
    backup_issues,
    capacity_issues,
    orphan_issues,
    reconcile_backup_findings,
    schedule_interval,
)
from tests.test_proxmox_adapter import _adapter

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)
DAILY = {"id": "j1", "enabled": 1, "schedule": "01:30", "all": 1, "storage": "pbs"}


@pytest.fixture
async def sessionmaker():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(eng)
    await eng.dispose()


def _guest(vmid: int, name: str = "g", **kw: Any) -> dict[str, Any]:
    return {"vmid": vmid, "name": name, "node": "pve0", "type": "qemu", **kw}


def _backup(vmid: int, hours_ago: float, state: str = "ok", size: int = 1024**3) -> dict[str, Any]:
    ts = int((NOW - timedelta(hours=hours_ago)).timestamp())
    return {
        "vmid": vmid,
        "ctime": ts,
        "volid": f"pbs:backup/vm/{vmid}/{ts}",
        "size": size,
        "notes": f"guest{vmid}",
        "verification": {"state": state},
    }


@pytest.mark.parametrize(
    ("schedule", "expected"),
    [
        ("01:30", timedelta(days=1)),
        ("sat 02:00", timedelta(days=7)),
        ("mon..fri 21:00", timedelta(days=7 / 5)),
        ("mon,wed,fri 03:00", timedelta(days=7 / 3)),
        ("*/6:00", timedelta(hours=6)),
        ("nonsense", timedelta(days=1)),
        (None, timedelta(days=1)),
    ],
)
def test_schedule_interval(schedule, expected) -> None:
    assert schedule_interval(schedule) == expected


def test_uncovered_stale_missing_and_unverified_guests() -> None:
    guests = [
        _guest(100, "fresh"),
        _guest(101, "stale"),
        _guest(102, "never"),
        _guest(103, "badverify"),
        _guest(104, "excluded"),
        _guest(9000, "template", template=1),
    ]
    job = {**DAILY, "exclude": "104"}
    backups = [_backup(100, 14), _backup(101, 60), _backup(103, 10, state="failed")]
    issues = {(i.category, i.target_id): i for i in backup_issues(guests, [job], backups, now=NOW)}
    assert set(issues) == {
        ("backup-stale", "101"),
        ("backup-stale", "102"),
        ("backup-verify", "103"),
        ("backup-uncovered", "104"),
    }
    assert issues["backup-uncovered", "104"].severity is FindingSeverity.HIGH
    assert issues["backup-verify", "103"].severity is FindingSeverity.MEDIUM


def test_weekly_job_tolerates_a_six_day_old_backup() -> None:
    weekly = {**DAILY, "schedule": "sun 02:00"}
    assert backup_issues([_guest(100)], [weekly], [_backup(100, 6 * 24)], now=NOW) == []
    (stale,) = backup_issues([_guest(100)], [weekly], [_backup(100, 15 * 24)], now=NOW)
    assert stale.category == "backup-stale"


def test_disabled_node_and_explicit_vmid_selection() -> None:
    guests = [_guest(100), _guest(101, node="pve1")]
    jobs = [
        {**DAILY, "enabled": 0},
        {"id": "j2", "schedule": "01:30", "vmid": "100", "node": "pve0"},
    ]
    issues = backup_issues(guests, jobs, [_backup(100, 1), _backup(101, 1)], now=NOW)
    assert [(i.category, i.target_id) for i in issues] == [("backup-uncovered", "101")]


def test_orphaned_backups_are_one_low_finding_per_storage() -> None:
    (orphan,) = orphan_issues(
        "pbs", [_guest(100)], [_backup(100, 1), _backup(107, 900), _backup(107, 924)]
    )
    assert orphan.severity is FindingSeverity.LOW
    assert orphan.evidence == {"vmids": [107], "bytes": 2 * 1024**3}
    assert "never" in orphan.description
    assert orphan_issues("pbs", [_guest(100)], [_backup(100, 1)]) == []


def test_capacity_thresholds() -> None:
    gib = 1024**3
    rows = [
        {"storage": "ok", "disk": 50 * gib, "maxdisk": 100 * gib},
        {"storage": "warm", "disk": 85 * gib, "maxdisk": 100 * gib},
        {"storage": "hot", "disk": 95 * gib, "maxdisk": 100 * gib},
    ]
    by = {i.target_id: i.severity for i in capacity_issues(rows)}
    assert by == {"warm": FindingSeverity.MEDIUM, "hot": FindingSeverity.HIGH}


async def test_backup_findings_lifecycle(sessionmaker) -> None:
    guests = [_guest(100), _guest(101)]
    bad = backup_issues(guests, [DAILY], [_backup(100, 1)], now=NOW)
    async with session_scope(sessionmaker) as s:
        first = await reconcile_backup_findings(s, bad, set(CATEGORIES), when=NOW)
        assert len(first.opened) == 1
        kinds = {r.kind for r in (await s.execute(select(ReconciliationFinding))).scalars()}
        assert kinds == {FindingKind.BACKUP_GAP}
        unobserved = await reconcile_backup_findings(s, [], set(), when=NOW)
        assert unobserved.resolved == []
        good = backup_issues(guests, [DAILY], [_backup(100, 1), _backup(101, 1)], now=NOW)
        healed = await reconcile_backup_findings(s, good, set(CATEGORIES), when=NOW)
        assert len(healed.resolved) == 1
        row = (await s.execute(select(ReconciliationFinding))).scalar_one()
        assert row.status is FindingStatus.RESOLVED


async def test_proxmox_backup_reads_are_plain_gets() -> None:
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode()))
        return httpx.Response(200, json={"data": []})

    adapter = _adapter(handler)
    try:
        assert await adapter.list_backup_jobs() == []
        assert await adapter.storage_content("pve0", "pbs") == []
    finally:
        await adapter.aclose()
    assert seen == [
        ("GET", "/api2/json/cluster/backup", ""),
        ("GET", "/api2/json/nodes/pve0/storage/pbs/content", "content=backup"),
    ]
