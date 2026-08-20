"""CLI tests for `helper plan` (Phase 5)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

import homelab_helper.cli.plan as cli_plan
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import Architecture
from homelab_helper.db.models import Host
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from tests.test_cli_chat import EchoRouter, RefusingRouter

runner = CliRunner()

_GB = 1024**3


def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'plan.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    async def _init() -> None:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as s:
            s.add(
                Host(
                    hostname="nuc",
                    arch=Architecture.AMD64,
                    capabilities={"mem_total_bytes": 32 * _GB, "cpu_threads": 12, "gpu_count": 1},
                )
            )
            s.add(
                Host(
                    hostname="pi4",
                    arch=Architecture.ARM64,
                    capabilities={"mem_total_bytes": 8 * _GB, "cpu_threads": 4},
                )
            )
        await engine.dispose()

    asyncio.run(_init())
    return url


def test_plan_workloads_lists_library() -> None:
    result = runner.invoke(app, ["plan", "workloads"])
    assert result.exit_code == 0
    assert "immich" in result.stdout
    assert "workload library" in result.stdout


def test_plan_workloads_category_filter() -> None:
    result = runner.invoke(app, ["plan", "workloads", "--category", "nvr"])
    assert result.exit_code == 0
    assert "frigate" in result.stdout
    assert "immich" not in result.stdout


def test_plan_add_workload_ranks_hosts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: 'if I add Immich, where should it run?' with reasoning."""
    _db(tmp_path, monkeypatch)
    result = runner.invoke(app, ["plan", "add-workload", "immich"])
    assert result.exit_code == 0
    assert "nuc" in result.stdout  # eligible + ranked
    assert "rejected pi4" in result.stdout  # arm64 not supported by immich
    assert "arch" in result.stdout


def test_plan_add_workload_unknown_suggests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db(tmp_path, monkeypatch)
    result = runner.invoke(app, ["plan", "add-workload", "imich"])
    assert result.exit_code == 2
    assert "did you mean" in result.stdout
    assert "immich" in result.stdout


def test_plan_add_workload_narrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _db(tmp_path, monkeypatch)
    router = EchoRouter(reply="Run Immich on nuc; the GPU accelerates ML tagging.")
    monkeypatch.setattr(cli_plan, "_load_router", lambda: router)
    result = runner.invoke(app, ["plan", "add-workload", "immich", "--narrate"])
    assert result.exit_code == 0
    assert "ML tagging" in result.stdout
    # The narration prompt carried the deterministic report, not raw DB rows.
    prompt = router.messages[0][0]["content"]
    assert "RANKED CANDIDATES" in prompt
    assert "nuc" in prompt


def test_plan_narrate_refusal_keeps_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Planning tier refusal degrades to the deterministic table, exit 0."""
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_plan, "_load_router", RefusingRouter)
    result = runner.invoke(app, ["plan", "add-workload", "immich", "--narrate"])
    assert result.exit_code == 0
    assert "placement candidates" in result.stdout
    assert "narration unavailable" in result.stdout
