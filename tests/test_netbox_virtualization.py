"""NetBox virtualization (cluster + VM) CRUD/sync tests against MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx

from homelab_helper.adapters.netbox import (
    NetBoxAdapter,
    NetBoxConfig,
    _vm_to_netbox_payload,
)


def _adapter(handler) -> NetBoxAdapter:  # noqa: ANN001
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://netbox.example/",
        headers={"Authorization": "Token t", "Accept": "application/json"},
    )
    return NetBoxAdapter(NetBoxConfig(url="http://netbox.example/", token="t"), client=client)


def test_vm_payload_maps_bytes_to_mb_and_status() -> None:
    running = _vm_to_netbox_payload(
        {"name": "web", "status": "running", "maxcpu": 2, "maxmem_bytes": 4294967296}, 7
    )
    assert running == {
        "name": "web",
        "status": "active",
        "cluster": 7,
        "vcpus": 2.0,
        "memory": 4096,
    }
    stopped = _vm_to_netbox_payload({"name": "ct", "status": "stopped"}, 7)
    assert stopped["status"] == "offline"


async def test_sync_missing_cluster_returns_found_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # cluster lookup returns no rows
        return httpx.Response(200, json={"results": [], "next": None})

    async with _adapter(handler) as a:
        result = await a.sync_cluster_vms("lab", [{"name": "web", "status": "running"}])
    assert result.found is False
    assert "not found" in (result.reason or "")


async def test_sync_creates_updates_and_leaves_unchanged() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if path == "/api/virtualization/clusters/":
            return httpx.Response(200, json={"results": [{"id": 5, "name": "lab"}], "next": None})
        if path == "/api/virtualization/virtual-machines/" and request.method == "GET":
            # existing: "old" (matches desired) and "drift" (memory differs)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 1,
                            "name": "old",
                            "status": {"value": "active"},
                            "vcpus": 2.0,
                            "memory": 4096,
                        },
                        {
                            "id": 2,
                            "name": "drift",
                            "status": {"value": "active"},
                            "vcpus": 1.0,
                            "memory": 1024,
                        },
                    ],
                    "next": None,
                },
            )
        # POST create / PATCH update
        body: dict[str, Any] = json.loads(request.content or b"{}")
        return httpx.Response(201, json={"id": 99, **body})

    vms = [
        {"name": "old", "status": "running", "maxcpu": 2, "maxmem_bytes": 4096 * 1024 * 1024},
        {"name": "drift", "status": "running", "maxcpu": 1, "maxmem_bytes": 8192 * 1024 * 1024},
        {"name": "new", "status": "stopped", "maxcpu": 4, "maxmem_bytes": 2048 * 1024 * 1024},
    ]
    async with _adapter(handler) as a:
        result = await a.sync_cluster_vms("lab", vms)

    assert result.found is True
    assert result.unchanged == ["old"]
    assert result.updated == ["drift"]
    assert result.created == ["new"]
    # Never issues a DELETE (no reaping of operator VMs).
    assert not any(method == "DELETE" for method, _ in calls)


async def test_sync_dry_run_makes_no_writes() -> None:
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method in ("POST", "PATCH", "DELETE"):
            writes.append(request.method)
        if path == "/api/virtualization/clusters/":
            return httpx.Response(200, json={"results": [{"id": 5, "name": "lab"}], "next": None})
        return httpx.Response(200, json={"results": [], "next": None})

    async with _adapter(handler) as a:
        result = await a.sync_cluster_vms("lab", [{"name": "x", "status": "running"}], dry_run=True)
    assert result.created == ["x"]
    assert writes == []
