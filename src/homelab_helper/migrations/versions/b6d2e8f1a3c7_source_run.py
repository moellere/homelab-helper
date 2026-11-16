"""source_run table

One row per management-plane discovery run, so ``schedule.yaml`` can give each
source its own cadence (``sources:``) the way it gives probes and assertions theirs.

Revision ID: b6d2e8f1a3c7
Revises: a4f8c2e6d1b3
Create Date: 2026-11-16 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6d2e8f1a3c7"
down_revision: str | Sequence[str] | None = "a4f8c2e6d1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_source_run_latest", "source_run", ["source", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_source_run_latest", table_name="source_run")
    op.drop_table("source_run")
