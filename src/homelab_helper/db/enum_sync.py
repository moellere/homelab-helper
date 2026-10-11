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

from typing import TYPE_CHECKING

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


def enum_statements() -> list[str]:
    """One ``ALTER TYPE … ADD VALUE IF NOT EXISTS`` per declared member, sorted."""
    statements: list[str] = []
    for type_name, enum_class in sorted(declared_enums().items()):
        statements.extend(
            f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{member.name}'"
            for member in enum_class
        )
    return statements


__all__ = ["declared_enums", "enum_statements"]
