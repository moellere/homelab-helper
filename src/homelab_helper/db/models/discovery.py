"""DiscoveryRun + Observation — every probe execution and the facts it produced.

DiscoveryRun records the act of running a probe (success/failure, timing,
target). Observation rows record the individual facts the probe produced.
Both are append-only.

Observations are not the inventory itself — they're evidence. The reconciler
reads observations and updates Host/Placement/NetBox accordingly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import Confidence, IntentTargetType, PrivilegeLevel

if TYPE_CHECKING:
    from .host import Host


class DiscoveryRun(Base):
    __tablename__ = "discovery_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)

    host_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("host.id"), index=True)
    # Null for network-wide scans (cold discovery against a CIDR).
    target_spec: Mapped[str | None] = mapped_column(String(255))
    # Network: "10.250.6.0/23". Host-scoped: same as host.primary_ip.

    probe_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("probe.id"), index=True)
    probe_name: Mapped[str] = mapped_column(String(255), index=True)
    probe_version: Mapped[str] = mapped_column(String(32))

    privilege_level: Mapped[PrivilegeLevel] = mapped_column(SAEnum(PrivilegeLevel))

    started_at: Mapped[datetime] = mapped_column(default=now, index=True)
    ended_at: Mapped[datetime | None] = mapped_column()
    success: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)

    observation_count: Mapped[int] = mapped_column(Integer, default=0)

    triggered_by: Mapped[str] = mapped_column(String(64), default="scheduled")
    # "scheduled" | "manual" | "agent" | "first-contact" | "reconciler"

    host: Mapped[Host | None] = relationship(back_populates="discovery_runs")
    observations: Mapped[list[Observation]] = relationship(back_populates="run")


class Observation(Base):
    __tablename__ = "observation"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("discovery_run.id"), index=True)

    # Dotted key path: "host.cpu.model", "host.memory.dimms",
    # "host.storage.devices[0].smart.healthy", "network.subnet.live_hosts"
    key: Mapped[str] = mapped_column(String(255), index=True)

    # JSON to accept anything: scalar, list, dict.
    value: Mapped[Any] = mapped_column(JSON)

    confidence: Mapped[Confidence] = mapped_column(SAEnum(Confidence), default=Confidence.VERIFIED)
    recorded_at: Mapped[datetime] = mapped_column(default=now, index=True)

    # Target this observation describes; differs from run.host_id for network scans.
    target_type: Mapped[IntentTargetType | None] = mapped_column(
        SAEnum(IntentTargetType), index=True
    )
    target_id: Mapped[str | None] = mapped_column(String(255), index=True)

    run: Mapped[DiscoveryRun] = relationship(back_populates="observations")

    __table_args__ = (
        # "latest observations of key K for target T"
        Index("ix_obs_target_key_time", "target_type", "target_id", "key", "recorded_at"),
        Index("ix_obs_key_time", "key", "recorded_at"),
    )
