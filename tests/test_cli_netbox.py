"""CLI tests for ``helper netbox ...``.

These tests stub ``cli.netbox._load_adapter`` so the verb runs against a mock
``NetBoxAdapter`` that uses ``httpx.MockTransport`` for transport. No live
NetBox is involved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from homelab_helper.adapters.netbox import NetBoxAdapter, NetBoxConfig
from homelab_helper.cli import netbox as cli_netbox
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    Architecture,
    DiscoverySource,
    PartKind,
    PowerPolicy,
    PowerState,
)
from homelab_helper.db.models import Host, PhysicalPart, Placement
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope

runner = CliRunner()


def _adapter_with(handler: Callable[[httpx.Request], httpx.Response]) -> NetBoxAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://netbox.example/",
        headers={"Authorization": "Token test", "Accept": "application/json"},
    )
    return NetBoxAdapter(
        NetBoxConfig(url="http://netbox.example/", token="test"),
        client=client,
    )


@pytest.fixture
def patch_adapter(monkeypatch: pytest.MonkeyPatch):
    """Replace cli.netbox._load_adapter with a factory the test controls."""
    holder: dict[str, Any] = {}

    def _set(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        def _factory() -> NetBoxAdapter:
            return _adapter_with(handler)

        monkeypatch.setattr(cli_netbox, "_load_adapter", _factory)
        holder["set"] = True

    return _set


@pytest.fixture
async def db_url_with_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Seed a file-backed SQLite with one host (bmax0)."""
    db_path = tmp_path / "netbox.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)
    async with session_scope(sm) as s:
        s.add(
            Host(
                hostname="bmax0",
                primary_ip="10.250.6.20",
                arch=Architecture.AMD64,
                power_policy=PowerPolicy.ALWAYS_ON,
                expected_power_state=PowerState.ON,
                discovery_source=DiscoverySource.KERNEL_PROBE,
                capabilities={"kernel": "6.8.0"},
            )
        )
    await engine.dispose()
    return url


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_netbox_health_prints_version(patch_adapter) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"netbox-version": "4.1.2", "plugins": {"foo": "1.0"}},
        )

    patch_adapter(handler)
    result = runner.invoke(app, ["netbox", "health"])
    assert result.exit_code == 0
    assert "4.1.2" in result.stdout
    assert "reachable" in result.stdout.lower()


def test_netbox_health_reports_unreachable_on_error(patch_adapter) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    patch_adapter(handler)
    result = runner.invoke(app, ["netbox", "health"])
    assert result.exit_code == 1
    assert "unreachable" in result.stdout.lower()


def test_netbox_health_errors_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No URL/token in env → exit 2 with a clear message."""
    monkeypatch.delenv("HOMELAB_HELPER_NETBOX_URL", raising=False)
    monkeypatch.delenv("HOMELAB_HELPER_NETBOX_TOKEN", raising=False)
    result = runner.invoke(app, ["netbox", "health"])
    assert result.exit_code == 2
    assert "config error" in result.stdout.lower()


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def test_netbox_bootstrap_creates_missing_cfs(patch_adapter) -> None:
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"count": 0, "next": None, "previous": None, "results": []},
            )
        # POST — record the CF name and accept.
        import json as _json

        payload = _json.loads(request.content)
        posted.append(payload["name"])
        return httpx.Response(201, json={"id": len(posted), "name": payload["name"]})

    patch_adapter(handler)
    result = runner.invoke(app, ["netbox", "bootstrap"])
    assert result.exit_code == 0
    # The shipped list contains ten CFs.
    assert len(posted) >= 10
    assert "created" in result.stdout.lower()


def test_netbox_bootstrap_idempotent_second_run(patch_adapter) -> None:
    """Pre-populate the list endpoint with all shipped CFs."""
    from homelab_helper.adapters.netbox import _DEVICE_CFS

    existing = [{"name": spec.name} for spec in _DEVICE_CFS]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "count": len(existing),
                    "next": None,
                    "previous": None,
                    "results": existing,
                },
            )
        return httpx.Response(409, json={"detail": "already exists"})

    patch_adapter(handler)
    result = runner.invoke(app, ["netbox", "bootstrap"])
    assert result.exit_code == 0
    assert "0 created" in result.stdout or "0[/green] created" in result.stdout


def test_netbox_bootstrap_dry_run_makes_no_posts(patch_adapter) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json={"count": 0, "next": None, "previous": None, "results": []},
        )

    patch_adapter(handler)
    result = runner.invoke(app, ["netbox", "bootstrap", "--dry-run"])
    assert result.exit_code == 0
    assert "dry run" in result.stdout.lower()
    assert "POST" not in methods


# ---------------------------------------------------------------------------
# sync-host
# ---------------------------------------------------------------------------


def test_netbox_sync_host_patches_existing_device(db_url_with_host: str, patch_adapter) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/dcim/devices/":
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{"id": 99, "name": "bmax0"}],
                },
            )
        if request.method == "PATCH":
            import json as _json

            seen["path"] = request.url.path
            seen["body"] = _json.loads(request.content)
            return httpx.Response(200, json={"id": 99, "name": "bmax0"})
        return httpx.Response(404)

    patch_adapter(handler)
    # --skip-inventory keeps this test focused on the CF push.
    result = runner.invoke(app, ["netbox", "sync-host", "bmax0", "--skip-inventory"])
    assert result.exit_code == 0
    assert "synced" in result.stdout.lower()
    assert seen["path"] == "/api/dcim/devices/99/"
    assert seen["body"]["custom_fields"]["cf_arch"] == "amd64"


def test_netbox_sync_host_skips_when_device_missing(db_url_with_host: str, patch_adapter) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0, "next": None, "previous": None, "results": []})

    patch_adapter(handler)
    result = runner.invoke(app, ["netbox", "sync-host", "bmax0"])
    assert result.exit_code == 0  # tolerant, not an error
    assert "skipped" in result.stdout.lower()
    assert "Create the Device" in result.stdout


def test_netbox_sync_host_errors_on_unknown_local_host(
    db_url_with_host: str, patch_adapter
) -> None:
    patch_adapter(
        lambda request: httpx.Response(
            200, json={"count": 0, "next": None, "previous": None, "results": []}
        )
    )
    result = runner.invoke(app, ["netbox", "sync-host", "no-such-host"])
    assert result.exit_code == 2
    assert "no harness host" in result.stdout.lower()


def test_netbox_sync_host_dry_run_does_not_patch(db_url_with_host: str, patch_adapter) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{"id": 1, "name": "bmax0"}],
                },
            )
        return httpx.Response(200, json={"id": 1})

    patch_adapter(handler)
    result = runner.invoke(app, ["netbox", "sync-host", "bmax0", "--dry-run"])
    assert result.exit_code == 0
    assert "dry run" in result.stdout.lower()
    assert "PATCH" not in methods


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["netbox", "--help"],
        ["netbox", "health", "--help"],
        ["netbox", "bootstrap", "--help"],
        ["netbox", "sync-host", "--help"],
    ],
)
def test_netbox_subcommand_help_loads(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0


def test_top_level_help_lists_netbox() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "netbox" in result.stdout


# ---------------------------------------------------------------------------
# sync-host inventory pass — end-to-end through the CLI
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_url_with_host_and_dimm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Seed bmax0 with one open DIMM placement."""
    db_path = tmp_path / "netbox-inv.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)
    async with session_scope(sm) as s:
        host = Host(hostname="bmax0", primary_ip="10.250.6.20")
        s.add(host)
        await s.flush()
        part = PhysicalPart(
            kind=PartKind.DIMM,
            serial="DIMM-AAA",
            manufacturer="Crucial",
            model="CT16G",
            capacity_bytes=16 * 1024**3,
        )
        s.add(part)
        await s.flush()
        s.add(Placement(part_id=part.id, host_id=host.id, slot="DIMM_A1"))
    await engine.dispose()
    return url


def test_sync_host_runs_both_passes_by_default(
    db_url_with_host_and_dimm: str, patch_adapter
) -> None:
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append(request.method)
        path = request.url.path
        if request.method == "GET" and path == "/api/dcim/devices/":
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{"id": 7, "name": "bmax0"}],
                },
            )
        if request.method == "PATCH" and path.startswith("/api/dcim/devices/"):
            return httpx.Response(200, json={"id": 7})
        if request.method == "GET" and path == "/api/dcim/inventory-items/":
            return httpx.Response(
                200,
                json={"count": 0, "next": None, "previous": None, "results": []},
            )
        if request.method == "POST" and path == "/api/dcim/inventory-items/":
            return httpx.Response(201, json={"id": 100})
        return httpx.Response(404)

    patch_adapter(handler)
    result = runner.invoke(app, ["netbox", "sync-host", "bmax0"])
    assert result.exit_code == 0
    # Both passes ran: a PATCH on the device + a POST on inventory-items.
    assert "PATCH" in seen_methods
    assert "POST" in seen_methods
    assert "synced" in result.stdout.lower()
    assert "inventory" in result.stdout.lower()


def test_sync_host_skip_inventory(db_url_with_host_and_dimm: str, patch_adapter) -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.method == "GET" and request.url.path == "/api/dcim/devices/":
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{"id": 7, "name": "bmax0"}],
                },
            )
        return httpx.Response(200, json={"id": 7})

    patch_adapter(handler)
    result = runner.invoke(app, ["netbox", "sync-host", "bmax0", "--skip-inventory"])
    assert result.exit_code == 0
    # Should never hit the inventory-items endpoint.
    assert all("inventory-items" not in p for p in seen_paths)


def test_sync_host_skip_fields(db_url_with_host_and_dimm: str, patch_adapter) -> None:
    methods: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/dcim/devices/":
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{"id": 7, "name": "bmax0"}],
                },
            )
        if request.method == "GET" and request.url.path == "/api/dcim/inventory-items/":
            return httpx.Response(
                200,
                json={"count": 0, "next": None, "previous": None, "results": []},
            )
        if request.method == "POST":
            return httpx.Response(201, json={"id": 100})
        return httpx.Response(404)

    patch_adapter(handler)
    result = runner.invoke(app, ["netbox", "sync-host", "bmax0", "--skip-fields"])
    assert result.exit_code == 0
    # No Device PATCH was issued.
    assert not any(m == "PATCH" and p.startswith("/api/dcim/devices/") for m, p in methods)
    # Inventory pass still ran.
    assert any("inventory-items" in p for _, p in methods)


def test_sync_host_dry_run_makes_no_writes(db_url_with_host_and_dimm: str, patch_adapter) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET" and request.url.path == "/api/dcim/devices/":
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{"id": 7, "name": "bmax0"}],
                },
            )
        if request.method == "GET" and request.url.path == "/api/dcim/inventory-items/":
            return httpx.Response(
                200,
                json={"count": 0, "next": None, "previous": None, "results": []},
            )
        return httpx.Response(404)

    patch_adapter(handler)
    result = runner.invoke(app, ["netbox", "sync-host", "bmax0", "--dry-run"])
    assert result.exit_code == 0
    assert "dry run" in result.stdout.lower()
    # No writes — neither device PATCH nor inventory POST.
    assert "PATCH" not in methods
    assert "POST" not in methods
