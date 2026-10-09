"""CLI tests for ``helper digest show|send|history``.

File-backed SQLite (each CLI invocation builds its own engine), and the
Home Assistant channel is never reached: the delivery variables are unset, so
``send`` takes its "unconfigured" path and records that rather than buzzing
anything.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ProposalLog, ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture
async def digest_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """One recent finding and one pending proposal; no delivery configured."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'digest.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    for var in (
        "HOMELAB_HELPER_HASS_URL",
        "HOMELAB_HELPER_HASS_TOKEN",
        "HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE",
    ):
        monkeypatch.delenv(var, raising=False)

    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)
    now = datetime.now(UTC)
    async with session_scope(sm) as s:
        s.add(
            ReconciliationFinding(
                kind=FindingKind.VERSION_DRIFT,
                severity=FindingSeverity.HIGH,
                fingerprint="a" * 16,
                title="bmax1 is behind on packages",
                description="123 pending",
                affected=[{"target_type": "host", "target_id": "bmax1"}],
                status=FindingStatus.OPEN,
                first_seen=now - timedelta(days=1),
                last_seen=now,
            )
        )
        s.add(
            ProposalLog(
                title="Resize esphome-lxc to 1 core",
                artifact={"kind": "action"},
                blast_radius="single-host",
            )
        )
    await engine.dispose()
    return url


@pytest.fixture
async def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Schema, no rows — the quiet-window case."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'quiet.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return url


def _runs(url: str) -> list[sqlite3.Row]:
    """Read digest_run synchronously — these tests run outside an event loop,
    because CliRunner's own asyncio.run cannot nest inside one."""
    con = sqlite3.connect(url.split("///", 1)[1])
    con.row_factory = sqlite3.Row
    try:
        return list(
            con.execute(
                "SELECT window_start, window_end, delivery, counts, quiet "
                "FROM digest_run ORDER BY generated_at"
            )
        )
    finally:
        con.close()


def test_show_renders_the_page_without_recording(digest_db: str) -> None:
    result = runner.invoke(app, ["digest", "show"])
    assert result.exit_code == 0, result.output
    assert "bmax1 is behind on packages" in result.output
    assert "not recorded" in result.output


def test_show_does_not_move_the_window(digest_db: str) -> None:
    runner.invoke(app, ["digest", "show"])
    assert _runs(digest_db) == [], "looking must not eat a week of changes"


def test_show_writes_the_page_to_a_file(digest_db: str, tmp_path: Path) -> None:
    out = tmp_path / "digest.md"
    result = runner.invoke(app, ["digest", "show", "--out", str(out)])
    assert result.exit_code == 0, result.output
    page = out.read_text()
    assert page.startswith("# homelab-helper digest")
    assert "## What is recommended" in page


def test_send_without_a_channel_records_unconfigured(digest_db: str) -> None:
    result = runner.invoke(app, ["digest", "send"])
    assert result.exit_code == 0, result.output
    assert "not sent" in result.output


def test_send_records_the_window(digest_db: str) -> None:
    runner.invoke(app, ["digest", "send"])
    runs = _runs(digest_db)
    assert len(runs) == 1
    assert runs[0]["delivery"] == "unconfigured"
    assert json.loads(runs[0]["counts"])["opened"] == 1


def test_a_second_send_covers_only_the_new_window(digest_db: str) -> None:
    runner.invoke(app, ["digest", "send"])
    runner.invoke(app, ["digest", "send"])
    runs = _runs(digest_db)
    assert len(runs) == 2
    assert runs[1]["window_start"] == runs[0]["window_end"]
    assert json.loads(runs[1]["counts"])["opened"] == 0, "the finding was already reported"


def test_dry_run_neither_sends_nor_records(digest_db: str) -> None:
    result = runner.invoke(app, ["digest", "send", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would send" in result.output
    assert "dry run" in result.output


def test_dry_run_leaves_no_row(digest_db: str) -> None:
    runner.invoke(app, ["digest", "send", "--dry-run"])
    assert _runs(digest_db) == []


def test_quiet_window_is_recorded_but_not_sent(
    empty_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = empty_db
    result = runner.invoke(app, ["digest", "send"])
    assert result.exit_code == 0, result.output
    assert "quiet window" in result.output
    runs = _runs(url)
    assert len(runs) == 1
    assert runs[0]["quiet"]


def test_history_lists_what_was_sent(digest_db: str) -> None:
    runner.invoke(app, ["digest", "send"])
    result = runner.invoke(app, ["digest", "history"])
    assert result.exit_code == 0, result.output
    assert "1 digest(s)" in result.output
    assert "unconfigured" in result.output
