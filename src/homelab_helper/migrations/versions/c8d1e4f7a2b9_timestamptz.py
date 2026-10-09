"""timestamps with time zone

Every persisted timestamp is written by ``db.base.now()`` as an aware UTC
value, but the columns were declared ``DateTime()`` — ``timestamp without
time zone`` on Postgres, which asyncpg refuses to bind an aware value to. This
converts every such column to ``timestamptz`` (the stored values were UTC) so
Postgres accepts what the models write and hands back aware datetimes. SQLite
stores ISO text whatever the declared type, so there is nothing to do there.

Revision ID: c8d1e4f7a2b9
Revises: b4e1f7a9c3d2
Create Date: 2026-10-09 07:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d1e4f7a2b9"
down_revision: str | Sequence[str] | None = "b4e1f7a9c3d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _naive_timestamp_columns(*, timezone: bool) -> list[tuple[str, str]]:
    inspector = sa.inspect(op.get_bind())
    found: list[tuple[str, str]] = []
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        for column in inspector.get_columns(table):
            kind = column["type"]
            if isinstance(kind, sa.DateTime) and bool(getattr(kind, "timezone", False)) == timezone:
                found.append((table, column["name"]))
    return found


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, column in _naive_timestamp_columns(timezone=False):
        op.alter_column(
            table,
            column,
            type_=sa.DateTime(timezone=True),
            postgresql_using=f"\"{column}\" AT TIME ZONE 'UTC'",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, column in _naive_timestamp_columns(timezone=True):
        op.alter_column(
            table,
            column,
            type_=sa.DateTime(timezone=False),
            postgresql_using=f"\"{column}\" AT TIME ZONE 'UTC'",
        )
