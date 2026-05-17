"""ConfigurationAssertion + AssertionRun.

The assertion library is the seed corpus of "things we know should be true."
The 16 assertions from the dorktool walkthrough are the starter pack;
the framework auto-runs verifiers on a schedule (P2) and on demand (P1).

AssertionRun is append-only; the latest row per assertion is the "current
status" — drift findings are generated on the pass→fail transition.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import (
    AssertionKind,
    AssertionScope,
    AssertionStatus,
    FindingSeverity,
)

if TYPE_CHECKING:
    from .finding import ReconciliationFinding


class ConfigurationAssertion(Base):
    __tablename__ = "configuration_assertion"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    # "covomv.br6.mac_pinned", "bmax-fleet.e1000e-offload-disabled"

    scope: Mapped[AssertionScope] = mapped_column(SAEnum(AssertionScope))
    scope_target: Mapped[str | None] = mapped_column(String(255))
    # For host scope: hostname. For cluster: cluster name. Null for global.

    description: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)

    kind: Mapped[AssertionKind] = mapped_column(SAEnum(AssertionKind))
    # Verifier specification; structure depends on kind.
    # ssh-command: {"command": "...", "expected_exit": 0, "expected_stdout_regex": "..."}
    # http-check: {"url": "...", "expected_status": 200, ...}
    # api-query: {"adapter": "proxmox", "query": {...}, "expected": {...}}
    # observation-predicate: {"key": "...", "predicate": "eq", "compare_to_key": "..."}
    # file-hash: {"path": "...", "expected_sha256": "..."}
    verifier_spec: Mapped[dict[str, Any]] = mapped_column(JSON)

    severity_on_fail: Mapped[FindingSeverity] = mapped_column(
        SAEnum(FindingSeverity), default=FindingSeverity.MEDIUM
    )

    schedule: Mapped[str | None] = mapped_column(String(64))  # cron or interval

    artifact_link: Mapped[str | None] = mapped_column(String(512))
    # Pointer to Ansible role, git path, runbook URL.

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    runs: Mapped[list[AssertionRun]] = relationship(back_populates="assertion")


class AssertionRun(Base):
    __tablename__ = "assertion_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    assertion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("configuration_assertion.id"), index=True
    )

    ran_at: Mapped[datetime] = mapped_column(default=now, index=True)
    status: Mapped[AssertionStatus] = mapped_column(SAEnum(AssertionStatus))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    finding_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reconciliation_finding.id"), index=True
    )

    assertion: Mapped[ConfigurationAssertion] = relationship(back_populates="runs")
    finding: Mapped[ReconciliationFinding | None] = relationship()

    __table_args__ = (Index("ix_assertion_run_latest", "assertion_id", "ran_at"),)
