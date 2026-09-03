"""service and service_endpoint

Revision ID: 5372e3c99f7e
Revises: b2f1a9c7d3e4
Create Date: 2026-07-12 15:57:55.229134

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5372e3c99f7e"
down_revision: str | Sequence[str] | None = "b2f1a9c7d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("netbox_service_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("service", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_service_name"), ["name"], unique=True)
        batch_op.create_index(
            batch_op.f("ix_service_netbox_service_id"), ["netbox_service_id"], unique=True
        )

    op.create_table(
        "service_endpoint",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.Enum("INTERNAL", "EXTERNAL", name="resolutionscope"), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("resolver", sa.String(length=32), nullable=False),
        sa.Column("tls_provider", sa.String(length=64), nullable=True),
        sa.Column(
            "discovery_source",
            sa.Enum(
                "MANUAL",
                "UNIFI",
                "PROXMOX",
                "K8S",
                "KERNEL_PROBE",
                "CLOUDFLARE",
                "NETBOX",
                name="discoverysource",
            ),
            nullable=False,
        ),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["service.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "service_id", "scope", "resolver", "hostname", name="uq_endpoint_identity"
        ),
    )
    with op.batch_alter_table("service_endpoint", schema=None) as batch_op:
        batch_op.create_index("ix_endpoint_scope_host", ["scope", "hostname"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_service_endpoint_hostname"), ["hostname"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_service_endpoint_scope"), ["scope"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_service_endpoint_service_id"), ["service_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("service_endpoint", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_service_endpoint_service_id"))
        batch_op.drop_index(batch_op.f("ix_service_endpoint_scope"))
        batch_op.drop_index(batch_op.f("ix_service_endpoint_hostname"))
        batch_op.drop_index("ix_endpoint_scope_host")

    op.drop_table("service_endpoint")
    with op.batch_alter_table("service", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_service_netbox_service_id"))
        batch_op.drop_index(batch_op.f("ix_service_name"))

    op.drop_table("service")
