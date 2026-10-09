"""DigestRun — one row per digest the harness produced (Phase 8.6).

The row is what makes "one a week" checkable rather than aspirational: each
digest records the window it covered, so the next one starts where this one
stopped and no change is reported twice or missed entirely. It also records
whether the notification actually left the building, which keeps a digest the
operator never received distinguishable from one they ignored.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from homelab_helper.db.base import Base, now, uuid7


class DigestRun(Base):
    __tablename__ = "digest_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    generated_at: Mapped[datetime] = mapped_column(default=now, index=True)
    window_start: Mapped[datetime] = mapped_column(index=True)
    window_end: Mapped[datetime] = mapped_column()
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    """Headline numbers, so history reads without rebuilding each digest."""
    delivery: Mapped[str] = mapped_column(String(32), default="page")
    """page | sent | unconfigured | failed"""
    delivery_detail: Mapped[str | None] = mapped_column(Text)
    quiet: Mapped[bool] = mapped_column(Boolean, default=False)
    """Nothing happened in the window — recorded, usually not sent."""
