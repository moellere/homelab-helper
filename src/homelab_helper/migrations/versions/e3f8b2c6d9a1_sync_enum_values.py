"""sync enum values

Every Python enum member missing from its Postgres type. The initial schema
created each type with the members of its day; eight finding kinds (and the
odd member elsewhere) arrived since without an ``ALTER TYPE``, so a Postgres
database upgraded by migrations could not store a ``version-drift`` finding.
``db.enum_sync`` derives the list from the models, so this migration — and
any later one that calls the same helper — never needs to know the names.
No-op on SQLite.

Revision ID: e3f8b2c6d9a1
Revises: d2e7f1a9b4c6
Create Date: 2026-10-11 18:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from homelab_helper.db.enum_sync import apply_enum_sync

# revision identifiers, used by Alembic.
revision: str = "e3f8b2c6d9a1"
down_revision: str | Sequence[str] | None = "d2e7f1a9b4c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    apply_enum_sync(op)


def downgrade() -> None:
    # Postgres cannot drop an enum value; rows carrying it would have to go first.
    pass
