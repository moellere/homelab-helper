"""Tests for stray-config detection (STRAY_CONFIG finding lifecycle)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.stray_config import reconcile_stray_config


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


def _net(name: str, subnet: str | None, *, vlan_id=None, enabled: bool = True) -> dict:
    return {"name": name, "subnet": subnet, "vlan_id": vlan_id, "enabled": enabled}


def _client(ip: str) -> dict:
    return {"hostname": "x", "ip": ip}


async def _findings(sm) -> list[ReconciliationFinding]:
    async with sm() as s:
        return list((await s.execute(select(ReconciliationFinding))).scalars().all())


# ---------------------------------------------------------------------------
# open / resolve / reopen
# ---------------------------------------------------------------------------


async def test_empty_network_opens_stray_finding(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        result = await reconcile_stray_config(s, [_net("IoT", "10.0.20.0/24", vlan_id=20)], [])
    assert result.opened == ["IoT"]
    findings = await _findings(sessionmaker)
    assert len(findings) == 1
    assert findings[0].kind == FindingKind.STRAY_CONFIG
    assert findings[0].severity == FindingSeverity.LOW
    assert findings[0].status == FindingStatus.OPEN


async def test_network_with_a_client_in_subnet_opens_nothing(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        result = await reconcile_stray_config(
            s, [_net("LAN", "10.0.1.0/24")], [_client("10.0.1.50")]
        )
    assert result.opened == []
    assert await _findings(sessionmaker) == []


async def test_client_outside_subnet_does_not_count(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        # Client is on a different subnet than the IoT network.
        result = await reconcile_stray_config(
            s, [_net("IoT", "10.0.20.0/24")], [_client("10.0.1.50")]
        )
    assert result.opened == ["IoT"]


async def test_is_idempotent(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        first = await reconcile_stray_config(s, [_net("IoT", "10.0.20.0/24")], [])
        second = await reconcile_stray_config(s, [_net("IoT", "10.0.20.0/24")], [])
    assert first.opened == ["IoT"]
    assert second.updated == ["IoT"]
    assert len(await _findings(sessionmaker)) == 1


async def test_client_appears_resolves_finding(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_stray_config(s, [_net("IoT", "10.0.20.0/24")], [])
        result = await reconcile_stray_config(
            s, [_net("IoT", "10.0.20.0/24")], [_client("10.0.20.5")]
        )
    assert result.resolved == ["IoT"]
    findings = await _findings(sessionmaker)
    assert findings[0].status == FindingStatus.RESOLVED


async def test_recurrence_reopens_same_row(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_stray_config(s, [_net("IoT", "10.0.20.0/24")], [])
        await reconcile_stray_config(s, [_net("IoT", "10.0.20.0/24")], [_client("10.0.20.5")])
        result = await reconcile_stray_config(s, [_net("IoT", "10.0.20.0/24")], [])
    assert result.reopened == ["IoT"]
    assert len(await _findings(sessionmaker)) == 1
    findings = await _findings(sessionmaker)
    assert findings[0].status == FindingStatus.OPEN


# ---------------------------------------------------------------------------
# skips + invariant #1
# ---------------------------------------------------------------------------


async def test_network_without_subnet_is_skipped(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        result = await reconcile_stray_config(s, [_net("WAN", None)], [])
    assert result.skipped_no_subnet == 1
    assert result.opened == []


async def test_disabled_network_is_skipped(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        result = await reconcile_stray_config(
            s, [_net("OldVLAN", "10.0.99.0/24", enabled=False)], []
        )
    assert result.opened == []
    assert await _findings(sessionmaker) == []


async def test_vanished_network_not_auto_resolved(sessionmaker) -> None:
    """A network dropped from the config must not silently resolve (invariant #1)."""
    async with session_scope(sessionmaker) as s:
        await reconcile_stray_config(s, [_net("IoT", "10.0.20.0/24")], [])
        # Next run: IoT absent entirely.
        result = await reconcile_stray_config(
            s, [_net("LAN", "10.0.1.0/24")], [_client("10.0.1.5")]
        )
    assert result.resolved == []
    findings = await _findings(sessionmaker)
    iot = next(f for f in findings if "IoT" in f.title)
    assert iot.status == FindingStatus.OPEN


async def test_bad_subnet_string_is_skipped(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        result = await reconcile_stray_config(s, [_net("Bad", "not-a-cidr")], [])
    assert result.skipped_no_subnet == 1
    assert result.opened == []
