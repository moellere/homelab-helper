"""SourceRun — one row per management-plane discovery a source ran.

Probes leave a ``DiscoveryRun`` per host and probe; a source (Proxmox, Argo CD,
the usage rollups, ...) left nothing, so a schedule could not tell when one
last ran. This row is that record: the daemon reads the newest per source to
honour per-source cadences, and a failed run is kept with its error.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from homelab_helper.db.base import Base, now, uuid7


class SourceRun(Base):
    __tablename__ = "source_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    source: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(default=now)
    ended_at: Mapped[datetime | None] = mapped_column()
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    """Redacted; set when ``ok`` is false."""

    __table_args__ = (Index("ix_source_run_latest", "source", "started_at"),)
