"""CLI test for `helper discover mikrotik` (mocked router, file-backed DB for --persist)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select
from typer.testing import CliRunner

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

import homelab_helper.cli.discover as cli_discover
from homelab_helper.adapters.mikrotik import MikroTikAdapter, MikroTikConfig
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import DiscoverySource, FindingKind
from homelab_helper.db.models import ReconciliationFinding, ServiceEndpoint
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from tests.test_mikrotik_adapter import ROUTES

runner = CliRunner()


def _mikrotik(handler: Callable[[httpx.Request], httpx.Response]) -> MikroTikAdapter:
    client = httpx.AsyncClient(
        base_url="https://r.lan/rest", transport=httpx.MockTransport(handler)
    )
    return MikroTikAdapter(
        MikroTikConfig(url="https://r.lan", username="ro", password="pw"), client=client
    )


def _handler(request: httpx.Request) -> httpx.Response:
    body = ROUTES.get(request.url.path)
    return httpx.Response(200, json=body) if body is not None else httpx.Response(404)


def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'mikrotik.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    monkeypatch.delenv("HOMELAB_HELPER_SERVICE_ALIASES", raising=False)

    async def _init() -> None:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_init())
    return url


def test_discover_mikrotik_prints_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_discover, "_load_mikrotik_adapter", lambda: _mikrotik(_handler))
    result = runner.invoke(app, ["discover", "mikrotik"])
    assert result.exit_code == 0, result.stdout
    assert "core-router" in result.stdout
    assert "vlan9-iot" in result.stdout
    assert "static DNS" in result.stdout


def test_discover_mikrotik_unreachable_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": 401})

    monkeypatch.setattr(cli_discover, "_load_mikrotik_adapter", lambda: _mikrotik(down))
    result = runner.invoke(app, ["discover", "mikrotik"])
    assert result.exit_code == 1
    assert "unreachable" in result.stdout


def test_discover_mikrotik_persist_writes_endpoints_and_stray_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_discover, "_load_mikrotik_adapter", lambda: _mikrotik(_handler))
    result = runner.invoke(app, ["discover", "mikrotik", "--persist"])
    assert result.exit_code == 0, result.stdout
    assert "endpoints" in result.stdout
    assert "stray-config" in result.stdout

    async def _rows() -> tuple[list[ServiceEndpoint], list[ReconciliationFinding]]:
        engine = make_engine(url)
        try:
            sm = make_sessionmaker(engine)
            async with sm() as s:
                eps = list((await s.execute(select(ServiceEndpoint))).scalars().all())
                findings = list((await s.execute(select(ReconciliationFinding))).scalars().all())
                return eps, findings
        finally:
            await engine.dispose()

    eps, findings = asyncio.run(_rows())
    # Enabled A/AAAA records only: ha.lan + v6.lan (nas.lan disabled, www.lan CNAME).
    assert sorted(e.hostname for e in eps) == ["ha.lan", "v6.lan"]
    assert {e.resolver for e in eps} == {"mikrotik"}
    assert {e.discovery_source for e in eps} == {DiscoverySource.MIKROTIK}
    # vlan9-iot serves 10.0.9.0/24 with no leases → stray; bridge has leases; vlan8-old is disabled.
    stray = [f for f in findings if f.kind is FindingKind.STRAY_CONFIG]
    assert [f.affected[0]["target_id"] for f in stray] == ["vlan9-iot"]


def test_discover_mikrotik_dry_run_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_discover, "_load_mikrotik_adapter", lambda: _mikrotik(_handler))
    result = runner.invoke(app, ["discover", "mikrotik", "--persist", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "rolled back" in result.stdout

    async def _count() -> int:
        engine = make_engine(url)
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as s:
                return len((await s.execute(select(ServiceEndpoint))).scalars().all())
        finally:
            await engine.dispose()

    assert asyncio.run(_count()) == 0
