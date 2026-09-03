"""normalize physical_part.wwid

Rewrites stored WWNs to the canonical bare-hex form so parts recorded by
different sources (lsblk's ``0x…`` vs Talos/COSI's ``naa.…``) resolve to one
identity. ``wwid`` is indexed but not unique, so collapsing distinct spellings
onto a shared value is safe here; the reconciler's forged-WWN guard re-keys the
resulting collisions by serial on the next run.

Downgrade cannot restore the original notation — the prefix is not recoverable
from the normalized value — so it is a no-op rather than a lie.

Revision ID: 7c1e4a90b3d2
Revises: 5372e3c99f7e
Create Date: 2026-07-31 13:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from homelab_helper.engine.reconciler import normalize_wwn

# revision identifiers, used by Alembic.
revision: str = "7c1e4a90b3d2"
down_revision: str | Sequence[str] | None = "5372e3c99f7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, wwid FROM physical_part WHERE wwid IS NOT NULL AND wwid != ''")
    ).fetchall()
    for row_id, wwid in rows:
        canonical = normalize_wwn(wwid)
        if canonical != wwid:
            bind.execute(
                sa.text("UPDATE physical_part SET wwid = :wwid WHERE id = :id"),
                {"wwid": canonical, "id": row_id},
            )


def downgrade() -> None:
    """No-op — the original WWN notation is not recoverable from bare hex."""
