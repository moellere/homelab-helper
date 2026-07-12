"""Tests for the internal-endpoint DNS reconcile (Service + ServiceEndpoint)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import ResolutionScope
from homelab_helper.db.models import Service, ServiceEndpoint
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.dns_reconcile import (
    _address_records,
    reconcile_external_endpoints,
    reconcile_internal_endpoints,
)


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


def _rec(hostname: str, ip: str, record_type: str = "A", enabled: bool = True) -> dict:
    return {"hostname": hostname, "value": ip, "record_type": record_type, "enabled": enabled}


# ---------------------------------------------------------------------------
# _address_records (pure)
# ---------------------------------------------------------------------------


def test_address_records_keeps_only_address_types() -> None:
    recs = [
        _rec("a.lan", "10.0.0.1", "A"),
        _rec("b.lan", "fd00::1", "AAAA"),
        _rec("c.lan", "a.lan", "CNAME"),
        {"hostname": "d.lan", "value": "text", "record_type": "TXT"},
    ]
    out = _address_records(recs)
    assert out == {"a.lan": "10.0.0.1", "b.lan": "fd00::1"}


def test_address_records_skips_disabled_and_lowercases() -> None:
    recs = [_rec("NAS.LAN", "10.0.0.5"), _rec("off.lan", "10.0.0.9", enabled=False)]
    out = _address_records(recs)
    assert out == {"nas.lan": "10.0.0.5"}


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


async def test_reconcile_creates_service_and_internal_endpoint(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        result = await reconcile_internal_endpoints(s, [_rec("nas.lan", "10.0.1.5")])
    assert result.created == ["nas.lan"]

    async with sessionmaker() as s:
        svc = (await s.execute(select(Service))).scalar_one()
        ep = (await s.execute(select(ServiceEndpoint))).scalar_one()
        assert svc.name == "nas.lan"
        assert ep.scope == ResolutionScope.INTERNAL
        assert ep.resolver == "unifi"
        assert ep.ip == "10.0.1.5"
        assert ep.service_id == svc.id


async def test_reconcile_is_idempotent(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        first = await reconcile_internal_endpoints(s, [_rec("nas.lan", "10.0.1.5")])
        second = await reconcile_internal_endpoints(s, [_rec("nas.lan", "10.0.1.5")])
    assert first.created == ["nas.lan"]
    assert second.created == []
    assert second.unchanged == ["nas.lan"]

    async with sessionmaker() as s:
        assert len((await s.execute(select(ServiceEndpoint))).scalars().all()) == 1


async def test_reconcile_updates_ip_in_place(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_internal_endpoints(s, [_rec("nas.lan", "10.0.1.5")])
        result = await reconcile_internal_endpoints(s, [_rec("nas.lan", "10.0.1.9")])
    assert result.updated == ["nas.lan"]
    async with sessionmaker() as s:
        ep = (await s.execute(select(ServiceEndpoint))).scalar_one()
        assert ep.ip == "10.0.1.9"


async def test_reconcile_removes_deleted_records_and_orphan_service(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_internal_endpoints(
            s, [_rec("nas.lan", "10.0.1.5"), _rec("ha.lan", "10.0.1.6")]
        )
        # ha.lan dropped from the source.
        result = await reconcile_internal_endpoints(s, [_rec("nas.lan", "10.0.1.5")])
    assert result.removed == ["ha.lan"]
    async with sessionmaker() as s:
        names = {sv.name for sv in (await s.execute(select(Service))).scalars().all()}
        assert names == {"nas.lan"}  # orphaned ha.lan service cleaned up


async def test_reconcile_leaves_other_resolver_endpoints_untouched(sessionmaker) -> None:
    """An external (e.g. cloudflare) endpoint on the same service must survive."""
    async with session_scope(sessionmaker) as s:
        # Seed a service with a pre-existing external endpoint.
        svc = Service(name="nas.lan")
        s.add(svc)
        await s.flush()
        s.add(
            ServiceEndpoint(
                service_id=svc.id,
                scope=ResolutionScope.EXTERNAL,
                hostname="nas.lan",
                ip="203.0.113.9",
                resolver="cloudflare",
            )
        )
        await s.flush()

        # Reconcile internal (unifi) — must not touch the external row.
        await reconcile_internal_endpoints(s, [_rec("nas.lan", "10.0.1.5")])

    async with sessionmaker() as s:
        eps = (await s.execute(select(ServiceEndpoint))).scalars().all()
        by_scope = {ep.scope: ep for ep in eps}
        assert by_scope[ResolutionScope.EXTERNAL].ip == "203.0.113.9"
        assert by_scope[ResolutionScope.INTERNAL].ip == "10.0.1.5"


async def test_reconcile_removal_keeps_service_with_surviving_endpoint(sessionmaker) -> None:
    """Removing the internal endpoint must not delete a service that still has an external one."""
    async with session_scope(sessionmaker) as s:
        svc = Service(name="nas.lan")
        s.add(svc)
        await s.flush()
        s.add(
            ServiceEndpoint(
                service_id=svc.id,
                scope=ResolutionScope.EXTERNAL,
                hostname="nas.lan",
                ip="203.0.113.9",
                resolver="cloudflare",
            )
        )
        await s.flush()
        await reconcile_internal_endpoints(s, [_rec("nas.lan", "10.0.1.5")])
        # Now drop the internal record entirely.
        result = await reconcile_internal_endpoints(s, [])

    assert result.removed == ["nas.lan"]
    async with sessionmaker() as s:
        assert len((await s.execute(select(Service))).scalars().all()) == 1  # survived
        eps = (await s.execute(select(ServiceEndpoint))).scalars().all()
        assert len(eps) == 1
        assert eps[0].scope == ResolutionScope.EXTERNAL


# ---------------------------------------------------------------------------
# external reconcile (Cloudflare) + split-brain pairing
# ---------------------------------------------------------------------------


async def test_external_reconcile_creates_cloudflare_endpoint(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        result = await reconcile_external_endpoints(s, [_rec("ha.example.com", "203.0.113.9")])
    assert result.created == ["ha.example.com"]
    async with sessionmaker() as s:
        ep = (await s.execute(select(ServiceEndpoint))).scalar_one()
        assert ep.scope == ResolutionScope.EXTERNAL
        assert ep.resolver == "cloudflare"
        assert ep.ip == "203.0.113.9"


async def test_external_reconcile_leaves_internal_untouched(sessionmaker) -> None:
    """Reciprocal scope discipline: an external sync must not touch internal rows."""
    async with session_scope(sessionmaker) as s:
        await reconcile_internal_endpoints(s, [_rec("ha.lan", "10.0.1.50")])
        await reconcile_external_endpoints(s, [_rec("ha.lan", "203.0.113.9")])
        # Now the external source drops the record entirely.
        result = await reconcile_external_endpoints(s, [])
    assert result.removed == ["ha.lan"]
    async with sessionmaker() as s:
        eps = (await s.execute(select(ServiceEndpoint))).scalars().all()
        assert len(eps) == 1
        assert eps[0].scope == ResolutionScope.INTERNAL
        assert eps[0].ip == "10.0.1.50"


async def test_internal_and_external_pair_on_one_service(sessionmaker) -> None:
    """Same hostname from both resolvers lands as two endpoints on one Service."""
    async with session_scope(sessionmaker) as s:
        await reconcile_internal_endpoints(s, [_rec("ha.lan", "10.0.1.50")])
        await reconcile_external_endpoints(s, [_rec("ha.lan", "203.0.113.9")])
    async with sessionmaker() as s:
        services = (await s.execute(select(Service))).scalars().all()
        assert len(services) == 1  # one service, two scopes
        eps = (await s.execute(select(ServiceEndpoint))).scalars().all()
        by_scope = {ep.scope: ep.ip for ep in eps}
        assert by_scope[ResolutionScope.INTERNAL] == "10.0.1.50"
        assert by_scope[ResolutionScope.EXTERNAL] == "203.0.113.9"  # split-brain, ready for `view`
