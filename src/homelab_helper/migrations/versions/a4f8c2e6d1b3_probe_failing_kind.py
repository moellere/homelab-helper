"""probe-failing finding kind

``FindingKind.PROBE_FAILING`` needs its Postgres enum value. ``db.enum_sync`` derives the statements from the
models. No-op on SQLite.

Revision ID: a4f8c2e6d1b3
Revises: e7d3b1a9c5f2
Create Date: 2026-11-06 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from homelab_helper.db.enum_sync import apply_enum_sync

# revision identifiers, used by Alembic.
revision: str = "a4f8c2e6d1b3"
down_revision: str | Sequence[str] | None = "e7d3b1a9c5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    apply_enum_sync(op)


def downgrade() -> None:
    pass
