"""Verify the Alembic migration round-trips: upgrade head → tables exist → downgrade base → tables gone.

The config comes from ``homelab_helper.db.migrate`` — the same code path
``helper db init`` uses — so these tests also prove the packaged migrations
work with no ``alembic.ini`` in sight (the fixture runs from a temp cwd).
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from alembic import command

from homelab_helper.db.base import Base
from homelab_helper.db.migrate import MIGRATIONS_DIR, alembic_config
from homelab_helper.db.models import ExecutionReceipt  # noqa: F401 - populate metadata

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def alembic_db(tmp_path, monkeypatch):
    """Spin up a temporary SQLite DB and point Alembic at it."""
    db_path = tmp_path / "alembic-test.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.delenv("HOMELAB_HELPER_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    return alembic_config(url), db_path


def test_migrations_ship_inside_the_package() -> None:
    assert MIGRATIONS_DIR.name == "migrations"
    assert MIGRATIONS_DIR.parent.name == "homelab_helper"
    assert (MIGRATIONS_DIR / "env.py").is_file()
    assert any((MIGRATIONS_DIR / "versions").glob("*.py"))


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


def _columns(db_path: Path, table: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def test_migrated_schema_covers_every_model(alembic_db) -> None:
    """``upgrade head`` must produce every table *and column* the models declare.

    The subset check above only ever knew about the Slice-1 eleven, so a
    migration that stopped being reachable — left outside the packaged
    versions directory, say — was invisible to it while ``helper db init``
    quietly produced a database missing that table. Columns are checked too:
    a migration that only adds columns to an existing table would otherwise
    still go missing unnoticed.
    """
    cfg, db_path = alembic_db
    command.upgrade(cfg, "head")

    tables = _table_names(db_path)
    declared = set(Base.metadata.tables)
    assert declared <= tables, f"migrations do not create: {sorted(declared - tables)}"

    missing: dict[str, list[str]] = {}
    for name, table in Base.metadata.tables.items():
        gap = {c.name for c in table.columns} - _columns(db_path, name)
        if gap:
            missing[name] = sorted(gap)
    assert not missing, f"migrated schema is missing columns: {missing}"


def test_alembic_downgrade_drops_application_tables(alembic_db) -> None:
    cfg, db_path = alembic_db
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    tables = _table_names(db_path)
    # alembic_version row stays around; application tables should be gone.
    assert tables == {"alembic_version"}, f"unexpected tables remain: {tables}"
