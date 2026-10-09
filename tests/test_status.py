"""Phase 9.7a — the status snapshot, the HTTP endpoint and ``helper status``."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    AutonomyLevel,
    FindingKind,
    FindingSeverity,
    FindingStatus,
    PrivilegeLevel,
    ProposalOutcome,
    TrustDomain,
)
from homelab_helper.db.models import (
    CellTrust,
    DiscoveryRun,
    ExecutionReceipt,
    Host,
    Probe,
    ProposalLog,
    ReconciliationFinding,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.status import _health, snapshot
from homelab_helper.status_api import build_app

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _finding(fp: str, severity: FindingSeverity, status: FindingStatus) -> ReconciliationFinding:
    return ReconciliationFinding(
        kind=FindingKind.STRAY_CONFIG,
        severity=severity,
        fingerprint=fp,
        title=fp,
        description="",
        status=status,
    )


async def _seed(url: str, *, failed_receipt: bool = False) -> None:
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope(make_sessionmaker(engine)) as s:
        s.add(Host(hostname="node0", primary_ip="10.0.1.20"))
        s.add(_finding("f-high", FindingSeverity.HIGH, FindingStatus.OPEN))
        s.add(_finding("f-med", FindingSeverity.MEDIUM, FindingStatus.ACKNOWLEDGED))
        s.add(_finding("f-low", FindingSeverity.LOW, FindingStatus.RESOLVED))
        action = ProposalLog(
            title="Restart thing",
            artifact={"kind": "action", "version": 1},
            proposed_at=T0 - timedelta(hours=2),
        )
        s.add(action)
        s.add(
            ProposalLog(
                title="A note", artifact={"kind": "markdown"}, proposed_at=T0 - timedelta(hours=1)
            )
        )
        s.add(
            ProposalLog(
                title="Done already",
                artifact={"kind": "action"},
                outcome=ProposalOutcome.USER_ACCEPTED,
            )
        )
        probe = Probe(
            name="host.smart",
            version="0.1.0",
            module_path="x",
            required_privilege=PrivilegeLevel.USER,
        )
        s.add(probe)
        await s.flush()
        for name, at, ok in (
            ("host.smart", T0 - timedelta(hours=3), True),
            ("host.smart", T0 - timedelta(hours=1), False),
            ("host.smart", T0 - timedelta(days=3), True),
        ):
            s.add(
                DiscoveryRun(
                    probe_id=probe.id,
                    probe_name=name,
                    probe_version="0.1.0",
                    privilege_level=PrivilegeLevel.USER,
                    started_at=at,
                    success=ok,
                )
            )
        s.add(
            CellTrust(
                domain=TrustDomain.HYPERVISOR,
                action_kind="vm-power",
                blast_radius="single-guest",
                level=AutonomyLevel.CONFIRM,
            )
        )
        if failed_receipt:
            s.add(
                ExecutionReceipt(
                    proposal_id=action.id,
                    executed_at=T0 - timedelta(minutes=30),
                    actor="daemon",
                    decision_level=AutonomyLevel.AUTONOMOUS,
                    action={"kind": "vm-power"},
                    outcome="failed",
                    error="boom",
                )
            )
    await engine.dispose()


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'status.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    await _seed(url)
    return url


async def _snap(url: str, **kw):
    engine = make_engine(url)
    try:
        async with make_sessionmaker(engine)() as session:
            return await snapshot(session, now=T0, **kw)
    finally:
        await engine.dispose()


async def test_snapshot_rolls_up_findings_proposals_runs_and_trust(db: str) -> None:
    snap = await _snap(db)
    assert snap["findings"]["open"] == 2  # resolved one excluded
    assert snap["findings"]["highest"] == "high"
    assert snap["findings"]["by_severity"]["high"] == 1
    assert snap["findings"]["by_severity"]["medium"] == 1
    assert snap["findings"]["by_kind"] == {"stray-config": 2}
    assert snap["proposals"]["pending"] == 1  # only pending *action* proposals
    assert snap["proposals"]["titles"] == ["Restart thing"]
    assert snap["discovery"]["age_seconds"] == 3600
    assert snap["discovery"]["runs_24h"] == 2
    assert snap["discovery"]["failed_24h"] == 1
    assert "host.smart" in snap["discovery"]["probes"]
    assert snap["assertions"]["last_run_at"] is None
    assert snap["trust"]["cells_by_level"] == {"confirm": 1}
    assert snap["trust"]["open_windows"] == 0
    assert snap["inventory"]["hosts"] == 1
    assert snap["health"] == "critical"  # the open high finding
    assert "2 open finding(s), highest high" in snap["headline"]
    assert snap["generated_at"] == T0.isoformat()


async def test_failed_receipt_is_critical(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"
    await _seed(url, failed_receipt=True)
    snap = await _snap(url)
    assert snap["receipts_24h"] == {
        "succeeded": 0,
        "failed": 1,
        "last_at": (T0 - timedelta(minutes=30)).isoformat(),
    }
    assert snap["health"] == "critical"


def test_health_ladder() -> None:
    quiet = dict.fromkeys(("critical", "high", "medium", "low", "info"), 0)
    stale = timedelta(hours=12)
    assert _health(quiet, 0, 0, 60, stale) == "ok"
    assert _health({**quiet, "low": 3}, 0, 0, 60, stale) == "ok"
    assert _health({**quiet, "medium": 1}, 0, 0, 60, stale) == "attention"
    assert _health(quiet, 1, 0, 60, stale) == "attention"
    assert _health(quiet, 0, 0, None, stale) == "attention"  # never discovered
    assert _health(quiet, 0, 0, 13 * 3600, stale) == "attention"  # stale
    assert _health({**quiet, "high": 1}, 0, 0, 60, stale) == "critical"
    assert _health(quiet, 0, 1, 60, stale) == "critical"


async def test_empty_database_is_attention_not_an_error(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    snap = await _snap(url)
    assert snap["findings"]["open"] == 0
    assert snap["findings"]["highest"] is None
    assert snap["discovery"]["last_run_at"] is None
    assert snap["health"] == "attention"
    assert "discovery has never run" in snap["headline"]


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_endpoint_serves_snapshot_and_healthz(db: str) -> None:
    async with _client(build_app(url=db, token="")) as c:
        assert (await c.get("/healthz")).json() == {"ok": True}
        r = await c.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["findings"]["open"] == 2
    assert body["proposals"]["pending"] == 1
    assert set(body) >= {"health", "headline", "findings", "proposals", "discovery", "trust"}


async def test_endpoint_requires_the_token_when_one_is_set(db: str) -> None:
    async with _client(build_app(url=db, token="s3cret")) as c:
        assert (await c.get("/status")).status_code == 401
        assert (await c.get("/status", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (
            await c.get("/status", headers={"Authorization": "Bearer s3cret"})
        ).status_code == 200
        assert (await c.get("/status", headers={"X-API-Token": "s3cret"})).status_code == 200
        assert (await c.get("/healthz")).status_code == 200  # liveness never needs a token


def test_endpoint_token_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_STATUS_TOKEN", "from-env")
    app = build_app(url="sqlite+aiosqlite:///:memory:")
    routes = {getattr(r, "path", None): r for r in app.routes}
    assert "/status" in routes


def test_endpoint_has_only_get_routes() -> None:
    app = build_app(url="sqlite+aiosqlite:///:memory:", token="")
    paths = {}
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods:
            paths[route.path] = set(methods) - {"HEAD"}
    assert paths == {"/healthz": {"GET"}, "/status": {"GET"}}


def test_status_path_never_imports_the_executor_or_an_llm() -> None:
    # The rollup itself: no LLM, no write path. The HTTP layer reaches
    # ``config`` (which knows the LLM *settings*), so it is held to the
    # executor rule only.
    code = (
        "import sys; import homelab_helper.engine.status; "
        "bad = [m for m in sys.modules if m.startswith('homelab_helper.llm') "
        "or m == 'homelab_helper.engine.executor']; "
        "assert not bad, f'write or LLM modules on the status path: {bad}'; "
        "import homelab_helper.status_api; "
        "assert 'homelab_helper.engine.executor' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_status_show(db: str) -> None:
    result = CliRunner().invoke(app, ["status", "show"])
    assert result.exit_code == 0, result.output
    assert "critical" in result.output
    assert "Restart thing" in result.output


def test_cli_status_show_json(db: str) -> None:
    result = CliRunner().invoke(app, ["status", "show", "--json"])
    assert result.exit_code == 0, result.output
    assert '"health": "critical"' in result.output
