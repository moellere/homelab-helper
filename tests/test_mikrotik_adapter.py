"""MikroTikAdapter unit tests against httpx.MockTransport — no live router."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from homelab_helper.adapters.mikrotik import (
    MikroTikAdapter,
    MikroTikAPIError,
    MikroTikConfig,
    MikroTikConfigError,
    parse_address,
    parse_lease,
    parse_resource,
    parse_static_dns,
)

_CONFIG = MikroTikConfig(url="https://router.example.lan", username="ro", password="pw")

IDENTITY = {"name": "core-router"}
RESOURCE = {
    "version": "7.16.1 (stable)",
    "board-name": "RB5009UG+S+",
    "architecture-name": "arm64",
    "cpu": "ARM64",
    "cpu-count": "4",
    "total-memory": "1073741824",
    "uptime": "3w2d",
}
INTERFACES = [
    {
        ".id": "*1",
        "name": "ether1",
        "type": "ether",
        "mac-address": "DC:2C:6E:00:00:01",
        "running": "true",
        "disabled": "false",
    },
    {
        ".id": "*2",
        "name": "bridge",
        "type": "bridge",
        "mac-address": "DC:2C:6E:00:00:02",
        "running": "true",
        "disabled": "false",
    },
]
ADDRESSES = [
    {".id": "*1", "address": "10.0.1.1/24", "interface": "bridge", "disabled": "false"},
    {
        ".id": "*2",
        "address": "10.0.9.1/24",
        "interface": "vlan9-iot",
        "disabled": "false",
        "comment": "IoT",
    },
    {".id": "*3", "address": "10.0.8.1/24", "interface": "vlan8-old", "disabled": "true"},
]
LEASES = [
    {
        ".id": "*1",
        "address": "10.0.1.50",
        "mac-address": "AA:BB:CC:00:00:50",
        "host-name": "ha",
        "dynamic": "false",
        "server": "lan",
        "status": "bound",
    },
    {
        ".id": "*2",
        "address": "10.0.1.51",
        "mac-address": "AA:BB:CC:00:00:51",
        "comment": "printer",
        "dynamic": "true",
        "server": "lan",
        "status": "bound",
    },
]
STATIC_DNS = [
    {".id": "*1", "name": "ha.lan", "address": "10.0.1.50", "ttl": "1d"},
    {".id": "*2", "name": "nas.lan", "address": "10.0.1.60", "type": "A", "disabled": "true"},
    {".id": "*3", "name": "www.lan", "cname": "ha.lan", "type": "CNAME"},
    {".id": "*4", "name": "v6.lan", "address": "fd00::1", "type": "AAAA"},
]

ROUTES = {
    "/rest/system/identity": IDENTITY,
    "/rest/system/resource": RESOURCE,
    "/rest/interface": INTERFACES,
    "/rest/ip/address": ADDRESSES,
    "/rest/ip/dhcp-server/lease": LEASES,
    "/rest/ip/dns/static": STATIC_DNS,
}


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> MikroTikAdapter:
    client = httpx.AsyncClient(
        base_url=_CONFIG.url + "/rest",
        auth=(_CONFIG.username, _CONFIG.password),
        transport=httpx.MockTransport(handler),
    )
    return MikroTikAdapter(_CONFIG, client=client)


def _handler(request: httpx.Request) -> httpx.Response:
    if not request.headers.get("Authorization", "").startswith("Basic "):
        return httpx.Response(401, json={"error": 401, "message": "Unauthorized"})
    body = ROUTES.get(request.url.path)
    return (
        httpx.Response(200, json=body)
        if body is not None
        else httpx.Response(404, text="no such item")
    )


def test_config_from_env_requires_all_three(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_MIKROTIK_URL", "https://r.lan")
    monkeypatch.setenv("HOMELAB_HELPER_MIKROTIK_USERNAME", "ro")
    monkeypatch.delenv("HOMELAB_HELPER_MIKROTIK_PASSWORD", raising=False)
    with pytest.raises(MikroTikConfigError):
        MikroTikConfig.from_env()


def test_config_from_env_resolver_and_ssl_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_MIKROTIK_URL", "https://r.lan")
    monkeypatch.setenv("HOMELAB_HELPER_MIKROTIK_USERNAME", "ro")
    monkeypatch.setenv("HOMELAB_HELPER_MIKROTIK_PASSWORD", "pw")
    monkeypatch.delenv("HOMELAB_HELPER_MIKROTIK_NAME", raising=False)
    monkeypatch.delenv("HOMELAB_HELPER_MIKROTIK_VERIFY_SSL", raising=False)
    cfg = MikroTikConfig.from_env()
    assert cfg.verify_ssl is False
    assert cfg.resolver == "mikrotik"
    monkeypatch.setenv("HOMELAB_HELPER_MIKROTIK_NAME", "wyola")
    assert MikroTikConfig.from_env().resolver == "mikrotik:wyola"


def test_parsers_map_onto_unifi_shapes() -> None:
    res = parse_resource(RESOURCE)
    assert res["board"] == "RB5009UG+S+"
    assert res["cpu_count"] == 4
    assert res["memory_bytes"] == 1073741824

    net = parse_address(ADDRESSES[1])
    assert net == {
        "name": "vlan9-iot",
        "vlan_id": None,
        "subnet": "10.0.9.1/24",
        "purpose": "corporate",
        "enabled": True,
        "vlan_enabled": False,
        "router_ip": "10.0.9.1",
        "comment": "IoT",
    }
    assert parse_address(ADDRESSES[2])["enabled"] is False

    lease = parse_lease(LEASES[0])
    assert lease["hostname"] == "ha"
    assert lease["ip"] == "10.0.1.50"
    assert lease["mac"] == "aa:bb:cc:00:00:50"
    assert lease["fixed"] is True
    assert parse_lease(LEASES[1])["hostname"] == "printer"  # comment fallback

    a = parse_static_dns(STATIC_DNS[0])
    assert a == {
        "hostname": "ha.lan",
        "value": "10.0.1.50",
        "record_type": "A",
        "enabled": True,
        "ttl": "1d",
    }
    assert parse_static_dns(STATIC_DNS[1])["enabled"] is False
    cname = parse_static_dns(STATIC_DNS[2])
    assert (cname["record_type"], cname["value"]) == ("CNAME", "ha.lan")
    assert parse_static_dns(STATIC_DNS[3])["record_type"] == "AAAA"


async def test_reads_use_basic_auth_and_rest_prefix() -> None:
    adapter = _adapter(_handler)
    try:
        assert await adapter.identity() == "core-router"
        assert (await adapter.resource())["version"].startswith("7.16")
        assert [i["name"] for i in await adapter.list_interfaces()] == ["ether1", "bridge"]
        assert [n["subnet"] for n in await adapter.list_addresses()] == [
            "10.0.1.1/24",
            "10.0.9.1/24",
            "10.0.8.1/24",
        ]
        assert [c["ip"] for c in await adapter.list_leases()] == ["10.0.1.50", "10.0.1.51"]
        assert [r["hostname"] for r in await adapter.list_dns_records()] == [
            "ha.lan",
            "nas.lan",
            "www.lan",
            "v6.lan",
        ]
        ok, err = await adapter.health_check()
        assert ok is True
        assert err is None
    finally:
        await adapter.aclose()


async def test_health_check_reports_auth_failure_and_errors_carry_path() -> None:
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": 401, "message": "Unauthorized"})

    adapter = _adapter(deny)
    ok, err = await adapter.health_check()
    assert ok is False
    assert err is not None
    assert "401" in err
    with pytest.raises(MikroTikAPIError) as excinfo:
        await adapter.list_leases()
    assert "/ip/dhcp-server/lease" in str(excinfo.value)


async def test_injected_client_is_not_closed() -> None:
    adapter = _adapter(_handler)
    client = adapter.client
    await adapter.aclose()
    assert not client.is_closed
    await client.aclose()


def test_built_client_targets_rest_with_basic_auth() -> None:
    adapter = MikroTikAdapter(_CONFIG)
    client = adapter.client
    assert str(client.base_url).rstrip("/") == "https://router.example.lan/rest"
    assert client.auth is not None
