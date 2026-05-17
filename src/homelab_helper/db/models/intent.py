"""OperationalIntent — generic "ask once, remember forever" answers.

One current intent per ``(target_type, target_id)`` pair. Updating intent
overwrites; if you want history, query ``ProposalLog`` for intent-change
proposals.

The reconciler uses this to suppress findings (the bmax2 stopped-VMs case),
and to recognize the WAN2 ``STRAY`` case as "this was never real."
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import IntentState, IntentTargetType


class OperationalIntent(Base):
    __tablename__ = "operational_intent"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)

    target_type: Mapped[IntentTargetType] = mapped_column(SAEnum(IntentTargetType), index=True)
    # HOST: Host.id as UUID string.
    # VM/LXC: "<host_hostname>:<vmid>" or "<host_id>:<vmid>".
    # SERVICE: NetBox Service ID. CIRCUIT: NetBox Circuit ID.
    target_id: Mapped[str] = mapped_column(String(255), index=True)

    intent: Mapped[IntentState] = mapped_column(SAEnum(IntentState), default=IntentState.UNKNOWN)
    declared_at: Mapped[datetime] = mapped_column(default=now)
    declared_by: Mapped[str] = mapped_column(String(255))  # user identifier
    rationale: Mapped[str | None] = mapped_column(Text)

    # When intent should be re-asked. Null = never auto-expire.
    review_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("target_type", "target_id", name="uq_intent_per_target"),
        Index("ix_intent_target", "target_type", "target_id"),
    )
