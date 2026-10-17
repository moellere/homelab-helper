"""print-warden source: adapter, findings reconcile, run_discovery and `helper discover printwarden`."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

import homelab_helper.cli.discover as cli_discover
import homelab_helper.mcp_server as mcp_srv
from homelab_helper.adapters.printwarden import (
    PrintWardenAdapter,
    PrintWardenAPIError,
    PrintWardenConfigError,
)
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.printwarden import printer_issues, reconcile_printwarden

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

URL = "http://warden.lan:8710"


def _printer(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "connected": True,
        "klippy": "ready",
        "state": "standby",
        "progress_pct": None,
        "finishes_in_s": None,
        "bed": "clear",
        "queue": 0,
        "last_update_s_ago": 2.0,
    }
    return {**base, **over}


def _status(**printers: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "api": 1, "version": "0.2.0", "printers": printers}


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> PrintWardenAdapter:
    client = httpx.AsyncClient(base_url=URL, transport=httpx.MockTransport(handler))
    return PrintWardenAdapter(URL, client=client)


def _serving(body: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/healthz"
        return httpx.Response(200, json=body)

    return handler


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


# --- adapter ---------------------------------------------------------------


async def test_status_reads_api_1() -> None:
    adapter = _adapter(_serving(_status(sw1=_printer())))
    body = await adapter.status()
    assert body["printers"]["sw1"]["klippy"] == "ready"
    assert await adapter.health_check() == (True, None)


async def test_status_refuses_an_unknown_api() -> None:
    adapter = _adapter(_serving({**_status(sw1=_printer()), "api": 2}))
    with pytest.raises(PrintWardenAPIError, match="api 2"):
        await adapter.status()
    ok, err = await adapter.health_check()
    assert not ok
    assert err
    assert "not supported" in err


async def test_status_refuses_a_body_without_printers() -> None:
    adapter = _adapter(_serving({"api": 1, "ok": True}))
    with pytest.raises(PrintWardenAPIError, match="printers"):
        await adapter.status()


def test_from_env_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_PRINTWARDEN_URL", raising=False)
    with pytest.raises(PrintWardenConfigError):
        PrintWardenAdapter.from_env()
    monkeypatch.setenv("HOMELAB_HELPER_PRINTWARDEN_URL", URL + "/")
    assert PrintWardenAdapter.from_env().url == URL


# --- checks ----------------------------------------------------------------


def test_printer_issues() -> None:
    status = _status(
        ok=_printer(),
        off=_printer(connected=False, klippy=None, last_update_s_ago=None),
        down=_printer(klippy="shutdown"),
        boot=_printer(klippy="startup"),
        quiet=_printer(last_update_s_ago=3600.0),
    )
    got = {(i.target_id, i.category): i.severity for i in printer_issues(status)}
    assert got == {
        ("print-warden/off", "printer-offline"): FindingSeverity.MEDIUM,
        ("print-warden/down", "printer-klippy"): FindingSeverity.HIGH,
        ("print-warden/boot", "printer-klippy"): FindingSeverity.MEDIUM,
        ("print-warden/quiet", "printer-telemetry-stale"): FindingSeverity.LOW,
    }


# --- reconcile -------------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(engine)


async def _open(session: AsyncSession) -> set[str]:
    rows = (
        (
            await session.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.kind == FindingKind.SERVICE_UNHEALTHY,
                    ReconciliationFinding.status == FindingStatus.OPEN,
                )
            )
        )
        .scalars()
        .all()
    )
    return {r.title for r in rows}


async def test_unreachable_opens_then_resolves(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(sessionmaker) as s:
        _, counts = await reconcile_printwarden(s, URL, None, "connection refused")
        assert counts["opened"] == 1
        assert await _open(s) == {"print-warden is unreachable"}
    async with session_scope(sessionmaker) as s:
        _, counts = await reconcile_printwarden(s, URL, _status(sw1=_printer()))
        assert counts["resolved"] == 1
        assert await _open(s) == set()


async def test_unreachable_does_not_resolve_printer_findings(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Invariant 1: a warden that cannot be read observes nothing about its printers."""
    async with session_scope(sessionmaker) as s:
        await reconcile_printwarden(s, URL, _status(sw1=_printer(klippy="shutdown")))
    async with session_scope(sessionmaker) as s:
        _, counts = await reconcile_printwarden(s, URL, None, "timeout")
        assert counts["resolved"] == 0
        assert await _open(s) == {"Klipper on sw1 is shutdown", "print-warden is unreachable"}


async def test_offline_printer_keeps_its_klippy_finding(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Klipper's state is unknown while Moonraker is down, so its finding stands until seen ready."""
    async with session_scope(sessionmaker) as s:
        await reconcile_printwarden(s, URL, _status(sw1=_printer(klippy="shutdown")))
    async with session_scope(sessionmaker) as s:
        await reconcile_printwarden(s, URL, _status(sw1=_printer(connected=False, klippy=None)))
        assert await _open(s) == {
            "Klipper on sw1 is shutdown",
            "Printer sw1 is offline to print-warden",
        }
    async with session_scope(sessionmaker) as s:
        _, counts = await reconcile_printwarden(s, URL, _status(sw1=_printer()))
        assert counts["resolved"] == 2
        assert await _open(s) == set()


# --- run_discovery + CLI ---------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'pw.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    async def _init() -> None:
        eng = make_engine(url)
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await eng.dispose()

    asyncio.run(_init())
    return url


def test_run_discovery_printwarden(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _status(sw1=_printer(), sw2=_printer(connected=False))
    monkeypatch.setattr(mcp_srv, "_load_printwarden_adapter", lambda: _adapter(_serving(body)))
    out = asyncio.run(mcp_srv.run_discovery("printwarden"))
    assert out["reachable"]
    assert out["printers"] == ["sw1", "sw2"]
    assert [i["category"] for i in out["issues"]] == ["printer-offline"]
    assert out["findings"]["opened"] == 1

    monkeypatch.setattr(mcp_srv, "_load_printwarden_adapter", lambda: _adapter(_down))
    out = asyncio.run(mcp_srv.run_discovery("printwarden"))
    assert not out["reachable"]
    assert out["findings"] == {"opened": 1, "reopened": 0, "updated": 0, "resolved": 0}


runner = CliRunner()


def test_cli_discover_printwarden_shows_printers(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _status(sw1=_printer(state="printing", progress_pct=42.0, queue=2))
    monkeypatch.setattr(cli_discover, "_load_printwarden_adapter", lambda: _adapter(_serving(body)))
    result = runner.invoke(app, ["discover", "printwarden"])
    assert result.exit_code == 0, result.stdout
    assert "1 printer(s)" in result.stdout
    assert "42.0%" in result.stdout


def test_cli_discover_printwarden_unreachable_persists(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_discover, "_load_printwarden_adapter", lambda: _adapter(_down))
    dry = runner.invoke(app, ["discover", "printwarden", "--persist", "--dry-run"])
    assert dry.exit_code == 1
    assert "unreachable" in dry.stdout
    assert "rolled back" in dry.stdout

    result = runner.invoke(app, ["discover", "printwarden", "--persist"])
    assert result.exit_code == 1
    assert "1 opened" in result.stdout

    async def _count() -> int:
        eng = make_engine(db_url)
        try:
            async with session_scope(make_sessionmaker(eng)) as s:
                return len(await _open(s))
        finally:
            await eng.dispose()

    assert asyncio.run(_count()) == 1


def test_shutdown_cause_failing_and_maintenance():
    status = _status(
        hot=_printer(
            klippy="shutdown",
            last_shutdown={
                "cause": "thermal_runaway",
                "hint": "check the thermistor and heater",
                "s_ago": 60,
            },
        ),
        flaky=_printer(outcomes_7d={"prints": 4, "failed": 3}),
        few=_printer(outcomes_7d={"prints": 2, "failed": 2}),
        worn=_printer(connected=False, maintenance_due=["nozzle", "belts"]),
    )
    issues = {(i.target_id, i.category): i for i in printer_issues(status)}
    hot = issues[("print-warden/hot", "printer-klippy")]
    assert hot.title == "Klipper on hot is shutdown: thermal runaway"
    assert "check the thermistor and heater" in hot.description
    assert issues[("print-warden/flaky", "printer-failing")].severity == FindingSeverity.MEDIUM
    assert ("print-warden/few", "printer-failing") not in issues
    worn = issues[("print-warden/worn", "printer-maintenance")]
    assert worn.title == "Maintenance due on worn: nozzle, belts"
    assert ("print-warden/worn", "printer-offline") in issues


async def test_ledger_findings_resolve_only_when_reported(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    worn = {"maintenance_due": ["nozzle"], "outcomes_7d": {"prints": 0, "failed": 0}}
    async with session_scope(sessionmaker) as s:
        await reconcile_printwarden(s, URL, _status(sw1=_printer(**worn)))
        assert await _open(s) == {"Maintenance due on sw1: nozzle"}
    async with session_scope(sessionmaker) as s:
        await reconcile_printwarden(s, URL, _status(sw1=_printer()))
        assert await _open(s) == {"Maintenance due on sw1: nozzle"}, (
            "an older warden observes nothing"
        )
    async with session_scope(sessionmaker) as s:
        clean = {"maintenance_due": [], "outcomes_7d": {"prints": 1, "failed": 0}}
        _, counts = await reconcile_printwarden(
            s, URL, _status(sw1=_printer(connected=False, **clean))
        )
        assert counts["resolved"] == 1
        assert await _open(s) == {"Printer sw1 is offline to print-warden"}
