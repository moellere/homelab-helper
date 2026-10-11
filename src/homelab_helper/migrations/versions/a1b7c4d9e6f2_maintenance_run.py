"""maintenance run

Phase 10.4: one row per node maintenance run (drain → update → reboot →
return), plus the enum values the models gained since the last sync.

Revision ID: a1b7c4d9e6f2
Revises: f4a9c3d7e2b5
Create Date: 2026-10-12 00:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from homelab_helper.db.enum_sync import enum_statements

# revision identifiers, used by Alembic.
revision: str = "a1b7c4d9e6f2"
down_revision: str | Sequence[str] | None = "f4a9c3d7e2b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("node", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PLANNED", "RUNNING", "SUCCEEDED", "FAILED", "ABORTED", name="maintenancestatus"
            ),
            nullable=False,
        ),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_maintenance_run_node", "maintenance_run", ["node"])
    op.create_index("ix_maintenance_run_status", "maintenance_run", ["status"])
    op.create_index("ix_maintenance_run_created_at", "maintenance_run", ["created_at"])
    if op.get_bind().dialect.name == "postgresql":
        for statement in enum_statements():
            op.execute(statement)


def downgrade() -> None:
    op.drop_index("ix_maintenance_run_created_at", table_name="maintenance_run")
    op.drop_index("ix_maintenance_run_status", table_name="maintenance_run")
    op.drop_index("ix_maintenance_run_node", table_name="maintenance_run")
    op.drop_table("maintenance_run")
