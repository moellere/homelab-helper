"""Keep Postgres enum types in step with the Python enums.

SQLAlchemy's ``Enum`` column becomes a native ``CREATE TYPE`` on Postgres,
created with whatever members existed when the table's migration ran; a
member added to the Python enum later is unknown to the database until
``ALTER TYPE … ADD VALUE``. SQLite stores the member name as text and needs
nothing. ``enum_statements()`` lists every ``ADD VALUE IF NOT EXISTS`` the
model metadata implies, so a migration can apply them all in one line and a
test can hold the live database to the same list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from homelab_helper.db.base import Base

if TYPE_CHECKING:
    from enum import Enum


def declared_enums() -> dict[str, type[Enum]]:
    """``{postgres type name: python enum}`` for every native enum column."""
    import homelab_helper.db.models  # noqa: F401, PLC0415 - populate Base.metadata

    found: dict[str, type[Enum]] = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            kind = column.type
            if isinstance(kind, sa.Enum) and kind.enum_class is not None and kind.name:
                found[kind.name] = kind.enum_class
    return found


def enum_statements(existing: set[str] | None = None) -> list[str]:
    """One ``ALTER TYPE … ADD VALUE IF NOT EXISTS`` per declared member, sorted.

    ``existing`` narrows to the types already in the database: a migration
    runs with the models of *today*, which may declare a type that a later
    migration creates, and altering that one would fail.
    """
    statements: list[str] = []
    for type_name, enum_class in sorted(declared_enums().items()):
        if existing is not None and type_name not in existing:
            continue
        statements.extend(
            f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{member.name}'"
            for member in enum_class
        )
    return statements


def apply_enum_sync(op: Any) -> int:
    """From a migration: add every missing member to every enum type that exists. Postgres only."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return 0
    existing = {row[0] for row in bind.execute(sa.text("SELECT typname FROM pg_type")).fetchall()}
    statements = enum_statements(existing)
    for statement in statements:
        op.execute(statement)
    return len(statements)


__all__ = ["apply_enum_sync", "declared_enums", "enum_statements"]
