"""ReconciliationFinding — the audit output.

Findings persist across reconciler runs; the same finding doesn't re-create
on every pass. Dedup is via ``fingerprint`` (deterministic hash). Re-runs
update ``last_seen``, refresh evidence, and bump description if it changed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus


class ReconciliationFinding(Base):
    __tablename__ = "reconciliation_finding"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)

    kind: Mapped[FindingKind] = mapped_column(SAEnum(FindingKind), index=True)
    severity: Mapped[FindingSeverity] = mapped_column(SAEnum(FindingSeverity), index=True)

    # Dedup key: same kind + same target + same root cause → same finding.
    # Computed via homelab_helper.engine.fingerprint.make_fingerprint.
    fingerprint: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text)

    # Pointers to the evidence that produced this finding:
    # [{"type": "observation", "id": "..."}, {"type": "assertion_run", "id": "..."}]
    evidence_refs: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)

    # Affected resources for blast-radius display:
    # [{"target_type": "host", "target_id": "node0"}, ...]
    affected: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)

    # Suggested fixes — L1 means we show but never run.
    proposed_actions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    status: Mapped[FindingStatus] = mapped_column(
        SAEnum(FindingStatus), default=FindingStatus.OPEN, index=True
    )
    first_seen: Mapped[datetime] = mapped_column(default=now, index=True)
    last_seen: Mapped[datetime] = mapped_column(default=now, index=True)

    acknowledged_at: Mapped[datetime | None] = mapped_column()
    acknowledged_by: Mapped[str | None] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column()
    suppressed_until: Mapped[datetime | None] = mapped_column()
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_finding_open", "status", "severity", "last_seen"),)
