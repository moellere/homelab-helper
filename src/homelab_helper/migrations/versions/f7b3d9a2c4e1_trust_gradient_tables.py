"""trust gradient tables

Phase 6 authorization model: Domain (taxonomy + policy defaults), CellTrust
(the moving floor), TrustBoundary (per-host ceilings), ElevationWindow
(time-boxed floor lifts), TrustHistory (append-only audit spine). Shapes per
the forward spec in docs/harness-schema-slice1.md.

Revision ID: f7b3d9a2c4e1
Revises: e5a2c8f1b9d0
Create Date: 2026-08-20 23:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7b3d9a2c4e1"
down_revision: str | Sequence[str] | None = "e5a2c8f1b9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEVEL = sa.Enum("BLOCK", "PROPOSE", "CONFIRM", "AUTONOMOUS", name="autonomylevel")
_DOMAIN = sa.Enum(
    "INVENTORY_METADATA",
    "CONTAINERS",
    "DNS",
    "NETWORK_FABRIC",
    "STORAGE",
    "HYPERVISOR",
    "HOST_OS",
    "SECRETS",
    name="trustdomain",
)


def upgrade() -> None:
    op.create_table(
        "trust_domain",
        sa.Column("name", _DOMAIN, primary_key=True),
        sa.Column("default_level", _LEVEL, nullable=False),
        sa.Column("max_level", _LEVEL, nullable=False),
        sa.Column("is_absolute", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "cell_trust",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("domain", _DOMAIN, nullable=False),
        sa.Column("action_kind", sa.String(length=64), nullable=False),
        sa.Column("blast_radius", sa.String(length=64), nullable=False),
        sa.Column("level", _LEVEL, nullable=False),
        sa.Column("granted_by", sa.String(length=255), nullable=True),
        sa.Column("clean_streak", sa.Integer(), nullable=False),
        sa.Column("on_probation", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("domain", "action_kind", "blast_radius", name="uq_cell"),
    )
    op.create_index("ix_cell_trust_domain", "cell_trust", ["domain"])
    op.create_table(
        "trust_boundary",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("host_id", sa.Uuid(), sa.ForeignKey("host.id"), nullable=True),
        sa.Column("netbox_device_id", sa.Integer(), nullable=True),
        sa.Column("max_agent_authority", _LEVEL, nullable=False),
        sa.Column("absolute", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_trust_boundary_host_id", "trust_boundary", ["host_id"])
    op.create_index("ix_trust_boundary_netbox_device_id", "trust_boundary", ["netbox_device_id"])
    op.create_table(
        "elevation_window",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("opened_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(length=255), nullable=True),
    )
    op.create_index("ix_elevation_window_opened_at", "elevation_window", ["opened_at"])
    op.create_index("ix_elevation_window_expires_at", "elevation_window", ["expires_at"])
    op.create_table(
        "trust_history",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("domain", _DOMAIN, nullable=True),
        sa.Column("cell_trust_id", sa.Uuid(), sa.ForeignKey("cell_trust.id"), nullable=True),
        sa.Column("window_id", sa.Uuid(), sa.ForeignKey("elevation_window.id"), nullable=True),
        sa.Column("proposal_id", sa.Uuid(), sa.ForeignKey("proposal_log.id"), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
    )
    op.create_index("ix_trust_history_at", "trust_history", ["at"])


def downgrade() -> None:
    op.drop_index("ix_trust_history_at", table_name="trust_history")
    op.drop_table("trust_history")
    op.drop_index("ix_elevation_window_expires_at", table_name="elevation_window")
    op.drop_index("ix_elevation_window_opened_at", table_name="elevation_window")
    op.drop_table("elevation_window")
    op.drop_index("ix_trust_boundary_netbox_device_id", table_name="trust_boundary")
    op.drop_index("ix_trust_boundary_host_id", table_name="trust_boundary")
    op.drop_table("trust_boundary")
    op.drop_index("ix_cell_trust_domain", table_name="cell_trust")
    op.drop_table("cell_trust")
    op.drop_table("trust_domain")
