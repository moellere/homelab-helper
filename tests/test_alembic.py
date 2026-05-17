"""Verify the Alembic migration round-trips: upgrade head → tables exist → downgrade base → tables gone."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


@pytest.fixture
def alembic_db(tmp_path, monkeypatch):
    """Spin up a temporary SQLite DB and point Alembic at it."""
    db_path = tmp_path / "alembic-test.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    # Locate alembic.ini at the project root
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "alembic.ini"
        if candidate.exists():
            cfg = Config(str(candidate))
            break
    else:
        pytest.fail("alembic.ini not found")

    return cfg, db_path


def _table_names(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        con.close()


def test_alembic_upgrade_creates_all_eleven_tables(alembic_db) -> None:
    cfg, db_path = alembic_db
    command.upgrade(cfg, "head")
    tables = _table_names(db_path)
    expected = {
        "host",
        "physical_part",
        "placement",
        "operational_intent",
        "probe",
        "discovery_run",
        "observation",
        "configuration_assertion",
        "assertion_run",
        "reconciliation_finding",
        "proposal_log",
    }
    assert expected.issubset(tables), f"missing: {expected - tables}"
    assert "alembic_version" in tables


def test_alembic_downgrade_drops_application_tables(alembic_db) -> None:
    cfg, db_path = alembic_db
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    tables = _table_names(db_path)
    # alembic_version row stays around; application tables should be gone.
    assert tables == {"alembic_version"}, f"unexpected tables remain: {tables}"
