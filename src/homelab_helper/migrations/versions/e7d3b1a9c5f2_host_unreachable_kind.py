"""host-unreachable finding kind, jetkvm discovery source

The JetKVM source adds ``FindingKind.HOST_UNREACHABLE`` and ``DiscoverySource.JETKVM``; Postgres needs the enum
values. ``db.enum_sync`` derives the statements from the models. No-op on SQLite.

Revision ID: e7d3b1a9c5f2
Revises: c6e1a9f4d2b7
Create Date: 2026-10-23 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from homelab_helper.db.enum_sync import apply_enum_sync

# revision identifiers, used by Alembic.
revision: str = "e7d3b1a9c5f2"
down_revision: str | Sequence[str] | None = "c6e1a9f4d2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    apply_enum_sync(op)


def downgrade() -> None:
    pass
