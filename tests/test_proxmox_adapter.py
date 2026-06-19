"""ProxmoxAdapter unit tests against httpx.MockTransport — no live cluster."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from homelab_helper.adapters.proxmox import (
    ProxmoxAdapter,
    ProxmoxAPIError,
    ProxmoxConfig,
    parse_cluster_status,
    parse_vm_resource,
)

_CONFIG = ProxmoxConfig(url="https://pve.example.lan:8006", token_id="u@pam!t", token_secret="sek")


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> ProxmoxAdapter:
    client = httpx.AsyncClient(
        base_url=_CONFIG.url.rstrip("/") + "/api2/json",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"PVEAPIToken={_CONFIG.token_id}={_CONFIG.token_secret}"},
    )
    return ProxmoxAdapter(_CONFIG, client=client)


def test_parse_cluster_status_single_node_has_no_cluster_row() -> None:
    rows = [{"type": "node", "name": "pve1", "ip": "192.0.2.5", "online": 1, "nodeid": 1}]
    out = parse_cluster_status(rows)
    assert out["name"] is None
    assert out["node_count"] == 1
    assert out["nodes"][0]["online"] is True


def test_parse_cluster_status_clustered() -> None:
    rows = [
        {"type": "cluster", "name": "lab", "nodes": 2, "quorate": 1},
        {"type": "node", "name": "pve1", "online": 1},
        {"type": "node", "name": "pve2", "online": 0},
    ]
    out = parse_cluster_status(rows)
    assert out["name"] == "lab"
    assert out["quorate"] is True
    assert out["node_count"] == 2


def test_parse_vm_resource_maps_fields() -> None:
    raw = {
        "id": "qemu/100",
        "vmid": 100,
        "name": "web",
        "node": "pve1",
        "type": "qemu",
        "status": "running",
        "maxcpu": 2,
        "maxmem": 4294967296,
        "maxdisk": 68719476736,
        "template": 0,
        "uptime": 1000,
    }
    vm = parse_vm_resource(raw)
    assert vm["vmid"] == 100
    assert vm["type"] == "qemu"
    assert vm["maxmem_bytes"] == 4294967296
    assert vm["template"] is False


async def test_request_unwraps_data_envelope_and_lists_vms() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api2/json/cluster/resources"
        assert request.url.params.get("type") == "vm"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "vmid": 100,
                        "name": "web",
                        "node": "pve1",
                        "type": "qemu",
                        "status": "running",
                    },
                    {"vmid": 200, "name": "ct", "node": "pve2", "type": "lxc", "status": "stopped"},
                ]
            },
        )

    async with _adapter(handler) as adapter:
        vms = await adapter.list_vms()
    assert [v["name"] for v in vms] == ["web", "ct"]
    assert [v["type"] for v in vms] == ["qemu", "lxc"]


async def test_auth_header_sent_and_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "PVEAPIToken=u@pam!t=sek"
        return httpx.Response(401, text="authentication failure")

    async with _adapter(handler) as adapter:
        with pytest.raises(ProxmoxAPIError, match="401"):
            await adapter.version()


async def test_health_check_ok_and_failure() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"version": "8.2"}})

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="permission denied")

    async with _adapter(ok) as adapter:
        assert await adapter.health_check() == (True, None)
    async with _adapter(bad) as adapter:
        good, err = await adapter.health_check()
        assert good is False
        assert err is not None
