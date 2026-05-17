"""Declarative ``Base`` and shared DB utilities.

Cross-cutting conventions for the harness DB (per ``harness-schema-slice1.md``):

- UUIDv7 primary keys everywhere. Sortable, no PG sequences, agents can mint
  IDs offline. We use the ``uuid6`` PyPI package's ``uuid7()`` until/unless
  CPython gains a stdlib equivalent.
- Timezone-aware UTC for every persisted timestamp.
- Append-only history for tables where lineage matters (Placement, Observation,
  AssertionRun, ProposalLog); status fields elsewhere. No soft delete.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import uuid6
from sqlalchemy.orm import DeclarativeBase

if TYPE_CHECKING:
    import uuid


class Base(DeclarativeBase):
    """Project-wide SQLAlchemy declarative base.

    Centralizing this here gives Alembic a single ``Base.metadata`` to autogenerate
    migrations against, and keeps per-model files focused on the model itself.
    """


def uuid7() -> uuid.UUID:
    """Return a UUIDv7 (sortable, timestamp-prefixed) via the ``uuid6`` package.

    Used as the default factory for every primary key in the harness DB.
    """
    return uuid6.uuid7()


def now() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``.

    SQLAlchemy stores naive datetimes silently; using this helper consistently
    keeps the boundary clean.
    """
    return datetime.now(UTC)


__all__ = ["Base", "now", "uuid7"]
