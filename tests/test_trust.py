"""Trust gradient tests (P6 PR A) — decide() matrix, grants, seeding, CLI.

The decide() matrix is the point of this file: every floor tier gets an
explicit case, because this function is the only gate between a proposal and
execution once the executor lands.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import AutonomyLevel, TrustDomain
from homelab_helper.db.models import Domain, ElevationWindow, Host, TrustBoundary, TrustHistory
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.trust import (
    ActionRequest,
    BoundaryView,
    GrantError,
    TrustContext,
    WindowView,
    decide,
    grant_cell,
    load_trust_context,
    operator_identity,
    seed_domains,
)

runner = CliRunner()

_NOW = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)


def _action(**overrides) -> ActionRequest:
    defaults = {
        "domain": TrustDomain.HYPERVISOR,
        "action_kind": "restart",
        "blast_radius": "single-host",
        "hostnames": ("node0",),
        "rollback_verified": True,
    }
    defaults.update(overrides)
    return ActionRequest(**defaults)


def _context(**overrides) -> TrustContext:
    defaults = {
        "cell_level": AutonomyLevel.PROPOSE,
        "cell_granted": False,
        "cell_on_probation": False,
        "domain_default": AutonomyLevel.PROPOSE,
        "domain_max": AutonomyLevel.AUTONOMOUS,
        "domain_absolute": False,
        "boundaries": (),
        "windows": (),
        "now": _NOW,
    }
    defaults.update(overrides)
    return TrustContext(**defaults)


def _window(scope: dict, *, expired: bool = False, revoked: bool = False) -> WindowView:
    return WindowView(
        window_id="w1",
        scope=scope,
        expires_at=_NOW + (timedelta(minutes=-1) if expired else timedelta(minutes=60)),
        revoked_at=_NOW - timedelta(minutes=1) if revoked else None,
    )


# ---------------------------------------------------------------------------
# decide() — tier 1: the cell level (AC1's floor)
# ---------------------------------------------------------------------------


def test_default_is_propose_and_nothing_more() -> None:
    """AC1: no grants anywhere → PROPOSE, exactly L1."""
    decision = decide(_action(), _context())
    assert decision.level == AutonomyLevel.PROPOSE
    assert decision.window_id is None
    assert any("no grant" in r for r in decision.reasons)


def test_granted_confirm_is_confirm() -> None:
    decision = decide(_action(), _context(cell_level=AutonomyLevel.CONFIRM, cell_granted=True))
    assert decision.level == AutonomyLevel.CONFIRM


def test_granted_autonomous_with_verified_rollback_runs() -> None:
    decision = decide(
        _action(rollback_verified=True),
        _context(cell_level=AutonomyLevel.AUTONOMOUS, cell_granted=True),
    )
    assert decision.level == AutonomyLevel.AUTONOMOUS


def test_block_cell_blocks() -> None:
    decision = decide(_action(), _context(cell_level=AutonomyLevel.BLOCK, cell_granted=True))
    assert decision.level == AutonomyLevel.BLOCK


def test_probation_caps_at_confirm() -> None:
    decision = decide(
        _action(),
        _context(cell_level=AutonomyLevel.AUTONOMOUS, cell_granted=True, cell_on_probation=True),
    )
    assert decision.level == AutonomyLevel.CONFIRM
    assert any("probation" in r for r in decision.reasons)


# ---------------------------------------------------------------------------
# decide() — tier 2: soft-hard floors
# ---------------------------------------------------------------------------


def test_domain_max_clamps() -> None:
    decision = decide(
        _action(),
        _context(
            cell_level=AutonomyLevel.AUTONOMOUS,
            cell_granted=True,
            domain_max=AutonomyLevel.CONFIRM,
        ),
    )
    assert decision.level == AutonomyLevel.CONFIRM


def test_boundary_ceiling_clamps() -> None:
    decision = decide(
        _action(),
        _context(
            cell_level=AutonomyLevel.AUTONOMOUS,
            cell_granted=True,
            boundaries=(BoundaryView("node0", AutonomyLevel.CONFIRM, absolute=False),),
        ),
    )
    assert decision.level == AutonomyLevel.CONFIRM
    assert any("boundary ceiling on node0" in r for r in decision.reasons)


def test_unverified_rollback_degrades_autonomous_to_confirm() -> None:
    """AC4's rule, in decide(): no verified rollback → CONFIRM."""
    decision = decide(
        _action(rollback_verified=False),
        _context(cell_level=AutonomyLevel.AUTONOMOUS, cell_granted=True),
    )
    assert decision.level == AutonomyLevel.CONFIRM
    assert any("no verified rollback" in r for r in decision.reasons)


# ---------------------------------------------------------------------------
# decide() — windows lift soft-hard floors, never absolute ones
# ---------------------------------------------------------------------------


def test_window_lets_autonomous_cross_rollback_floor() -> None:
    decision = decide(
        _action(rollback_verified=False),
        _context(
            cell_level=AutonomyLevel.AUTONOMOUS,
            cell_granted=True,
            windows=(_window({"domains": ["hypervisor"]}),),
        ),
    )
    assert decision.level == AutonomyLevel.AUTONOMOUS
    assert decision.window_id == "w1"
    assert any("best-effort snapshot" in r for r in decision.reasons)


def test_window_lifts_non_absolute_boundary() -> None:
    decision = decide(
        _action(),
        _context(
            cell_level=AutonomyLevel.AUTONOMOUS,
            cell_granted=True,
            boundaries=(BoundaryView("node0", AutonomyLevel.CONFIRM, absolute=False),),
            windows=(_window({"hosts": ["node0"]}),),
        ),
    )
    assert decision.level == AutonomyLevel.AUTONOMOUS
    assert any("lifted by window" in r for r in decision.reasons)


def test_window_never_crosses_absolute_boundary() -> None:
    """AC6: an absolute host boundary is window-proof."""
    decision = decide(
        _action(),
        _context(
            cell_level=AutonomyLevel.AUTONOMOUS,
            cell_granted=True,
            boundaries=(BoundaryView("nas", AutonomyLevel.PROPOSE, absolute=True),),
            windows=(_window({"hosts": ["node0", "nas"]}),),
        ),
    )
    assert decision.level == AutonomyLevel.PROPOSE
    assert any("window-proof" in r for r in decision.reasons)


def test_window_never_applies_to_absolute_domain() -> None:
    """AC6: secrets rejects every window."""
    decision = decide(
        _action(domain=TrustDomain.SECRETS),
        _context(
            cell_level=AutonomyLevel.AUTONOMOUS,
            cell_granted=True,
            domain_max=AutonomyLevel.PROPOSE,
            domain_absolute=True,
            windows=(_window({"domains": ["secrets"]}),),
        ),
    )
    assert decision.level == AutonomyLevel.PROPOSE
    assert decision.window_id is None


def test_expired_and_revoked_windows_ignored() -> None:
    for window in (
        _window({"domains": ["hypervisor"]}, expired=True),
        _window({"domains": ["hypervisor"]}, revoked=True),
    ):
        decision = decide(
            _action(rollback_verified=False),
            _context(cell_level=AutonomyLevel.AUTONOMOUS, cell_granted=True, windows=(window,)),
        )
        assert decision.level == AutonomyLevel.CONFIRM
        assert decision.window_id is None


def test_window_scope_must_match() -> None:
    decision = decide(
        _action(rollback_verified=False),
        _context(
            cell_level=AutonomyLevel.AUTONOMOUS,
            cell_granted=True,
            windows=(_window({"domains": ["storage"], "hosts": ["other-host"]}),),
        ),
    )
    assert decision.level == AutonomyLevel.CONFIRM


def test_window_matches_by_exact_cell() -> None:
    decision = decide(
        _action(rollback_verified=False),
        _context(
            cell_level=AutonomyLevel.AUTONOMOUS,
            cell_granted=True,
            windows=(_window({"cells": ["hypervisor/restart/single-host"]}),),
        ),
    )
    assert decision.level == AutonomyLevel.AUTONOMOUS


# ---------------------------------------------------------------------------
# no LLM in the authorization path — mechanical, not aspirational
# ---------------------------------------------------------------------------


def test_decide_path_never_imports_llm() -> None:
    code = (
        "import sys; import homelab_helper.engine.trust; "
        "bad = [m for m in sys.modules if m.startswith('homelab_helper.llm')]; "
        "assert not bad, f'LLM modules in the authorization path: {bad}'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# ---------------------------------------------------------------------------
# DB side: seeding, context loading, grants
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine):
    return make_sessionmaker(engine)


async def test_seed_domains_idempotent_and_secrets_absolute(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        first = await seed_domains(s)
        second = await seed_domains(s)
    assert first == len(TrustDomain)
    assert second == 0
    async with sessionmaker() as s:
        secrets = await s.get(Domain, TrustDomain.SECRETS)
        assert secrets is not None
        assert secrets.is_absolute is True
        assert secrets.max_level == AutonomyLevel.PROPOSE
        hypervisor = await s.get(Domain, TrustDomain.HYPERVISOR)
        assert hypervisor.default_level == AutonomyLevel.PROPOSE  # AC1 floor


async def test_grant_then_decide_via_loaded_context(sessionmaker) -> None:
    """AC2's authorization leg: grant CONFIRM on containers/restart/single-host."""
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.CONFIRM,
            actor="enoch",
        )
    action = _action(domain=TrustDomain.CONTAINERS)
    async with sessionmaker() as s:
        context = await load_trust_context(s, action)
    decision = decide(action, context)
    assert decision.level == AutonomyLevel.CONFIRM


async def test_grant_above_domain_max_refused(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        with pytest.raises(GrantError, match="secrets"):
            await grant_cell(
                s,
                TrustDomain.SECRETS,
                "rotate",
                "single-host",
                AutonomyLevel.CONFIRM,
                actor="enoch",
            )


async def test_grant_writes_trust_history(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.HYPERVISOR,
            "restart",
            "single-host",
            AutonomyLevel.CONFIRM,
            actor="enoch",
        )
    async with sessionmaker() as s:
        history = (await s.execute(select(TrustHistory))).scalars().all()
    assert len(history) == 1
    assert history[0].event == "grant"
    assert history[0].actor == "enoch"
    assert history[0].detail["level"] == "confirm"


async def test_context_loads_boundary_and_window(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        host = Host(hostname="node0")
        s.add(host)
        await s.flush()
        s.add(
            TrustBoundary(
                host_id=host.id, max_agent_authority=AutonomyLevel.CONFIRM, absolute=False
            )
        )
        s.add(
            ElevationWindow(
                opened_by="enoch",
                reason="maintenance",
                scope={"hosts": ["node0"]},
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    async with sessionmaker() as s:
        context = await load_trust_context(s, _action())
    assert len(context.boundaries) == 1
    assert context.boundaries[0].hostname == "node0"
    assert len(context.windows) == 1


def test_operator_identity_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_OPERATOR", "enoch")
    assert operator_identity() == "enoch"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'trust.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    monkeypatch.setenv("HOMELAB_HELPER_OPERATOR", "enoch")

    async def _init() -> None:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as s:
            await seed_domains(s)
        await engine.dispose()

    asyncio.run(_init())


def test_cli_trust_show_default_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _db(tmp_path, monkeypatch)
    result = runner.invoke(app, ["trust", "show"])
    assert result.exit_code == 0
    assert "secrets" in result.stdout
    assert "no cells granted" in result.stdout
    assert "L1 floor" in result.stdout


def test_cli_trust_grant_and_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _db(tmp_path, monkeypatch)
    result = runner.invoke(
        app, ["trust", "grant", "containers", "restart", "single-host", "confirm"]
    )
    assert result.exit_code == 0
    assert "granted" in result.stdout
    assert "enoch" in result.stdout
    shown = runner.invoke(app, ["trust", "show"])
    assert "restart" in shown.stdout
    assert "single-host" in shown.stdout


def test_cli_trust_grant_secrets_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _db(tmp_path, monkeypatch)
    result = runner.invoke(
        app, ["trust", "grant", "secrets", "rotate", "single-host", "autonomous"]
    )
    assert result.exit_code == 2
    assert "refused" in result.stdout


def test_cli_trust_grant_bad_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _db(tmp_path, monkeypatch)
    result = runner.invoke(app, ["trust", "grant", "containers", "restart", "single-host", "yolo"])
    assert result.exit_code != 0
