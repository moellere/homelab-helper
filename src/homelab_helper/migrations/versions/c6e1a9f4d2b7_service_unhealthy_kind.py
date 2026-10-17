"""service-unhealthy finding kind

The print-warden source adds ``FindingKind.SERVICE_UNHEALTHY``; Postgres needs the enum value.
``db.enum_sync`` derives the statement from the models. No-op on SQLite.

Revision ID: c6e1a9f4d2b7
Revises: b2c8d4e9f1a3
Create Date: 2026-10-17 06:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from homelab_helper.db.enum_sync import apply_enum_sync

# revision identifiers, used by Alembic.
revision: str = "c6e1a9f4d2b7"
down_revision: str | Sequence[str] | None = "b2c8d4e9f1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    apply_enum_sync(op)


def downgrade() -> None:
    pass
