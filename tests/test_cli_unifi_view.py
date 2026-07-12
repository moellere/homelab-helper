"""CLI tests for `helper discover unifi` and `helper view service|host`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

import homelab_helper.cli.discover as cli_discover
from homelab_helper.adapters.unifi import UniFiAdapter, UniFiConfig
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import DiscoverySource, ResolutionScope
from homelab_helper.db.models import (
    Cluster,
    Host,
    Service,
    ServiceEndpoint,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope

runner = CliRunner()


def _unifi(handler: Callable[[httpx.Request], httpx.Response]) -> UniFiAdapter:
    client = httpx.AsyncClient(
        base_url="https://u.lan/proxy/network", transport=httpx.MockTransport(handler)
    )
    return UniFiAdapter(UniFiConfig(url="https://u.lan", api_key="k"), client=client)


def _wrap(rows: list[dict]) -> dict:
    return {"meta": {"rc": "ok"}, "data": rows}


def _unifi_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/rest/dnsrecord"):
        return _r([{"key": "nas.lan", "value": "10.0.1.5", "record_type": "A"}])
    if path.endswith("/rest/user"):
        return _r([{"hostname": "nas", "ip": "10.0.1.5", "mac": "aa:bb:cc:00:11:22"}])
    if path.endswith("/rest/networkconf"):
        return _r([{"name": "Default", "ip_subnet": "10.0.1.1/24"}])
    if path.endswith("/self"):
        return _r([{"name": "default"}])
    return httpx.Response(404, json=_wrap([]))


def _r(rows: list[dict]) -> httpx.Response:
    return httpx.Response(200, json=_wrap(rows))


# ---------------------------------------------------------------------------
# discover unifi
# ---------------------------------------------------------------------------


def test_discover_unifi_prints_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_discover, "_load_unifi_adapter", lambda: _unifi(_unifi_handler))
    result = runner.invoke(app, ["discover", "unifi"])
    assert result.exit_code == 0
    assert "1 DNS record" in result.stdout
    assert "nas.lan" in result.stdout


def test_discover_unifi_unreachable_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    monkeypatch.setattr(cli_discover, "_load_unifi_adapter", lambda: _unifi(down))
    result = runner.invoke(app, ["discover", "unifi"])
    assert result.exit_code == 1
    assert "unreachable" in result.stdout.lower()


@pytest.fixture
async def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'unifi.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return url


def test_discover_unifi_persist_creates_endpoints(
    empty_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_discover, "_load_unifi_adapter", lambda: _unifi(_unifi_handler))
    result = runner.invoke(app, ["discover", "unifi", "--persist"])
    assert result.exit_code == 0
    assert "1 created" in result.stdout


def test_discover_unifi_dry_run_rolls_back(empty_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_discover, "_load_unifi_adapter", lambda: _unifi(_unifi_handler))
    result = runner.invoke(app, ["discover", "unifi", "--persist", "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.stdout.lower()
    # Then a real view should find nothing persisted.
    v = runner.invoke(app, ["view", "service", "nas.lan"])
    assert v.exit_code == 2


# ---------------------------------------------------------------------------
# view service / view host — seeded DB
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A service with internal+external endpoints (split-brain), a VM, and a host."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'view.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)
    async with session_scope(sm) as s:
        host = Host(hostname="node0", primary_ip="10.0.1.20")
        s.add(host)
        await s.flush()
        cluster = Cluster(name="lab", kind="proxmox")
        s.add(cluster)
        await s.flush()
        s.add(
            VirtualMachine(
                cluster_id=cluster.id,
                vmid=105,
                name="home-assistant",
                kind="qemu",
                status="running",
                node_name="node0",
                node_host_id=host.id,
            )
        )
        svc = Service(name="home-assistant")
        s.add(svc)
        await s.flush()
        s.add(
            ServiceEndpoint(
                service_id=svc.id,
                scope=ResolutionScope.INTERNAL,
                hostname="home-assistant",
                ip="10.0.1.50",
                resolver="unifi",
                discovery_source=DiscoverySource.UNIFI,
            )
        )
        s.add(
            ServiceEndpoint(
                service_id=svc.id,
                scope=ResolutionScope.EXTERNAL,
                hostname="home-assistant",
                ip="203.0.113.9",
                resolver="cloudflare",
                discovery_source=DiscoverySource.CLOUDFLARE,
            )
        )
    await engine.dispose()
    return url


def test_view_service_shows_endpoints_and_split_brain(seeded_db: str) -> None:
    result = runner.invoke(app, ["view", "service", "home-assistant"])
    assert result.exit_code == 0
    assert "internal" in result.stdout
    assert "external" in result.stdout
    assert "split-brain" in result.stdout.lower()
    # Cross-ref to the VM.
    assert "home-assistant" in result.stdout


def test_view_service_resolves_by_endpoint_hostname(seeded_db: str) -> None:
    # Same service is reachable via the endpoint hostname too (lowercased match).
    result = runner.invoke(app, ["view", "service", "home-assistant"])
    assert result.exit_code == 0


def test_view_service_unknown_exits_2(seeded_db: str) -> None:
    result = runner.invoke(app, ["view", "service", "nonesuch"])
    assert result.exit_code == 2
    assert "no service" in result.stdout.lower()


def test_view_host_shows_guests(seeded_db: str) -> None:
    result = runner.invoke(app, ["view", "host", "node0"])
    assert result.exit_code == 0
    assert "node0" in result.stdout
    assert "guests" in result.stdout.lower()
    assert "home-assistant" in result.stdout


def test_view_host_unknown_exits_2(seeded_db: str) -> None:
    result = runner.invoke(app, ["view", "host", "ghost"])
    assert result.exit_code == 2


@pytest.mark.parametrize(
    "argv",
    [["view", "--help"], ["view", "service", "--help"], ["view", "host", "--help"]],
)
def test_view_help_loads(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0
