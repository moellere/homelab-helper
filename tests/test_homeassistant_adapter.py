"""HomeAssistantAdapter unit tests against httpx.MockTransport — no live hub."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from homelab_helper.adapters.homeassistant import (
    HomeAssistantAdapter,
    HomeAssistantAPIError,
    HomeAssistantConfig,
    HomeAssistantConfigError,
    integrations_of_interest,
    parse_config,
    parse_state,
    summarize_states,
)

_CONFIG = HomeAssistantConfig(url="http://ha.example.lan:8123", token="tok")

CONFIG_PAYLOAD = {
    "version": "2026.8.3",
    "location_name": "Home",
    "time_zone": "America/Chicago",
    "state": "RUNNING",
    "internal_url": "http://ha.example.lan:8123",
    "external_url": None,
    "components": ["sensor", "light", "proxmoxve", "unifi", "mqtt", "automation"],
}

STATES_PAYLOAD = [
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"}},
    {"entity_id": "light.porch", "state": "off", "attributes": {}},
    {"entity_id": "sensor.cpu", "state": "12", "attributes": {"friendly_name": "CPU"}},
    {"entity_id": "bogus", "state": "x", "attributes": {}},
]

SERVICES_PAYLOAD = [
    {"domain": "light", "services": {"turn_on": {}}},
    {"domain": "switch", "services": {"toggle": {}}},
]


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> HomeAssistantAdapter:
    client = httpx.AsyncClient(
        base_url=_CONFIG.url.rstrip("/"),
        headers={"Authorization": f"Bearer {_CONFIG.token}"},
        transport=httpx.MockTransport(handler),
    )
    return HomeAssistantAdapter(_CONFIG, client=client)


def _handler(request: httpx.Request) -> httpx.Response:
    if request.headers.get("Authorization") != "Bearer tok":
        return httpx.Response(401, json={"message": "Unauthorized"})
    routes = {
        "/api/": {"message": "API running."},
        "/api/config": CONFIG_PAYLOAD,
        "/api/states": STATES_PAYLOAD,
        "/api/services": SERVICES_PAYLOAD,
    }
    body = routes.get(request.url.path)
    if body is None:
        return httpx.Response(404, text="not found")
    return httpx.Response(200, json=body)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_from_env_requires_url_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_HASS_URL", "http://ha.lan:8123")
    monkeypatch.delenv("HOMELAB_HELPER_HASS_TOKEN", raising=False)
    with pytest.raises(HomeAssistantConfigError):
        HomeAssistantConfig.from_env()


def test_config_from_env_ok_verify_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_HASS_URL", "http://ha.lan:8123")
    monkeypatch.setenv("HOMELAB_HELPER_HASS_TOKEN", "t")
    monkeypatch.delenv("HOMELAB_HELPER_HASS_VERIFY_SSL", raising=False)
    cfg = HomeAssistantConfig.from_env()
    assert cfg.url == "http://ha.lan:8123"
    assert cfg.verify_ssl is True
    monkeypatch.setenv("HOMELAB_HELPER_HASS_VERIFY_SSL", "no")
    assert HomeAssistantConfig.from_env().verify_ssl is False


def test_adapter_needs_config_or_client() -> None:
    with pytest.raises(HomeAssistantConfigError):
        HomeAssistantAdapter()


# ---------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------


def test_parse_config_sorts_components_and_keeps_identity() -> None:
    parsed = parse_config(CONFIG_PAYLOAD)
    assert parsed["version"] == "2026.8.3"
    assert parsed["location_name"] == "Home"
    assert parsed["components"] == sorted(CONFIG_PAYLOAD["components"])


def test_parse_state_derives_domain_and_name() -> None:
    parsed = parse_state(STATES_PAYLOAD[0])
    assert parsed == {
        "entity_id": "light.kitchen",
        "domain": "light",
        "state": "on",
        "name": "Kitchen",
        "last_changed": None,
    }
    assert parse_state(STATES_PAYLOAD[3])["domain"] is None


def test_summarize_states_counts_by_domain_and_skips_undomained() -> None:
    states = [parse_state(s) for s in STATES_PAYLOAD]
    assert summarize_states(states) == {"light": 2, "sensor": 1}


def test_integrations_of_interest_filters_and_sorts() -> None:
    assert integrations_of_interest(CONFIG_PAYLOAD["components"]) == ["mqtt", "proxmoxve", "unifi"]


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


async def test_get_config_list_states_and_services() -> None:
    adapter = _adapter(_handler)
    try:
        config = await adapter.get_config()
        states = await adapter.list_states()
        domains = await adapter.list_service_domains()
    finally:
        await adapter.aclose()
    assert config["version"] == "2026.8.3"
    assert [s["entity_id"] for s in states] == [
        "light.kitchen",
        "light.porch",
        "sensor.cpu",
        "bogus",
    ]
    assert domains == ["light", "switch"]


async def test_health_check_reports_auth_failure() -> None:
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    adapter = _adapter(deny)
    ok, err = await adapter.health_check()
    assert ok is False
    assert err is not None
    assert "401" in err


async def test_api_error_carries_status_and_path() -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    adapter = _adapter(missing)
    with pytest.raises(HomeAssistantAPIError) as excinfo:
        await adapter.get_config()
    assert excinfo.value.status_code == 500
    assert "/api/config" in str(excinfo.value)


async def test_injected_client_is_not_closed_by_adapter() -> None:
    adapter = _adapter(_handler)
    client = adapter.client
    await adapter.aclose()
    assert not client.is_closed
    await client.aclose()


def test_built_client_sends_bearer_header() -> None:
    adapter = HomeAssistantAdapter(_CONFIG)
    client = adapter.client
    assert client.headers["Authorization"] == "Bearer tok"
    assert str(client.base_url).rstrip("/") == "http://ha.example.lan:8123"
