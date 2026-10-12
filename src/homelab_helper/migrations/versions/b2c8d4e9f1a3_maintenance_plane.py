"""maintenance run plane

Phase 11.3: a maintenance run is either a Proxmox node's drain → update →
reboot → return or a Talos cluster's node-by-node upgrade; the row says which.

Revision ID: b2c8d4e9f1a3
Revises: a1b7c4d9e6f2
Create Date: 2026-10-12 03:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c8d4e9f1a3"
down_revision: str | Sequence[str] | None = "a1b7c4d9e6f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "maintenance_run",
        sa.Column("plane", sa.String(length=16), nullable=False, server_default="proxmox"),
    )


def downgrade() -> None:
    op.drop_column("maintenance_run", "plane")
