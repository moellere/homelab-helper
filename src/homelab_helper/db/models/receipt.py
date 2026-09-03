"""ExecutionReceipt — one row per executed (or failed) action (Phase 6).

The receipt is the execution half of the audit spine (``TrustHistory`` is the
authority half). It captures what ran, at what decided level, why (the full
reason trace from ``decide()``), under which window if any, the rollback state
captured *before* dispatch, and how it ended. Append-only — a failed action
gets a receipt too; absence of a receipt means nothing executed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import AutonomyLevel


class ExecutionReceipt(Base):
    __tablename__ = "execution_receipt"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    proposal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("proposal_log.id"), index=True)
    executed_at: Mapped[datetime] = mapped_column(default=now, index=True)
    actor: Mapped[str] = mapped_column(String(255))

    decision_level: Mapped[AutonomyLevel] = mapped_column(SAEnum(AutonomyLevel))
    decision_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    window_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("elevation_window.id"))

    action: Mapped[dict[str, Any]] = mapped_column(JSON)
    rollback_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    outcome: Mapped[str] = mapped_column(String(32))  # "succeeded" | "failed"
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
