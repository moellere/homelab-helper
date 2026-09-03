"""Tests for discover._resolve_host — match by hostname OR primary_ip.

Regression guard: a probe run passing a short name must not duplicate a row
another source (e.g. the scan importer) created under an FQDN at the same IP.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import DiscoverySource
from homelab_helper.db.models import Host
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.host_probe import resolve_host as _resolve_host


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


async def test_resolve_host_matches_existing_by_ip_not_just_name(sessionmaker) -> None:
    # Scan importer created an FQDN row at this IP.
    async with session_scope(sessionmaker) as s:
        s.add(
            Host(
                hostname="node4.example.lan",
                primary_ip="10.0.6.24",
                discovery_source=DiscoverySource.MANUAL,
            )
        )

    # A probe run resolves with the short name + same IP — must reuse the row.
    async with session_scope(sessionmaker) as s:
        host = await _resolve_host(s, "node4", "10.0.6.24")
        assert host.hostname == "node4.example.lan"

    async with sessionmaker() as s:
        assert (await s.execute(select(func.count(Host.id)))).scalar_one() == 1


async def test_resolve_host_creates_when_no_match(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _resolve_host(s, "newbox", "10.0.0.9")
        assert host.hostname == "newbox"
        assert host.primary_ip == "10.0.0.9"

    async with sessionmaker() as s:
        assert (await s.execute(select(func.count(Host.id)))).scalar_one() == 1


async def test_resolve_host_backfills_missing_ip(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(Host(hostname="nas0", primary_ip=None))

    async with session_scope(sessionmaker) as s:
        host = await _resolve_host(s, "nas0", "10.0.6.29")
        assert host.primary_ip == "10.0.6.29"
