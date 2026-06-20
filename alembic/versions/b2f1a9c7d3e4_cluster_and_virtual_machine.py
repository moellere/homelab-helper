"""cluster and virtual_machine

Phase 3 virtualization slice: first-class Cluster + VirtualMachine tables for
what the management-plane adapters (Proxmox, K8s) discover.

Revision ID: b2f1a9c7d3e4
Revises: ad43bde457cc
Create Date: 2026-06-19 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2f1a9c7d3e4"
down_revision: str | Sequence[str] | None = "ad43bde457cc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DISCOVERY_SOURCE = sa.Enum(
    "MANUAL",
    "UNIFI",
    "PROXMOX",
    "K8S",
    "KERNEL_PROBE",
    "CLOUDFLARE",
    "NETBOX",
    name="discoverysource",
)


def upgrade() -> None:
    op.create_table(
        "cluster",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("netbox_cluster_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("discovery_source", _DISCOVERY_SOURCE, nullable=False),
        sa.Column("quorate", sa.Boolean(), nullable=True),
        sa.Column("node_count", sa.Integer(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("discovery_last_run", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("cluster", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_cluster_discovery_last_run"), ["discovery_last_run"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_cluster_name"), ["name"], unique=True)
        batch_op.create_index(
            batch_op.f("ix_cluster_netbox_cluster_id"), ["netbox_cluster_id"], unique=True
        )

    op.create_table(
        "virtual_machine",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("netbox_vm_id", sa.Integer(), nullable=True),
        sa.Column("cluster_id", sa.Uuid(), nullable=False),
        sa.Column("vmid", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("template", sa.Boolean(), nullable=False),
        sa.Column("node_name", sa.String(length=255), nullable=True),
        sa.Column("node_host_id", sa.Uuid(), nullable=True),
        sa.Column("vcpus", sa.Integer(), nullable=True),
        sa.Column("memory_bytes", sa.BigInteger(), nullable=True),
        sa.Column("disk_bytes", sa.BigInteger(), nullable=True),
        sa.Column("discovery_source", _DISCOVERY_SOURCE, nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["cluster.id"]),
        sa.ForeignKeyConstraint(["node_host_id"], ["host.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("virtual_machine", schema=None) as batch_op:
        batch_op.create_index("ix_vm_cluster_vmid", ["cluster_id", "vmid"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_virtual_machine_cluster_id"), ["cluster_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_virtual_machine_name"), ["name"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_virtual_machine_netbox_vm_id"), ["netbox_vm_id"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_virtual_machine_node_host_id"), ["node_host_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_virtual_machine_node_name"), ["node_name"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_virtual_machine_vmid"), ["vmid"], unique=False)


def downgrade() -> None:
    op.drop_table("virtual_machine")
    op.drop_table("cluster")
