"""MaintenanceRun — one node's drain → update → reboot → return, as a sequence (Phase 10.4).

The run is a plan and a ledger, not an authority: every step is an ordinary
``ProposalLog`` that goes through ``execute_proposal`` and leaves an
``ExecutionReceipt``. The row records the order, which proposal each step is,
how far the run got, and why it stopped. One run per node at a time.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import MaintenanceStatus


class MaintenanceRun(Base):
    __tablename__ = "maintenance_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    node: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[MaintenanceStatus] = mapped_column(
        SAEnum(MaintenanceStatus), default=MaintenanceStatus.PLANNED, index=True
    )
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    """Ordered: ``{index, name, kind, proposal_id, receipt_id, state, detail}`` per step."""
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=now, index=True)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    notes: Mapped[str | None] = mapped_column(Text)
    """Why it stopped, when it did not finish."""


__all__ = ["MaintenanceRun"]
