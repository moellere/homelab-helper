"""service_endpoint: CNAME target + record_type

Endpoints could only record an IP, so CNAME records were dropped at import.
A tunnel-published service has no public address record — its external name
aliases ``<uuid>.cfargotunnel.com`` — which meant the services most worth
comparing across the internal/external boundary were invisible to split-brain
detection.

Both columns are nullable and backfill-free: existing rows are address records,
so ``target`` stays NULL, and ``record_type`` is left NULL rather than guessed.
The next reconcile from each source stamps ``record_type`` in place.

Revision ID: 9b3c5d71e0a4
Revises: 7c1e4a90b3d2
Create Date: 2026-08-01 16:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b3c5d71e0a4"
down_revision: str | Sequence[str] | None = "7c1e4a90b3d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("service_endpoint", schema=None) as batch_op:
        batch_op.add_column(sa.Column("target", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("record_type", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("service_endpoint", schema=None) as batch_op:
        batch_op.drop_column("record_type")
        batch_op.drop_column("target")
