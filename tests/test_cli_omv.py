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


_RESPONSES: dict[tuple[str, str], object] = {
    ("session", "login"): {"authenticated": True},
    ("FileSystemMgmt", "enumerateMountedFilesystems"): [
        {
            "canonicaldevicefile": "/dev/sda1",
            "type": "ext4",
            "mountpoint": "/srv/data",
            "size": "1000204886016",
            "percentage": 42,
        }
    ],
    ("Smart", "enumerateDevices"): [{"canonicaldevicefile": "/dev/sda", "model": "WDC"}],
    ("ShareMgmt", "enumerateSharedFolders"): [
        {"uuid": "u1", "name": "media", "reldirpath": "media/"}
    ],
    ("Services", "getStatus"): {
        "total": 1,
        "data": [{"name": "smb", "enabled": True, "running": True}],
    },
    ("NFS", "getShareList"): [{"uuid": "n1", "sharedfoldername": "media"}],
    ("SMB", "getShareList"): [{"uuid": "s1", "name": "media"}],
}


def _handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_ok(_RESPONSES.get(_rpc_of(request), [])))


def test_discover_omv_prints_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_discover, "_load_omv_adapter", lambda: _omv(_handler))
    result = runner.invoke(app, ["discover", "omv"])
    assert result.exit_code == 0
    assert "1 filesystem(s)" in result.stdout
    assert "media" in result.stdout
    # Summary line wraps under the narrow test terminal; assert on the exports table instead.
    assert "exports" in result.stdout
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


def test_discover_omv_flags_stray_exports_and_persists(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A share whose folder is gone shows in the table, and --persist records it."""
    import asyncio

    from sqlalchemy import select

    from homelab_helper.db.base import Base
    from homelab_helper.db.models import ReconciliationFinding
    from homelab_helper.db.session import make_engine, make_sessionmaker

    url = f"sqlite+aiosqlite:///{tmp_path / 'omv.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    async def _init() -> None:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_init())

    responses = dict(_RESPONSES)
    responses[("SMB", "getShareList")] = [
        {"uuid": "s1", "name": "media", "sharedfoldername": "media"},
        {"uuid": "s9", "name": "Ghost", "sharedfoldername": "gone"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok(responses.get(_rpc_of(request), [])))

    monkeypatch.setattr(cli_discover, "_load_omv_adapter", lambda: _omv(handler))
    result = runner.invoke(app, ["discover", "omv"])
    assert result.exit_code == 0, result.stdout
    assert "stray exports" in result.stdout
    assert "Ghost" in result.stdout

    persisted = runner.invoke(app, ["discover", "omv", "--persist"])
    assert persisted.exit_code == 0, persisted.stdout
    assert "1 opened" in persisted.stdout

    async def _count() -> list[str]:
        engine = make_engine(url)
        try:
            sm = make_sessionmaker(engine)
            async with sm() as s:
                rows = (await s.execute(select(ReconciliationFinding))).scalars().all()
                return [f.affected[0]["target_id"] for f in rows]
        finally:
            await engine.dispose()

    assert asyncio.run(_count()) == ["smb:Ghost"]
