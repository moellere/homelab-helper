"""Phase 10.1 — drive health from ``host.smart`` observations, with history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.engine.disk_health import CATEGORIES, disk_issues
from homelab_helper.engine.reconciler import Reconciler
from tests.test_reconciler import (
    _record_observations,
    _seed_host,
    _seed_run,
    engine,  # noqa: F401 - fixture
    session_scope,
    sessionmaker,  # noqa: F401 - fixture
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
HOST = "00000000-0000-7000-8000-000000000001"


def _drive(name: str = "/dev/sda", serial: str = "S1", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "protocol": "ATA",
        "model": "TEST SSD 480GB",
        "serial": serial,
        "wwn": None,
        "health_passed": True,
        "power_on_hours": 10_000,
        "temperature_c": 35,
        "rotation_rate": 0,
        "reallocated_sectors": 0,
        "pending_sectors": 0,
        "crc_errors": 0,
        "percentage_used": None,
    }
    base.update(kw)
    return base


def _history(*points: tuple[int, dict[str, Any]]) -> list[tuple[datetime, list[dict[str, Any]]]]:
    """``(days_ago, drive)`` pairs → history oldest first."""
    return [(NOW - timedelta(days=d), [drive]) for d, drive in sorted(points, key=lambda p: -p[0])]


def _cats(issues: list) -> dict[str, FindingSeverity]:
    return {i.category: i.severity for i in issues}


def test_healthy_drive_raises_nothing() -> None:
    assert disk_issues(HOST, "nas", [_drive()], now=NOW) == []


def test_smart_failed_is_high() -> None:
    issues = disk_issues(HOST, "nas", [_drive(health_passed=False)], now=NOW)
    assert _cats(issues) == {"disk-failing": FindingSeverity.HIGH}
    assert issues[0].target_id == f"{HOST}/S1"
    assert "FAILED" in issues[0].title


def test_reallocated_sectors_medium_when_steady_high_when_growing() -> None:
    steady = disk_issues(
        HOST,
        "nas",
        [_drive(reallocated_sectors=4)],
        _history((30, _drive(reallocated_sectors=4))),
        now=NOW,
    )
    assert _cats(steady) == {"disk-reallocating": FindingSeverity.MEDIUM}
    assert steady[0].evidence["growing"] is False

    growing = disk_issues(
        HOST,
        "nas",
        [_drive(reallocated_sectors=9)],
        _history((30, _drive(reallocated_sectors=4))),
        now=NOW,
    )
    assert _cats(growing) == {"disk-reallocating": FindingSeverity.HIGH}
    assert growing[0].evidence["growing"] is True

    many = disk_issues(HOST, "nas", [_drive(reallocated_sectors=60)], now=NOW)
    assert _cats(many) == {"disk-reallocating": FindingSeverity.HIGH}

    pending = disk_issues(HOST, "nas", [_drive(pending_sectors=1)], now=NOW)
    assert _cats(pending) == {"disk-reallocating": FindingSeverity.MEDIUM}


def test_crc_errors_only_when_they_grow_in_the_window() -> None:
    # covomv's Hitachi: 58 CRC errors from years ago, unchanged — history, not news.
    static = disk_issues(
        HOST, "nas", [_drive(crc_errors=58)], _history((60, _drive(crc_errors=58))), now=NOW
    )
    assert static == []
    no_history = disk_issues(HOST, "nas", [_drive(crc_errors=58)], now=NOW)
    assert no_history == []
    growing = disk_issues(
        HOST, "nas", [_drive(crc_errors=61)], _history((10, _drive(crc_errors=58))), now=NOW
    )
    assert _cats(growing) == {"disk-link-errors": FindingSeverity.MEDIUM}
    assert growing[0].evidence["delta"] == 3
    assert "cable" in growing[0].description


def test_temperature_lines_differ_for_spinning_and_flash() -> None:
    hdd = _drive(rotation_rate=7200, temperature_c=52)
    assert _cats(disk_issues(HOST, "nas", [hdd], now=NOW)) == {"disk-hot": FindingSeverity.MEDIUM}
    ssd = _drive(temperature_c=52)
    assert disk_issues(HOST, "nas", [ssd], now=NOW) == []
    assert _cats(disk_issues(HOST, "nas", [_drive(temperature_c=71)], now=NOW)) == {
        "disk-hot": FindingSeverity.HIGH
    }
    # Judged on the window's peak: cooled down now, but it hit 66 last week.
    peaked = disk_issues(
        HOST, "nas", [_drive(temperature_c=40)], _history((7, _drive(temperature_c=66))), now=NOW
    )
    assert _cats(peaked) == {"disk-hot": FindingSeverity.MEDIUM}
    assert "66 °C peak (40 °C now)" in peaked[0].title


def test_wear_thresholds_and_projection() -> None:
    assert disk_issues(HOST, "nas", [_drive(percentage_used=40)], now=NOW) == []
    assert _cats(disk_issues(HOST, "nas", [_drive(percentage_used=85)], now=NOW)) == {
        "disk-worn": FindingSeverity.MEDIUM
    }
    assert _cats(disk_issues(HOST, "nas", [_drive(percentage_used=96)], now=NOW)) == {
        "disk-worn": FindingSeverity.HIGH
    }
    # 40% → 50% in 30 days: 100% in ~150 days, inside the horizon.
    fast = disk_issues(
        HOST,
        "nas",
        [_drive(percentage_used=50)],
        _history((30, _drive(percentage_used=40))),
        now=NOW,
    )
    assert _cats(fast) == {"disk-worn": FindingSeverity.MEDIUM}
    assert fast[0].evidence["projected_days_to_100"] == 150
    # Too little history to project: quiet.
    short = disk_issues(
        HOST,
        "nas",
        [_drive(percentage_used=50)],
        _history((5, _drive(percentage_used=40))),
        now=NOW,
    )
    assert short == []


def test_aged_spinning_drive_is_low_and_an_old_ssd_is_not() -> None:
    hdd = _drive(rotation_rate=7200, power_on_hours=68_613)
    issues = disk_issues(HOST, "nas", [hdd], now=NOW)
    assert _cats(issues) == {"disk-aged": FindingSeverity.LOW}
    assert "7.8 years" in issues[0].title
    assert disk_issues(HOST, "nas", [_drive(power_on_hours=68_613)], now=NOW) == []


def test_history_outside_the_window_is_ignored_and_other_drives_do_not_bleed() -> None:
    other = _drive(name="/dev/sdb", serial="S2", crc_errors=0)
    hist = [
        (NOW - timedelta(days=120), [_drive(crc_errors=0)]),  # too old
        (NOW - timedelta(days=20), [other]),  # a different drive
    ]
    assert disk_issues(HOST, "nas", [_drive(crc_errors=5)], hist, now=NOW) == []


def test_every_category_is_declared() -> None:
    assert set(CATEGORIES) == {
        "disk-failing",
        "disk-reallocating",
        "disk-link-errors",
        "disk-hot",
        "disk-worn",
        "disk-aged",
    }


async def test_reconciler_opens_disk_findings_per_host_and_resolves_on_replacement(
    sessionmaker,  # noqa: F811
) -> None:
    now = datetime.now(UTC)
    async with session_scope(sessionmaker) as s:
        nas = await _seed_host(s, "nas0")
        other = await _seed_host(s, "nas1")
        run0 = await _seed_run(s, nas.id, "host.smart")
        await _record_observations(
            s,
            run0.id,
            nas.id,
            {"host.smart.devices": [_drive(crc_errors=10)]},
            recorded_at=now - timedelta(days=5),
        )
        run1 = await _seed_run(s, nas.id, "host.smart")
        await _record_observations(
            s,
            run1.id,
            nas.id,
            {
                "host.smart.devices": [
                    _drive(crc_errors=14, reallocated_sectors=2),
                    _drive(name="/dev/sdb", serial="S2", health_passed=False),
                ]
            },
            recorded_at=now,
        )
        result = await Reconciler().reconcile_host(s, nas.id)
        assert len(result.findings_opened) == 3  # link errors, reallocating, failing

        # A host whose smart probe never reported resolves nothing.
        await Reconciler().reconcile_host(s, other.id)
        open_rows = (
            (
                await s.execute(
                    select(ReconciliationFinding).where(
                        ReconciliationFinding.kind == FindingKind.STORAGE_HEALTH,
                        ReconciliationFinding.status == FindingStatus.OPEN,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(open_rows) == 3
        assert {r.affected[0]["target_id"] for r in open_rows if r.affected} <= {
            f"{nas.id}/S1",
            f"{nas.id}/S2",
        }
        assert all(
            ("host", str(nas.id)) in {(a["target_type"], a["target_id"]) for a in r.affected}
            for r in open_rows
        )

        # sdb replaced (new serial, healthy); sda's CRC count stops moving and its
        # sectors hold: the failing finding resolves, link-errors resolves, the
        # reallocating one stays (steady → MEDIUM).
        run2 = await _seed_run(s, nas.id, "host.smart")
        await _record_observations(
            s,
            run2.id,
            nas.id,
            {
                "host.smart.devices": [
                    _drive(crc_errors=14, reallocated_sectors=2),
                    _drive(name="/dev/sdb", serial="S3"),
                ]
            },
            recorded_at=now + timedelta(days=1),
        )
        later = await Reconciler().reconcile_host(s, nas.id)
        assert len(later.findings_resolved) == 1  # the failing sdb (S2) is gone
        # CRC: the window still starts at 10 (five days ago), so 14 still counts as grown.
        still_open = (
            (
                await s.execute(
                    select(ReconciliationFinding).where(
                        ReconciliationFinding.kind == FindingKind.STORAGE_HEALTH,
                        ReconciliationFinding.status == FindingStatus.OPEN,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(still_open) == 2
