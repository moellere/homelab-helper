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
    ProxmoxTaskError,
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


_UPID = "UPID:pve1:0000ABCD:00112233:65000000:vzreboot:101:root@pam:"


def _task_adapter(statuses: list[dict[str, str] | Exception]) -> ProxmoxAdapter:
    """A guest whose power POST returns a task; each status read pops the next answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/tasks/" in request.url.path:
            assert request.url.path.endswith(f"/nodes/pve1/tasks/{_UPID}/status")
            answer = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            if isinstance(answer, Exception):
                raise answer
            return httpx.Response(200, json={"data": answer})
        return httpx.Response(200, json={"data": _UPID})

    adapter = _adapter(handler)
    adapter.task_poll_s = 0
    return adapter


async def test_write_returns_only_once_its_task_has_succeeded() -> None:
    statuses: list[dict[str, str] | Exception] = [
        {"status": "running"},
        httpx.ConnectError("blip"),
        {"status": "stopped", "exitstatus": "OK"},
    ]
    adapter = _task_adapter(statuses)
    assert await adapter.vm_power("pve1", 101, "lxc", "reboot") == _UPID
    assert len(statuses) == 1, "every earlier answer was read before the write returned"
    await adapter.aclose()


async def test_task_finished_with_warnings_still_counts_as_done() -> None:
    adapter = _task_adapter([{"status": "stopped", "exitstatus": "WARNINGS: 1"}])
    assert await adapter.migrate_guest("pve1", 101, "qemu", "pve2") == _UPID
    await adapter.aclose()


async def test_task_that_ends_in_an_error_raises() -> None:
    adapter = _task_adapter([{"status": "stopped", "exitstatus": "migration aborted"}])
    with pytest.raises(ProxmoxTaskError, match="migration aborted") as exc:
        await adapter.migrate_guest("pve1", 101, "qemu", "pve2")
    assert exc.value.upid == _UPID
    assert isinstance(exc.value, ProxmoxAPIError), "existing handlers must catch it"
    await adapter.aclose()


async def test_task_still_running_at_the_deadline_raises() -> None:
    adapter = _task_adapter([{"status": "running"}])
    with pytest.raises(ProxmoxTaskError, match="still running"):
        await adapter.wait_for_task("pve1", _UPID, timeout_s=0)
    await adapter.aclose()


async def test_a_result_that_is_no_task_id_is_returned_untouched() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/tasks/" not in request.url.path
        return httpx.Response(200, json={"data": None})

    adapter = _adapter(handler)
    assert await adapter.create_snapshot("pve1", 101, "lxc", "pre") is None
    await adapter.aclose()
