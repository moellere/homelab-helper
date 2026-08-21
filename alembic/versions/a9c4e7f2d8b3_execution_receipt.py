"""execution receipt

Phase 6 PR B: one row per executed (or failed) action. The execution half of
the audit spine — TrustHistory records authority changes, ExecutionReceipt
records what actually ran, at what decided level, and how it ended.

Revision ID: a9c4e7f2d8b3
Revises: f7b3d9a2c4e1
Create Date: 2026-08-21 00:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9c4e7f2d8b3"
down_revision: str | Sequence[str] | None = "f7b3d9a2c4e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEVEL = sa.Enum("BLOCK", "PROPOSE", "CONFIRM", "AUTONOMOUS", name="autonomylevel")


def upgrade() -> None:
    op.create_table(
        "execution_receipt",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("proposal_id", sa.Uuid(), sa.ForeignKey("proposal_log.id"), nullable=False),
        sa.Column("executed_at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("decision_level", _LEVEL, nullable=False),
        sa.Column("decision_reasons", sa.JSON(), nullable=False),
        sa.Column("window_id", sa.Uuid(), sa.ForeignKey("elevation_window.id"), nullable=True),
        sa.Column("action", sa.JSON(), nullable=False),
        sa.Column("rollback_state", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_execution_receipt_proposal_id", "execution_receipt", ["proposal_id"])
    op.create_index("ix_execution_receipt_executed_at", "execution_receipt", ["executed_at"])


def downgrade() -> None:
    op.drop_index("ix_execution_receipt_executed_at", table_name="execution_receipt")
    op.drop_index("ix_execution_receipt_proposal_id", table_name="execution_receipt")
    op.drop_table("execution_receipt")
