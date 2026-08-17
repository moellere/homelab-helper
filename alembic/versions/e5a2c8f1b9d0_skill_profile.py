"""skill_profile table

Per-domain operator proficiency for the Phase-4 Skill Inferer. One row per
domain; ``source`` distinguishes operator-pinned (manual) rows from passively
inferred ones — inference never overwrites manual.

Revision ID: e5a2c8f1b9d0
Revises: 7c1e4a90b3d2
Create Date: 2026-08-17 19:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5a2c8f1b9d0"
down_revision: str | Sequence[str] | None = "7c1e4a90b3d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_profile",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column(
            "level",
            sa.Enum("NOVICE", "BASIC", "INTERMEDIATE", "ADVANCED", name="skilllevel"),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.Enum("INFERRED", "MANUAL", name="skillsource"),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_skill_profile_domain", "skill_profile", ["domain"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_skill_profile_domain", table_name="skill_profile")
    op.drop_table("skill_profile")
