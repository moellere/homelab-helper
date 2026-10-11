"""anomaly finding kind

Phase 10.3 adds ``FindingKind.ANOMALY``; Postgres needs the enum value.
``db.enum_sync`` derives the statement from the models, so this migration is
the one line every new enum member needs. No-op on SQLite.

Revision ID: f4a9c3d7e2b5
Revises: e3f8b2c6d9a1
Create Date: 2026-10-11 22:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from homelab_helper.db.enum_sync import apply_enum_sync

# revision identifiers, used by Alembic.
revision: str = "f4a9c3d7e2b5"
down_revision: str | Sequence[str] | None = "e3f8b2c6d9a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    apply_enum_sync(op)


def downgrade() -> None:
    pass
