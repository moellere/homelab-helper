"""Trust gradient tables (Phase 6) — the deterministic authorization model.

Shapes follow the forward spec in ``docs/harness-schema-slice1.md`` ("Trust
gradient tables"). Two trust concepts, not to be conflated: trust *boundaries*
are information-flow (who gets secrets); the trust *gradient* is action
authority (how much the framework may change without asking). ``decide()``
in ``engine/trust.py`` is the only consumer — and the only gate every adapter
write path will route through.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import AutonomyLevel, TrustDomain


class Domain(Base):
    """Taxonomy + policy defaults. Seeded at db init; edited rarely."""

    __tablename__ = "trust_domain"

    name: Mapped[TrustDomain] = mapped_column(SAEnum(TrustDomain), primary_key=True)
    default_level: Mapped[AutonomyLevel] = mapped_column(
        SAEnum(AutonomyLevel), default=AutonomyLevel.PROPOSE
    )
    max_level: Mapped[AutonomyLevel] = mapped_column(
        SAEnum(AutonomyLevel), default=AutonomyLevel.AUTONOMOUS
    )
    is_absolute: Mapped[bool] = mapped_column(Boolean, default=False)
    """True ⇒ no override and no elevation window crosses this domain's
    floors; only a policy-config edit changes it. SECRETS ships True."""


class CellTrust(Base):
    """The moving floor, per (domain x action-kind x blast-radius)."""

    __tablename__ = "cell_trust"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    domain: Mapped[TrustDomain] = mapped_column(SAEnum(TrustDomain), index=True)
    action_kind: Mapped[str] = mapped_column(String(64))
    blast_radius: Mapped[str] = mapped_column(String(64))
    level: Mapped[AutonomyLevel] = mapped_column(
        SAEnum(AutonomyLevel), default=AutonomyLevel.PROPOSE
    )
    granted_by: Mapped[str | None] = mapped_column(String(255))
    """None ⇒ level reached by auto-escalation, not an explicit grant."""
    clean_streak: Mapped[int] = mapped_column(Integer, default=0)
    on_probation: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    __table_args__ = (UniqueConstraint("domain", "action_kind", "blast_radius", name="uq_cell"),)


class TrustBoundary(Base):
    """Per-host authority ceiling; ``absolute`` makes it window-proof."""

    __tablename__ = "trust_boundary"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    host_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("host.id"), index=True)
    netbox_device_id: Mapped[int | None] = mapped_column(Integer, index=True)
    max_agent_authority: Mapped[AutonomyLevel] = mapped_column(
        SAEnum(AutonomyLevel), default=AutonomyLevel.CONFIRM
    )
    absolute: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)


class ElevationWindow(Base):
    """Time-boxed lift of the soft-hard floors: scoped, expiring, killable."""

    __tablename__ = "elevation_window"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    opened_by: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON)
    """{"domains": [...], "hosts": [...], "cells": ["domain/kind/blast", ...]}"""
    opened_at: Mapped[datetime] = mapped_column(default=now, index=True)
    expires_at: Mapped[datetime] = mapped_column(index=True)
    revoked_at: Mapped[datetime | None] = mapped_column()
    revoked_by: Mapped[str | None] = mapped_column(String(255))


class TrustHistory(Base):
    """Append-only audit spine: every authority change of any kind."""

    __tablename__ = "trust_history"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=now, index=True)
    actor: Mapped[str] = mapped_column(String(255))
    event: Mapped[str] = mapped_column(String(64))
    """grant | auto-promote | demote | override | window-open | window-revoke"""
    domain: Mapped[TrustDomain | None] = mapped_column(SAEnum(TrustDomain))
    cell_trust_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cell_trust.id"))
    window_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("elevation_window.id"))
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("proposal_log.id"))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
