"""receipt rollback linkage

Phase 6 PR D: an executed action that was later undone points at the receipt
recording the undo. Receipts stay append-only — the original is linked, never
rewritten.

Revision ID: c1d8f4a6e2b7
Revises: a9c4e7f2d8b3
Create Date: 2026-09-03 19:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d8f4a6e2b7"
down_revision: str | Sequence[str] | None = "a9c4e7f2d8b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("execution_receipt") as batch:
        batch.add_column(sa.Column("rolled_back_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("rollback_receipt_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_execution_receipt_rollback",
            "execution_receipt",
            ["rollback_receipt_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("execution_receipt") as batch:
        batch.drop_constraint("fk_execution_receipt_rollback", type_="foreignkey")
        batch.drop_column("rollback_receipt_id")
        batch.drop_column("rolled_back_at")
