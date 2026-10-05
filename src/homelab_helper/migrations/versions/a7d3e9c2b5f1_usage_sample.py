"""usage sample

Phase 8.3: bounded usage history — hourly and daily rollups per host and guest,
backfilled from the hypervisor's round-robin data and pruned to a horizon.

Revision ID: a7d3e9c2b5f1
Revises: b4e7c2a9d1f3
Create Date: 2026-10-05 22:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d3e9c2b5f1"
down_revision: str | Sequence[str] | None = "b4e7c2a9d1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_sample",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_key", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("resolution", sa.String(length=8), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("cpu", sa.Float(), nullable=True),
        sa.Column("cpu_max", sa.Float(), nullable=True),
        sa.Column("cpus", sa.Float(), nullable=True),
        sa.Column("mem_used", sa.BigInteger(), nullable=True),
        sa.Column("mem_used_max", sa.BigInteger(), nullable=True),
        sa.Column("mem_total", sa.BigInteger(), nullable=True),
        sa.Column("disk_used", sa.BigInteger(), nullable=True),
        sa.Column("disk_total", sa.BigInteger(), nullable=True),
        sa.Column("net_in", sa.Float(), nullable=True),
        sa.Column("net_out", sa.Float(), nullable=True),
        sa.Column("disk_read", sa.Float(), nullable=True),
        sa.Column("disk_write", sa.Float(), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_type", "subject_key", "resolution", "ts", name="uq_usage_bucket"
        ),
    )
    op.create_index("ix_usage_sample_subject_type", "usage_sample", ["subject_type"])
    op.create_index("ix_usage_sample_subject_key", "usage_sample", ["subject_key"])
    op.create_index("ix_usage_sample_ts", "usage_sample", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_usage_sample_ts", table_name="usage_sample")
    op.drop_index("ix_usage_sample_subject_key", table_name="usage_sample")
    op.drop_index("ix_usage_sample_subject_type", table_name="usage_sample")
    op.drop_table("usage_sample")
