"""CLI + import tests for `helper discover hass` (file-backed DB, mocked hub)."""

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
from homelab_helper.adapters.homeassistant import HomeAssistantAdapter, HomeAssistantConfig
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import DiscoverySource
from homelab_helper.db.models import Service, ServiceEndpoint
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.hass_import import import_home_assistant
from tests.test_homeassistant_adapter import CONFIG_PAYLOAD, SERVICES_PAYLOAD, STATES_PAYLOAD

runner = CliRunner()

_URL = "http://ha.example.lan:8123"


def _hass(handler: Callable[[httpx.Request], httpx.Response]) -> HomeAssistantAdapter:
    client = httpx.AsyncClient(base_url=_URL, transport=httpx.MockTransport(handler))
    return HomeAssistantAdapter(HomeAssistantConfig(url=_URL, token="tok"), client=client)


def _handler(request: httpx.Request) -> httpx.Response:
    routes = {
        "/api/": {"message": "API running."},
        "/api/config": CONFIG_PAYLOAD,
        "/api/states": STATES_PAYLOAD,
        "/api/services": SERVICES_PAYLOAD,
    }
    body = routes.get(request.url.path)
    return httpx.Response(200, json=body) if body is not None else httpx.Response(404)


def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'hass.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    async def _init() -> None:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_init())
    return url


async def _rows(url: str) -> tuple[list[Service], list[ServiceEndpoint]]:
    engine = make_engine(url)
    try:
        sm = make_sessionmaker(engine)
        async with sm() as s:
            services = list((await s.execute(select(Service))).scalars().all())
            endpoints = list((await s.execute(select(ServiceEndpoint))).scalars().all())
            return services, endpoints
    finally:
        await engine.dispose()


def test_discover_hass_prints_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_discover, "_load_hass_adapter", lambda: _hass(_handler))
    result = runner.invoke(app, ["discover", "hass"])
    assert result.exit_code == 0, result.stdout
    assert "2026.8.3" in result.stdout
    assert "proxmoxve" in result.stdout
    assert "entities by domain" in result.stdout


def test_discover_hass_unreachable_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    monkeypatch.setattr(cli_discover, "_load_hass_adapter", lambda: _hass(down))
    result = runner.invoke(app, ["discover", "hass"])
    assert result.exit_code == 1
    assert "unreachable" in result.stdout


def test_discover_hass_persist_upserts_service_and_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_discover, "_load_hass_adapter", lambda: _hass(_handler))

    first = runner.invoke(app, ["discover", "hass", "--persist"])
    assert first.exit_code == 0, first.stdout
    assert "created" in first.stdout
    second = runner.invoke(app, ["discover", "hass", "--persist"])
    assert second.exit_code == 0, second.stdout
    assert "updated" in second.stdout

    services, endpoints = asyncio.run(_rows(url))
    assert [s.name for s in services] == ["home-assistant"]
    attrs = services[0].attributes
    assert attrs["version"] == "2026.8.3"
    assert attrs["integrations"] == ["mqtt", "proxmoxve", "unifi"]
    assert attrs["entities_by_domain"] == {"light": 2, "sensor": 1}
    assert attrs["entity_count"] == 4
    assert len(endpoints) == 1
    ep = endpoints[0]
    assert ep.hostname == "ha.example.lan"
    assert ep.ip is None
    assert ep.resolver == "home-assistant"
    assert ep.discovery_source == DiscoverySource.HOME_ASSISTANT


async def test_import_ip_literal_url_fills_ip_and_preserves_foreign_attributes(
    tmp_path: Path,
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'hass-import.db'}"
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as s:
            s.add(Service(name="home-assistant", attributes={"operator_note": "keep"}))
        async with session_scope(sm) as s:
            result = await import_home_assistant(
                s,
                config={"version": "1", "components": ["mqtt"]},
                states=[],
                service_domains=[],
                url="https://10.0.1.5:8123",
            )
        assert result.created is False
        assert result.endpoint_hostname == "10.0.1.5"
        services, endpoints = await _rows(url)
        assert services[0].attributes["operator_note"] == "keep"
        assert services[0].attributes["integrations"] == ["mqtt"]
        assert endpoints[0].ip == "10.0.1.5"
    finally:
        await engine.dispose()
