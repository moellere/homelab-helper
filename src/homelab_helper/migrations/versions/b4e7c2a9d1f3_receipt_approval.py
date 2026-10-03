"""receipt approval

Phase 7 slice 2: a receipt records who said yes at CONFIRM and over which
channel (the same facts the TrustHistory ``approval`` event carries), so
``list_receipts`` can answer "who approved this?" without a join.

Revision ID: b4e7c2a9d1f3
Revises: c1d8f4a6e2b7
Create Date: 2026-10-03 04:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e7c2a9d1f3"
down_revision: str | Sequence[str] | None = "c1d8f4a6e2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("execution_receipt") as batch:
        batch.add_column(sa.Column("approval", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("execution_receipt") as batch:
        batch.drop_column("approval")
