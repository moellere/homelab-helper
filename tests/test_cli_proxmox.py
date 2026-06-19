"""CLI tests for `helper discover proxmox` — adapters mocked via MockTransport."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from collections.abc import Callable

import homelab_helper.cli.discover as cli_discover
from homelab_helper.adapters.netbox import NetBoxAdapter, NetBoxConfig
from homelab_helper.adapters.proxmox import ProxmoxAdapter, ProxmoxConfig
from homelab_helper.cli.main import app

runner = CliRunner()


def _proxmox(handler: Callable[[httpx.Request], httpx.Response]) -> ProxmoxAdapter:
    cfg = ProxmoxConfig(url="https://pve.example.lan:8006", token_id="u@pam!t", token_secret="s")
    client = httpx.AsyncClient(
        base_url=cfg.url + "/api2/json", transport=httpx.MockTransport(handler)
    )
    return ProxmoxAdapter(cfg, client=client)


def _netbox(handler: Callable[[httpx.Request], httpx.Response]) -> NetBoxAdapter:
    client = httpx.AsyncClient(
        base_url="http://netbox.example/", transport=httpx.MockTransport(handler)
    )
    return NetBoxAdapter(NetBoxConfig(url="http://netbox.example/", token="t"), client=client)


def _proxmox_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/version"):
        return httpx.Response(200, json={"data": {"version": "8.2"}})
    if path.endswith("/cluster/status"):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"type": "cluster", "name": "lab", "quorate": 1},
                    {"type": "node", "name": "pve1", "online": 1},
                ]
            },
        )
    if path.endswith("/cluster/resources"):
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
                        "maxcpu": 2,
                        "maxmem": 4294967296,
                    },
                ]
            },
        )
    return httpx.Response(404, json={"data": None})


def test_discover_proxmox_prints_inventory(monkeypatch) -> None:
    monkeypatch.setattr(cli_discover, "_load_proxmox_adapter", lambda: _proxmox(_proxmox_handler))
    result = runner.invoke(app, ["discover", "proxmox"])
    assert result.exit_code == 0
    assert "cluster lab" in result.stdout
    assert "web" in result.stdout


def test_discover_proxmox_unreachable_exits_nonzero(monkeypatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="permission denied")

    monkeypatch.setattr(cli_discover, "_load_proxmox_adapter", lambda: _proxmox(down))
    result = runner.invoke(app, ["discover", "proxmox"])
    assert result.exit_code == 1
    assert "unreachable" in result.stdout


def test_discover_proxmox_netbox_sync_creates_vm(monkeypatch) -> None:
    posts: list[str] = []

    def nb_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/virtualization/clusters/":
            return httpx.Response(200, json={"results": [{"id": 1, "name": "lab"}], "next": None})
        if path == "/api/virtualization/virtual-machines/" and request.method == "GET":
            return httpx.Response(200, json={"results": [], "next": None})
        posts.append(request.method)
        return httpx.Response(201, json={"id": 9})

    monkeypatch.setattr(cli_discover, "_load_proxmox_adapter", lambda: _proxmox(_proxmox_handler))
    monkeypatch.setattr(cli_discover, "_load_netbox_adapter", lambda: _netbox(nb_handler))
    result = runner.invoke(app, ["discover", "proxmox", "--netbox-sync"])
    assert result.exit_code == 0
    assert "1 created" in result.stdout
    assert "POST" in posts


@pytest.mark.parametrize("flag", ["--netbox-sync", "--netbox-sync --dry-run"])
def test_discover_proxmox_sync_missing_cluster(monkeypatch, flag: str) -> None:
    def nb_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [], "next": None})  # no cluster

    monkeypatch.setattr(cli_discover, "_load_proxmox_adapter", lambda: _proxmox(_proxmox_handler))
    monkeypatch.setattr(cli_discover, "_load_netbox_adapter", lambda: _netbox(nb_handler))
    result = runner.invoke(app, ["discover", "proxmox", *flag.split()])
    assert result.exit_code == 1
    assert "not found" in result.stdout
