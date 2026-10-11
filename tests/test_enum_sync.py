"""Postgres enum types follow the Python enums (migration e3f8b2c6d9a1)."""

from __future__ import annotations

import asyncio
import os

import pytest
from alembic import command
from sqlalchemy import text

from homelab_helper.db.enum_sync import declared_enums, enum_statements
from homelab_helper.db.enums import FindingKind, IntentState
from homelab_helper.db.migrate import alembic_config
from homelab_helper.db.session import make_engine
from tests.conftest import PG_VAR, drop_postgres_schema


def test_statements_cover_every_member_of_every_native_enum() -> None:
    declared = declared_enums()
    assert declared["findingkind"] is FindingKind
    assert declared["intentstate"] is IntentState
    statements = enum_statements()
    assert "ALTER TYPE findingkind ADD VALUE IF NOT EXISTS 'RIGHTSIZING'" in statements
    assert "ALTER TYPE findingkind ADD VALUE IF NOT EXISTS 'CAPACITY'" in statements
    assert "ALTER TYPE intentstate ADD VALUE IF NOT EXISTS 'NO_NEW_GUESTS'" in statements
    assert len(statements) == sum(len(list(e)) for e in declared.values())
    assert statements == sorted(statements, key=lambda s: s.split()[2])  # grouped by type
    # A migration older than a type's table must not try to alter that type.
    narrowed = enum_statements({"findingkind"})
    assert narrowed
    assert all(s.startswith("ALTER TYPE findingkind ") for s in narrowed)


@pytest.mark.postgres_only
def test_migrated_postgres_types_hold_every_member(tmp_path, monkeypatch) -> None:
    # The redirect in conftest sends this SQLite URL — and Alembic's engine — to Postgres.
    url = f"sqlite+aiosqlite:///{tmp_path / 'enums.db'}"
    monkeypatch.delenv("HOMELAB_HELPER_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    drop_postgres_schema(os.environ[PG_VAR])  # other tests' create_all left types behind
    command.upgrade(alembic_config(url), "head")

    async def _labels() -> list[tuple[str, str]]:
        engine = make_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT t.typname, e.enumlabel FROM pg_type t "
                        "JOIN pg_enum e ON e.enumtypid = t.oid"
                    )
                )
                return [(str(a), str(b)) for a, b in result.all()]
        finally:
            await engine.dispose()

    rows = asyncio.run(_labels())
    live: dict[str, set[str]] = {}
    for typname, label in rows:
        live.setdefault(typname, set()).add(label)
    for type_name, enum_class in declared_enums().items():
        missing = {m.name for m in enum_class} - live.get(type_name, set())
        assert not missing, f"{type_name} lacks {sorted(missing)}"
