"""CLI test for `helper discover omv`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
from typer.testing import CliRunner

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

import homelab_helper.cli.discover as cli_discover
from homelab_helper.adapters.openmediavault import OpenMediaVaultAdapter, OpenMediaVaultConfig
from homelab_helper.cli.main import app

runner = CliRunner()


def _omv(handler: Callable[[httpx.Request], httpx.Response]) -> OpenMediaVaultAdapter:
    client = httpx.AsyncClient(base_url="https://nas.lan", transport=httpx.MockTransport(handler))
    return OpenMediaVaultAdapter(
        OpenMediaVaultConfig(url="https://nas.lan", username="admin", password="pw"), client=client
    )


def _ok(response: object) -> dict:
    return {"response": response, "error": None}


def _rpc_of(request: httpx.Request) -> tuple[str, str]:
    body = json.loads(request.content)
    return body["service"], body["method"]


def _handler(request: httpx.Request) -> httpx.Response:
    service, method = _rpc_of(request)
    if (service, method) == ("session", "login"):
        return httpx.Response(200, json=_ok({"authenticated": True}))
    if (service, method) == ("FileSystemMgmt", "enumerateMountedFilesystems"):
        return httpx.Response(
            200,
            json=_ok(
                [
                    {
                        "canonicaldevicefile": "/dev/sda1",
                        "type": "ext4",
                        "mountpoint": "/srv/data",
                        "size": "1000204886016",
                        "percentage": 42,
                    }
                ]
            ),
        )
    if (service, method) == ("Smart", "enumerateDevices"):
        return httpx.Response(200, json=_ok([{"canonicaldevicefile": "/dev/sda", "model": "WDC"}]))
    if (service, method) == ("ShareMgmt", "enumerateSharedFolders"):
        return httpx.Response(
            200, json=_ok([{"uuid": "u1", "name": "media", "reldirpath": "media/"}])
        )
    if (service, method) == ("Services", "getStatus"):
        return httpx.Response(
            200, json=_ok({"total": 1, "data": [{"name": "smb", "enabled": True, "running": True}]})
        )
    return httpx.Response(200, json=_ok([]))


def test_discover_omv_prints_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_discover, "_load_omv_adapter", lambda: _omv(_handler))
    result = runner.invoke(app, ["discover", "omv"])
    assert result.exit_code == 0
    assert "1 filesystem(s)" in result.stdout
    assert "media" in result.stdout
    assert "1/1 running" in result.stdout


def test_discover_omv_unreachable_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    monkeypatch.setattr(cli_discover, "_load_omv_adapter", lambda: _omv(down))
    result = runner.invoke(app, ["discover", "omv"])
    assert result.exit_code == 1
    assert "unreachable" in result.stdout.lower()


def test_discover_omv_help_loads() -> None:
    result = runner.invoke(app, ["discover", "omv", "--help"])
    assert result.exit_code == 0
