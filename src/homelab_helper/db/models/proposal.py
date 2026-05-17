"""ProposalLog — L1's accountability record.

Every proposed change, what the user did with it. Becomes the audit log if/when
L2 lands. The ``artifact`` JSON is shaped exactly like a future L2 action
manifest — kind, content, rollback, blast radius — so L2 is a wire-up, not a
re-architecture.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import ProposalOutcome


class ProposalLog(Base):
    __tablename__ = "proposal_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)

    proposed_at: Mapped[datetime] = mapped_column(default=now, index=True)
    proposed_by: Mapped[str] = mapped_column(String(64), default="reconciler")
    # "reconciler" | "agent:planner" | "agent:incident-triage" | ...

    finding_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reconciliation_finding.id"), index=True
    )
    assertion_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("configuration_assertion.id"), index=True
    )

    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text)

    # L2-ready artifact shape; today nothing executes it.
    # {"kind": "ansible-playbook", "content": "...", "rollback": "..."}
    # {"kind": "shell-command", "command": "...", "rollback_command": "..."}
    # {"kind": "netbox-change", "object_type": "device", "patch": {...}}
    artifact: Mapped[dict[str, Any]] = mapped_column(JSON)

    affected: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    blast_radius: Mapped[str] = mapped_column(String(64), default="unknown")
    # "single-host" | "cluster" | "site" | "everything" | "metadata-only"

    outcome: Mapped[ProposalOutcome] = mapped_column(
        SAEnum(ProposalOutcome), default=ProposalOutcome.PENDING
    )
    outcome_at: Mapped[datetime | None] = mapped_column()
    outcome_by: Mapped[str | None] = mapped_column(String(255))
    outcome_notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_proposal_pending", "outcome", "proposed_at"),)
