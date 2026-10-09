"""no-new-guests intent

Phase 9.8: a host-level ``OperationalIntent`` that keeps the rebalance and
placement planners from sending guests to a host the operator wants kept
light. SQLite stores the enum as text; Postgres needs the value added.

Revision ID: d2e7f1a9b4c6
Revises: c8d1e4f7a2b9
Create Date: 2026-10-09 16:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e7f1a9b4c6"
down_revision: str | Sequence[str] | None = "c8d1e4f7a2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE intentstate ADD VALUE IF NOT EXISTS 'NO_NEW_GUESTS'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; rows carrying it would have to go first.
    pass
