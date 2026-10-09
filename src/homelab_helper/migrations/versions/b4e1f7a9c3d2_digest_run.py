"""digest run

Phase 8.6: one row per digest produced, so the next digest's window starts
where the last one stopped and delivery is auditable.

Revision ID: b4e1f7a9c3d2
Revises: a7d3e9c2b5f1
Create Date: 2026-10-08 09:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e1f7a9c3d2"
down_revision: str | Sequence[str] | None = "a7d3e9c2b5f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "digest_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("delivery", sa.String(length=32), nullable=False),
        sa.Column("delivery_detail", sa.Text(), nullable=True),
        sa.Column("quiet", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_digest_run_generated_at", "digest_run", ["generated_at"])
    op.create_index("ix_digest_run_window_start", "digest_run", ["window_start"])


def downgrade() -> None:
    op.drop_index("ix_digest_run_window_start", table_name="digest_run")
    op.drop_index("ix_digest_run_generated_at", table_name="digest_run")
    op.drop_table("digest_run")
