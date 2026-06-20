"""CLI test for `helper netbox sync-cluster` — verifies wiring + DB write-back commit."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

import homelab_helper.cli.netbox as cli_netbox
from homelab_helper.adapters.netbox import NetBoxAdapter, NetBoxConfig
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.models import Cluster, VirtualMachine
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture
async def db_url_with_cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'nb.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)
    async with session_scope(sm) as s:
        c = Cluster(name="lab", kind="proxmox", node_count=1)
        s.add(c)
        await s.flush()
        s.add(VirtualMachine(cluster_id=c.id, vmid=100, name="web", kind="qemu", status="running"))
    await engine.dispose()
    return url


def _stateful_adapter() -> NetBoxAdapter:
    store: dict[int, dict] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/virtualization/clusters/":
            return httpx.Response(200, json={"results": [{"id": 5, "name": "lab"}], "next": None})
        if path == "/api/virtualization/virtual-machines/" and request.method == "GET":
            return httpx.Response(200, json={"results": list(store.values()), "next": None})
        if request.method == "POST":
            body = json.loads(request.content)
            store[100] = {"id": 100, "name": body["name"]}
            return httpx.Response(201, json=store[100])
        return httpx.Response(404, json={})

    client = httpx.AsyncClient(
        base_url="http://netbox.example/", transport=httpx.MockTransport(handle)
    )
    return NetBoxAdapter(NetBoxConfig(url="http://netbox.example/", token="t"), client=client)


def test_sync_cluster_writes_back_netbox_id(
    db_url_with_cluster: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_netbox, "_load_adapter", _stateful_adapter)
    result = runner.invoke(app, ["netbox", "sync-cluster", "lab"])
    assert result.exit_code == 0, result.stdout
    assert "1 created" in result.stdout
    assert "ids written back" in result.stdout

    async def _check() -> int | None:
        engine = make_engine(db_url_with_cluster)
        try:
            sm = make_sessionmaker(engine)
            async with sm() as s:
                vm = (await s.execute(select(VirtualMachine))).scalar_one()
                return vm.netbox_vm_id
        finally:
            await engine.dispose()

    assert asyncio.run(_check()) == 100  # write-back persisted (commit happened)


def test_sync_cluster_unknown_name_errors(
    db_url_with_cluster: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_netbox, "_load_adapter", _stateful_adapter)
    result = runner.invoke(app, ["netbox", "sync-cluster", "nope"])
    assert result.exit_code == 2
    assert "no harness cluster" in result.stdout
