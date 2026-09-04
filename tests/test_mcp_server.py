"""MCP server tests — tools called directly (no client), seeded file-backed DB."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml
from typer.testing import CliRunner

if TYPE_CHECKING:
    from collections.abc import Callable

    from homelab_helper.engine.talos_probe import TalosProbeRequest

import homelab_helper.mcp_server as mcp_srv
from homelab_helper.adapters.homeassistant import HomeAssistantAdapter, HomeAssistantConfig
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
    analyze_bottlenecks,
    analyze_surplus,
    audit_summary,
    get_finding,
    get_host,
    get_proposal,
    get_service,
    list_findings,
    list_hosts,
    list_proposals,
    list_services,
    list_workloads,
    network_path,
    pending_actions,
    plan_rebalance,
    probe_host,
    probe_talos,
    probe_target_refusal,
    propose_action,
    recommend_placement,
    retire_host,
    run_discovery,
    server,
)
from tests.test_homeassistant_adapter import CONFIG_PAYLOAD, SERVICES_PAYLOAD, STATES_PAYLOAD

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
    "retire_host",
    "probe_host",
    "probe_talos",
    "list_workloads",
    "recommend_placement",
    "plan_rebalance",
    "analyze_bottlenecks",
    "analyze_surplus",
    "network_path",
    # Phase 6 trust surface — read-only by design.
    "trust_status",
    "list_receipts",
    "pending_actions",
    # Agent-side proposals: draft only; the operator runs or rejects them.
    "propose_action",
    "list_proposals",
    "get_proposal",
}

# Anything that grants, elevates, overrides, rolls back, or executes belongs to
# the operator at the CLI. The gradient's premise is that an LLM is never in the
# authorization path, so these must never appear as MCP tools.
FORBIDDEN_TOOL_SUBSTRINGS = (
    "grant",
    "override",
    "window",
    "execute",
    "exec_",
    "rollback",
    "kill",
    "promote",
    "demote",
    "boundary",
)


async def test_server_registers_expected_tools() -> None:
    tools = {t.name for t in await server.list_tools()}
    assert tools == EXPECTED_TOOLS


async def test_no_tool_can_change_authority_or_execute() -> None:
    """The MCP surface may read the trust gradient; it may never move it."""
    names = {t.name for t in await server.list_tools()}
    offenders = [
        name
        for name in names
        for bad in FORBIDDEN_TOOL_SUBSTRINGS
        if bad in name and name not in {"list_receipts"}
    ]
    assert not offenders, f"authority-changing MCP tools: {offenders}"


def test_mcp_module_never_calls_the_write_paths() -> None:
    """Belt and braces: the module must not even import the mutating helpers."""
    import homelab_helper.mcp_server as mod

    forbidden = (
        "execute_proposal",
        "rollback_receipt",
        "grant_cell",
        "open_window",
        "revoke_window",
        "kill_switch",
        "set_boundary",
        "record_clean_outcome",
        "record_bad_outcome",
    )
    present = [name for name in forbidden if hasattr(mod, name)]
    assert not present, f"mcp_server imported write-path helpers: {present}"

    source = Path(mod.__file__).read_text()
    called = [name for name in forbidden if f"{name}(" in source]
    assert not called, f"mcp_server calls write-path helpers: {called}"


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


# ---------------------------------------------------------------------------
# probe_talos scoping
# ---------------------------------------------------------------------------


@pytest.fixture
def talos_calls(monkeypatch: pytest.MonkeyPatch) -> list[TalosProbeRequest]:
    calls: list[TalosProbeRequest] = []

    async def _fake(session: object, request: TalosProbeRequest) -> HostProbeResult:
        calls.append(request)
        return HostProbeResult(
            hostname=request.name, host_id="00000000-0000-0000-0000-000000000000"
        )

    monkeypatch.setattr(mcp_srv, "_probe_talos", _fake)
    monkeypatch.delenv(PROBE_ALLOW_VAR, raising=False)
    return calls


async def test_probe_talos_known_host_runs(seeded_db: str, talos_calls: list) -> None:
    out = await probe_talos("node0")
    assert "error" not in out
    assert [c.name for c in talos_calls] == ["node0"]


async def test_probe_talos_known_host_other_node_refused(seeded_db: str, talos_calls: list) -> None:
    out = await probe_talos("node0", node="203.0.113.7")
    assert "recorded at 10.0.1.20" in out["error"]
    assert talos_calls == []


async def test_probe_talos_unknown_refused_by_default(seeded_db: str, talos_calls: list) -> None:
    out = await probe_talos("rogue")
    assert PROBE_ALLOW_VAR in out["error"]
    assert talos_calls == []


# ---------------------------------------------------------------------------
# planners
# ---------------------------------------------------------------------------


async def test_list_workloads_and_category_filter() -> None:
    everything = await list_workloads()
    assert isinstance(everything, list)
    names = {p["name"] for p in everything}
    assert "immich" in names
    assert all(isinstance(p["arch"], list) for p in everything)
    nvr = await list_workloads(category="nvr")
    assert isinstance(nvr, list)
    assert {p["name"] for p in nvr} == {p["name"] for p in everything if p["category"] == "nvr"}


async def test_recommend_placement_ranks_and_rejects(seeded_db: str) -> None:
    out = await recommend_placement("immich")
    assert out["workload"] == "immich"
    assert out["profile"]["name"] == "immich"
    listed = {c["hostname"] for c in out["candidates"]} | {r["hostname"] for r in out["rejected"]}
    assert "node0" in listed
    for c in out["candidates"]:
        assert isinstance(c["rank"], int)


async def test_recommend_placement_unknown_suggests(seeded_db: str) -> None:
    out = await recommend_placement("imich")
    assert "error" in out
    assert "immich" in out["did_you_mean"]


async def test_plan_rebalance_reports_fleet(seeded_db: str) -> None:
    out = await plan_rebalance()
    assert set(out) >= {"balanced", "hosts", "plans", "unknown_hosts", "spread"}
    assert "node0" in {h["hostname"] for h in out["hosts"]} | set(out["unknown_hosts"])


async def test_analyze_bottlenecks_and_surplus_return_hit_lists(seeded_db: str) -> None:
    hits = await analyze_bottlenecks()
    assert isinstance(hits["hits"], list)
    assert "findings" not in hits
    persisted = await analyze_bottlenecks(persist=True)
    assert set(persisted["findings"]) == {"opened", "reopened", "updated", "resolved"}
    surplus = await analyze_surplus()
    assert isinstance(surplus["hits"], list)


async def test_network_path_without_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_NETWORK_TOPOLOGY", raising=False)
    out = await network_path("a", "b")
    assert out["topology_declared"] is False
    assert "HOMELAB_HELPER_NETWORK_TOPOLOGY" in out["note"]


_TOPOLOGY_EXAMPLE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "network-topology.example.yaml"
)


def _example_topology_hosts() -> list[str]:
    sites = yaml.safe_load(_TOPOLOGY_EXAMPLE.read_text())["sites"]
    return [h for spec in sites.values() for h in (spec or {}).get("hosts") or []]


async def test_network_path_with_example_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_NETWORK_TOPOLOGY", str(_TOPOLOGY_EXAMPLE))
    hosts = _example_topology_hosts()
    out = await network_path(hosts[0], hosts[-1], workload="immich")
    assert out["topology_declared"] is True
    assert "path" in out
    assert out["path"]["summary"]
    assert out["verdict"]["level"] in {"ok", "warn", "refuse"}


# ---------------------------------------------------------------------------
# run_discovery("hass")
# ---------------------------------------------------------------------------


def _hass(handler: Callable[[httpx.Request], httpx.Response]) -> HomeAssistantAdapter:
    client = httpx.AsyncClient(
        base_url="http://ha.example.lan:8123", transport=httpx.MockTransport(handler)
    )
    return HomeAssistantAdapter(
        HomeAssistantConfig(url="http://ha.example.lan:8123", token="tok"), client=client
    )


def _hass_handler(request: httpx.Request) -> httpx.Response:
    routes = {
        "/api/config": CONFIG_PAYLOAD,
        "/api/states": STATES_PAYLOAD,
        "/api/services": SERVICES_PAYLOAD,
    }
    body = routes.get(request.url.path)
    return httpx.Response(200, json=body) if body is not None else httpx.Response(404)


async def test_run_discovery_hass_persists(seeded_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_srv, "_load_hass_adapter", lambda: _hass(_hass_handler))
    out = await run_discovery("hass")
    assert out["source"] == "hass"
    assert out["created"] is True
    assert out["integrations"] == ["mqtt", "proxmoxve", "unifi"]
    svc = await get_service("home-assistant")
    assert "error" not in svc


# ---------------------------------------------------------------------------
# propose_action / list_proposals / get_proposal — draft only
# ---------------------------------------------------------------------------


async def test_propose_action_writes_pending_proposal_with_preview(seeded_db: str) -> None:
    out = await propose_action(
        action_kind="restart",
        node="node0",
        vmid=105,
        vm_kind="qemu",
        title="Restart ha (qemu/105)",
        description="drafted by a test",
    )
    assert "error" not in out, out
    assert out["outcome"] == "pending"
    assert out["proposed_by"] == "agent:mcp"
    assert out["cell"] == "hypervisor/restart/single-host"
    assert out["decision_if_run_now"] == "propose"  # L1 floor: no grants in the seeded DB
    assert out["decision_basis"].startswith("pessimistic")
    assert "helper exec run" in out["next"]
    assert out["affected"] == [{"target_type": "host", "target_id": "node0"}]

    listed = await list_proposals(outcome="pending")
    assert isinstance(listed, list)
    assert [p["id"] for p in listed] == [out["id"]]
    fetched = await get_proposal(out["id"][:8])
    assert fetched["id"] == out["id"]
    assert fetched["artifact"]["kind"] == "action"
    assert fetched["artifact"]["action"]["domain"] == "hypervisor"
    assert fetched["decision_if_run_now"] == "propose"
    pending = await pending_actions()
    assert [p["id"] for p in pending] == [out["id"]]


async def test_propose_action_rejects_bad_manifests_without_writing(seeded_db: str) -> None:
    bad_kind = await propose_action("explode", "node0", 1, "lxc", "t")
    assert "action_kind" in bad_kind["error"]
    bad_guest = await propose_action("stop", "node0", 1, "docker", "t")
    assert "vm_kind" in bad_guest["error"]
    bad_radius = await propose_action("stop", "node0", 1, "lxc", "t", blast_radius="galaxy")
    assert "blast_radius" in bad_radius["error"]
    assert await list_proposals() == []


async def test_list_proposals_rejects_unknown_outcome(seeded_db: str) -> None:
    out = await list_proposals(outcome="maybe")
    assert isinstance(out, dict)
    assert "unknown outcome" in out["error"]


async def test_get_proposal_unknown_and_ambiguous(seeded_db: str) -> None:
    assert "no proposal" in (await get_proposal("ffffffff"))["error"]
    a = await propose_action("start", "node0", 1, "lxc", "a")
    b = await propose_action("start", "node0", 2, "lxc", "b")
    assert "error" not in a
    assert "error" not in b
    # Every uuid7 minted in the same millisecond shares a prefix; the empty
    # prefix is the one guaranteed to be ambiguous.
    assert "ambiguous" in (await get_proposal(""))["error"]


def test_error_strings_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """An adapter error may echo a credential; the MCP surface scrubs it."""
    from homelab_helper.secrets import forget_secrets, secret_from_env

    forget_secrets()
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_API_KEY", "unifi-key-0123456789")
    secret_from_env("HOMELAB_HELPER_UNIFI_API_KEY")
    from homelab_helper.secrets import redact

    assert redact("401 for X-API-KEY unifi-key-0123456789") == "401 for X-API-KEY ***"
    forget_secrets()


# ---------------------------------------------------------------------------
# retire_host
# ---------------------------------------------------------------------------


async def test_retire_host_marks_and_flags(seeded_db: str) -> None:
    before = await list_hosts()
    assert [h["retired"] for h in before] == [False]
    out = await retire_host("node0", rationale="decommissioned")
    assert out["already_retired"] is False
    assert out["findings_resolved"] == ["deadbeefcafe0000"]
    after = await list_hosts()
    assert [h["retired"] for h in after] == [True]
    full = await get_host("node0")
    assert full["retired"] is True
    assert "error" in await retire_host("ghost")
