"""MCP server tests — tools called directly (no client), seeded file-backed DB."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

import homelab_helper.mcp_server as mcp_srv
from homelab_helper.adapters.unifi import UniFiAdapter, UniFiConfig
from homelab_helper.cli.main import app
from homelab_helper.config import PROBE_ALLOW_VAR
from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    DiscoverySource,
    FindingKind,
    FindingSeverity,
    ResolutionScope,
)
from homelab_helper.db.models import (
    Cluster,
    Host,
    ReconciliationFinding,
    Service,
    ServiceEndpoint,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.host_probe import HostProbeRequest, HostProbeResult
from homelab_helper.mcp_server import (
    audit_summary,
    get_finding,
    get_host,
    get_service,
    list_findings,
    list_hosts,
    list_services,
    probe_host,
    probe_target_refusal,
    run_discovery,
    server,
)

runner = CliRunner()

EXPECTED_TOOLS = {
    "list_hosts",
    "get_host",
    "list_findings",
    "get_finding",
    "list_services",
    "get_service",
    "audit_summary",
    "run_discovery",
    "config_status",
    "ack_finding",
    "resolve_finding",
    "suppress_finding",
    "probe_host",
}


async def test_server_registers_expected_tools() -> None:
    tools = {t.name for t in await server.list_tools()}
    assert tools == EXPECTED_TOOLS


async def test_every_tool_has_a_description() -> None:
    for t in await server.list_tools():
        assert t.description, f"tool {t.name} has no description"


# ---------------------------------------------------------------------------
# seeded DB
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'mcp.db'}"
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
                name="ha",
                kind="qemu",
                status="running",
                node_name="node0",
                node_host_id=host.id,
            )
        )
        svc = Service(name="ha")
        s.add(svc)
        await s.flush()
        s.add(
            ServiceEndpoint(
                service_id=svc.id,
                scope=ResolutionScope.INTERNAL,
                hostname="ha.lan",
                ip="10.0.1.50",
                resolver="unifi",
                discovery_source=DiscoverySource.UNIFI,
            )
        )
        s.add(
            ServiceEndpoint(
                service_id=svc.id,
                scope=ResolutionScope.EXTERNAL,
                hostname="ha.example.com",
                ip="203.0.113.9",
                resolver="cloudflare",
                discovery_source=DiscoverySource.CLOUDFLARE,
            )
        )
        s.add(
            ReconciliationFinding(
                kind=FindingKind.STRAY_CONFIG,
                severity=FindingSeverity.LOW,
                fingerprint="deadbeefcafe0000",
                title="Stray config: IoT",
                description="no clients",
                affected=[{"target_type": "host", "target_id": str(host.id)}],
            )
        )
    await engine.dispose()
    return url


async def test_list_hosts(seeded_db: str) -> None:
    hosts = await list_hosts()
    assert [h["hostname"] for h in hosts] == ["node0"]
    assert hosts[0]["primary_ip"] == "10.0.1.20"


async def test_get_host_synthesizes(seeded_db: str) -> None:
    out = await get_host("node0")
    assert out["hostname"] == "node0"
    assert [g["name"] for g in out["guests"]] == ["ha"]
    assert out["open_findings"][0]["fingerprint"] == "deadbeefcafe0000"


async def test_get_host_unknown_returns_error(seeded_db: str) -> None:
    out = await get_host("ghost")
    assert "error" in out


async def test_list_findings_filters(seeded_db: str) -> None:
    assert len(await list_findings()) == 1
    assert len(await list_findings(kind="stray-config")) == 1
    assert await list_findings(kind="drift-candidate") == []
    assert await list_findings(status="resolved") == []


async def test_get_finding_by_fingerprint(seeded_db: str) -> None:
    out = await get_finding("deadbeefcafe0000")
    assert out["title"] == "Stray config: IoT"
    assert "error" in await get_finding("nope")


async def test_list_services_flags_split_brain(seeded_db: str) -> None:
    services = await list_services()
    assert services == [
        {
            "name": "ha",
            "internal_endpoints": 1,
            "external_endpoints": 1,
            "split_brain": True,
        }
    ]


async def test_get_service_by_endpoint_hostname(seeded_db: str) -> None:
    out = await get_service("ha.example.com")
    assert out["name"] == "ha"
    assert out["split_brain"] == {
        "internal_ips": ["10.0.1.50"],
        "external_ips": ["203.0.113.9"],
    }
    assert out["hosted"]["vm"] == "ha"


async def test_audit_summary_counts(seeded_db: str) -> None:
    out = await audit_summary()
    assert out["hosts"] == 1
    assert out["virtual_machines"] == 1
    assert out["findings_by_status"] == {"open": 1}
    assert out["open_findings_by_severity"] == {"low": 1}


# ---------------------------------------------------------------------------
# run_discovery
# ---------------------------------------------------------------------------


def _unifi(handler: Callable[[httpx.Request], httpx.Response]) -> UniFiAdapter:
    client = httpx.AsyncClient(
        base_url="https://u.lan/proxy/network", transport=httpx.MockTransport(handler)
    )
    return UniFiAdapter(UniFiConfig(url="https://u.lan", api_key="k"), client=client)


def _wrap(rows: list[dict]) -> dict:
    return {"meta": {"rc": "ok"}, "data": rows}


def _unifi_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/static-dns"):
        return httpx.Response(
            200, json=_wrap([{"key": "nas.lan", "value": "10.0.1.5", "record_type": "A"}])
        )
    if path.endswith("/rest/user"):
        return httpx.Response(200, json=_wrap([{"hostname": "nas", "ip": "10.0.1.5"}]))
    if path.endswith("/rest/networkconf"):
        return httpx.Response(
            200, json=_wrap([{"name": "Default", "ip_subnet": "10.0.1.1/24", "enabled": True}])
        )
    return httpx.Response(200, json=_wrap([]))


async def test_run_discovery_unifi_persists(
    seeded_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_srv, "_load_unifi_adapter", lambda: _unifi(_unifi_handler))
    out = await run_discovery("unifi")
    assert out["source"] == "unifi"
    assert out["endpoints"]["created"] == 1
    # The endpoint is now queryable through the same MCP surface.
    services = await list_services()
    assert any(s["name"] == "nas" for s in services)


async def test_run_discovery_unknown_source(seeded_db: str) -> None:
    out = await run_discovery("fridge")
    assert "error" in out
    assert "unifi" in out["error"]


async def test_run_discovery_config_error_is_data(
    seeded_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> UniFiAdapter:
        raise RuntimeError("no credentials configured")

    monkeypatch.setattr(mcp_srv, "_load_unifi_adapter", _boom)
    out = await run_discovery("unifi")
    assert out["error"] == "no credentials configured"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_mcp_tools_lists_roster() -> None:
    result = runner.invoke(app, ["mcp", "tools"])
    assert result.exit_code == 0
    assert "run_discovery" in result.stdout
    assert "audit_summary" in result.stdout


@pytest.mark.parametrize("argv", [["mcp", "--help"], ["mcp", "serve", "--help"]])
def test_cli_mcp_help_loads(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# probe_host scoping
# ---------------------------------------------------------------------------


@pytest.fixture
def probe_calls(monkeypatch: pytest.MonkeyPatch) -> list[HostProbeRequest]:
    """Replace the SSH probe run with a recorder; the policy is what's under test."""
    calls: list[HostProbeRequest] = []

    async def _fake(session: object, request: HostProbeRequest) -> HostProbeResult:
        calls.append(request)
        return HostProbeResult(
            hostname=request.name, host_id="00000000-0000-0000-0000-000000000000"
        )

    monkeypatch.setattr(mcp_srv, "_probe_host", _fake)
    monkeypatch.setenv("HOMELAB_HELPER_SSH_KEY", "/nonexistent/key")
    monkeypatch.delenv(PROBE_ALLOW_VAR, raising=False)
    return calls


async def test_probe_host_known_host_runs(seeded_db: str, probe_calls: list) -> None:
    out = await probe_host("node0", "root")
    assert out["hostname"] == "node0"
    assert "error" not in out
    assert [c.name for c in probe_calls] == ["node0"]


async def test_probe_host_known_host_at_recorded_ip_runs(seeded_db: str, probe_calls: list) -> None:
    out = await probe_host("node0", "root", primary_ip="10.0.1.20")
    assert "error" not in out
    assert len(probe_calls) == 1


async def test_probe_host_known_host_at_other_ip_refused(seeded_db: str, probe_calls: list) -> None:
    out = await probe_host("node0", "root", primary_ip="203.0.113.7")
    assert "recorded at 10.0.1.20" in out["error"]
    assert probe_calls == []


async def test_probe_host_unknown_host_refused_by_default(
    seeded_db: str, probe_calls: list
) -> None:
    out = await probe_host("rogue", "root", primary_ip="203.0.113.7")
    assert PROBE_ALLOW_VAR in out["error"]
    assert "helper discover host" in out["error"]
    assert probe_calls == []


async def test_probe_host_allow_list_admits_matching_unknown_host(
    seeded_db: str, probe_calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROBE_ALLOW_VAR, "*.lan, 10.0.1.*")
    out = await probe_host("new-box.lan", "admin", primary_ip="10.0.1.77")
    assert "error" not in out
    assert probe_calls[0].primary_ip == "10.0.1.77"


async def test_probe_host_allow_list_still_refuses_unlisted_ip(
    seeded_db: str, probe_calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROBE_ALLOW_VAR, "*.lan")
    out = await probe_host("new-box.lan", "admin", primary_ip="203.0.113.7")
    assert "203.0.113.7" in out["error"]
    assert probe_calls == []


async def test_probe_host_allow_list_refuses_unlisted_name(
    seeded_db: str, probe_calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROBE_ALLOW_VAR, "*.lan")
    out = await probe_host("box.example.com", "admin")
    assert "matches no pattern" in out["error"]
    assert probe_calls == []


def test_probe_target_refusal_known_host_without_ip_needs_allowed_address() -> None:
    known = Host(hostname="bare")
    assert probe_target_refusal("bare", None, known, ()) is None
    assert probe_target_refusal("bare", "10.0.1.5", known, ()) is not None
    assert probe_target_refusal("bare", "10.0.1.5", known, ("10.0.1.*",)) is None


def test_probe_target_refusal_matches_case_insensitively() -> None:
    assert probe_target_refusal("NODE9.LAN", None, None, ("*.lan",)) is None
