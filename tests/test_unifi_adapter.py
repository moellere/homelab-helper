"""UniFiAdapter unit tests against httpx.MockTransport — no live controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from homelab_helper.adapters.unifi import (
    UniFiAdapter,
    UniFiAPIError,
    UniFiConfig,
    UniFiConfigError,
    parse_client,
    parse_dns_record,
    parse_network,
)

_CONFIG = UniFiConfig(url="https://unifi.example.lan", api_key="key123", site="default")


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> UniFiAdapter:
    client = httpx.AsyncClient(
        base_url=_CONFIG.url.rstrip("/") + "/proxy/network",
        transport=httpx.MockTransport(handler),
        headers={"X-API-KEY": _CONFIG.api_key},
    )
    return UniFiAdapter(_CONFIG, client=client)


def _wrap(rows: list[dict]) -> dict:
    """UniFi wraps list payloads under 'data'."""
    return {"meta": {"rc": "ok"}, "data": rows}


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_from_env_requires_url_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_UNIFI_URL", raising=False)
    monkeypatch.delenv("HOMELAB_HELPER_UNIFI_API_KEY", raising=False)
    with pytest.raises(UniFiConfigError):
        UniFiConfig.from_env()


def test_config_from_env_defaults_site(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_URL", "https://u.lan")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_API_KEY", "k")
    monkeypatch.delenv("HOMELAB_HELPER_UNIFI_SITE", raising=False)
    cfg = UniFiConfig.from_env()
    assert cfg.site == "default"
    assert cfg.verify_ssl is False


def test_config_from_env_reads_site_and_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_URL", "https://u.lan")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_API_KEY", "k")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_SITE", "lab")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_VERIFY_SSL", "true")
    cfg = UniFiConfig.from_env()
    assert cfg.site == "lab"
    assert cfg.verify_ssl is True


def test_adapter_requires_config_or_client() -> None:
    with pytest.raises(UniFiConfigError):
        UniFiAdapter()


# ---------------------------------------------------------------------------
# parse functions (pure)
# ---------------------------------------------------------------------------


def test_parse_dns_record_maps_key_and_value() -> None:
    out = parse_dns_record(
        {"key": "nas.lan", "value": "10.0.1.5", "record_type": "A", "enabled": True}
    )
    assert out["hostname"] == "nas.lan"
    assert out["value"] == "10.0.1.5"
    assert out["record_type"] == "A"
    assert out["enabled"] is True


def test_parse_dns_record_defaults_record_type_to_a() -> None:
    out = parse_dns_record({"key": "x.lan", "value": "10.0.0.9"})
    assert out["record_type"] == "A"


def test_parse_client_prefers_fixed_ip_when_reserved() -> None:
    out = parse_client(
        {
            "hostname": "printer",
            "ip": "10.0.1.50",
            "use_fixedip": True,
            "fixed_ip": "10.0.1.10",
            "mac": "AA:BB:CC:DD:EE:FF",
        }
    )
    assert out["ip"] == "10.0.1.10"  # reservation wins over last-seen lease
    assert out["fixed"] is True
    assert out["mac"] == "aa:bb:cc:dd:ee:ff"


def test_parse_client_falls_back_to_lease_ip() -> None:
    out = parse_client({"name": "laptop", "ip": "10.0.1.77", "use_fixedip": False})
    assert out["hostname"] == "laptop"  # name used when hostname absent
    assert out["ip"] == "10.0.1.77"
    assert out["fixed"] is False


def test_parse_network_extracts_vlan_and_subnet() -> None:
    out = parse_network(
        {
            "name": "IoT",
            "vlan": 20,
            "ip_subnet": "10.0.20.1/24",
            "purpose": "corporate",
            "enabled": True,
        }
    )
    assert out["name"] == "IoT"
    assert out["vlan_id"] == 20
    assert out["subnet"] == "10.0.20.1/24"


def test_parse_network_vlan_none_when_absent() -> None:
    out = parse_network({"name": "Default", "ip_subnet": "10.0.1.1/24"})
    assert out["vlan_id"] is None


# ---------------------------------------------------------------------------
# reads over the mock transport
# ---------------------------------------------------------------------------


async def test_list_dns_records_hits_site_path() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("x-api-key", "")
        return httpx.Response(
            200, json=_wrap([{"key": "nas.lan", "value": "10.0.1.5", "record_type": "A"}])
        )

    adapter = _adapter(handler)
    try:
        records = await adapter.list_dns_records()
    finally:
        await adapter.aclose()
    assert seen["path"] == "/proxy/network/v2/api/site/default/static-dns"
    assert seen["key"] == "key123"
    assert records[0]["hostname"] == "nas.lan"


async def test_list_clients_unwraps_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_wrap(
                [
                    {"hostname": "nas", "ip": "10.0.1.5", "mac": "aa:bb:cc:00:11:22"},
                    {"name": "pi", "ip": "10.0.1.6"},
                ]
            ),
        )

    adapter = _adapter(handler)
    try:
        clients = await adapter.list_clients()
    finally:
        await adapter.aclose()
    assert [c["hostname"] for c in clients] == ["nas", "pi"]


async def test_list_networks_maps_vlans() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_wrap(
                [
                    {"name": "Default", "ip_subnet": "10.0.1.1/24", "purpose": "corporate"},
                    {"name": "IoT", "vlan": 20, "ip_subnet": "10.0.20.1/24"},
                ]
            ),
        )

    adapter = _adapter(handler)
    try:
        nets = await adapter.list_networks()
    finally:
        await adapter.aclose()
    assert {n["name"] for n in nets} == {"Default", "IoT"}
    iot = next(n for n in nets if n["name"] == "IoT")
    assert iot["vlan_id"] == 20


async def test_site_path_uses_configured_site() -> None:
    cfg = UniFiConfig(url="https://u.lan", api_key="k", site="lab")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=_wrap([]))

    client = httpx.AsyncClient(
        base_url="https://u.lan/proxy/network", transport=httpx.MockTransport(handler)
    )
    adapter = UniFiAdapter(cfg, client=client)
    try:
        await adapter.list_dns_records()
    finally:
        await adapter.aclose()
    assert "/v2/api/site/lab/" in seen["path"]


async def test_api_error_raised_on_non_2xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="api.err.Invalid")

    adapter = _adapter(handler)
    try:
        with pytest.raises(UniFiAPIError) as excinfo:
            await adapter.list_dns_records()
    finally:
        await adapter.aclose()
    assert excinfo.value.status_code == 401


async def test_health_check_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_wrap([{"name": "default"}]))

    adapter = _adapter(handler)
    try:
        ok, err = await adapter.health_check()
    finally:
        await adapter.aclose()
    assert ok is True
    assert err is None


async def test_health_check_reports_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    adapter = _adapter(handler)
    try:
        ok, err = await adapter.health_check()
    finally:
        await adapter.aclose()
    assert ok is False
    assert err is not None


# ---------------------------------------------------------------------------
# multi-controller configuration
# ---------------------------------------------------------------------------


def test_resolver_tag_distinguishes_controllers() -> None:
    """Endpoints are keyed by (scope, resolver), so two gateways must not share
    a resolver or each sync would reap the other's rows."""
    assert UniFiConfig(url="u", api_key="k").resolver == "unifi"
    assert UniFiConfig(url="u", api_key="k", name="covington").resolver == "unifi:covington"
    assert UniFiConfig(url="u", api_key="k", name="wyola").resolver == "unifi:wyola"


def test_all_from_env_returns_single_controller_when_list_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_UNIFI_CONTROLLERS", raising=False)
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_URL", "https://gw.lan")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_API_KEY", "k1")
    configs = UniFiConfig.all_from_env()
    assert [c.name for c in configs] == ["default"]
    assert configs[0].resolver == "unifi"


def test_all_from_env_reads_each_named_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_CONTROLLERS", "covington, wyola")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_COVINGTON_URL", "https://10.250.0.1")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_COVINGTON_API_KEY", "key-a")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_WYOLA_URL", "https://10.254.0.1")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_WYOLA_API_KEY", "key-b")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_WYOLA_SITE", "wyola-site")

    configs = UniFiConfig.all_from_env()

    assert [c.name for c in configs] == ["covington", "wyola"]
    # A key is per-controller — one gateway's key is rejected by another.
    assert [c.api_key for c in configs] == ["key-a", "key-b"]
    assert [c.resolver for c in configs] == ["unifi:covington", "unifi:wyola"]
    assert configs[1].site == "wyola-site"
    assert configs[0].site == "default"


def test_all_from_env_names_the_controller_that_is_missing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_CONTROLLERS", "covington,wyola")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_COVINGTON_URL", "https://10.250.0.1")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_COVINGTON_API_KEY", "key-a")
    monkeypatch.delenv("HOMELAB_HELPER_UNIFI_WYOLA_URL", raising=False)
    monkeypatch.delenv("HOMELAB_HELPER_UNIFI_WYOLA_API_KEY", raising=False)

    with pytest.raises(UniFiConfigError) as exc:
        UniFiConfig.all_from_env()
    assert "wyola" in str(exc.value)
    assert "HOMELAB_HELPER_UNIFI_WYOLA_URL" in str(exc.value)


def test_hyphenated_controller_name_maps_to_underscored_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_CONTROLLERS", "site-b")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_SITE_B_URL", "https://gw-b.lan")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_SITE_B_API_KEY", "key-b")
    (config,) = UniFiConfig.all_from_env()
    assert config.name == "site-b"
    assert config.resolver == "unifi:site-b"
