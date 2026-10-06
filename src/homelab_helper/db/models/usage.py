"""UsageSample — bounded usage history (Phase 8.3).

One row per subject, resolution and time bucket: what a host or guest actually
used, as opposed to what it was allocated. Rows are rollups (``hour`` or
``day``) of the hypervisor's own round-robin data, upserted idempotently on
``(subject_type, subject_key, resolution, ts)`` and pruned to a retention
horizon per resolution, so the table never grows without bound.

``subject_key`` is the stable identity the source uses — a node name for a
host, ``<cluster>/<vmid>`` for a guest — so history follows a guest across
migrations and survives the harness re-creating a row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Float, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from homelab_helper.db.base import Base, uuid7


class UsageSample(Base):
    __tablename__ = "usage_sample"
    __table_args__ = (
        UniqueConstraint("subject_type", "subject_key", "resolution", "ts", name="uq_usage_bucket"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    subject_type: Mapped[str] = mapped_column(String(16), index=True)  # "host" | "guest"
    subject_key: Mapped[str] = mapped_column(String(255), index=True)
    label: Mapped[str | None] = mapped_column(String(255))
    resolution: Mapped[str] = mapped_column(String(8))  # "hour" | "day"
    ts: Mapped[datetime] = mapped_column(index=True)
    """Bucket start, UTC."""

    cpu: Mapped[float | None] = mapped_column(Float)
    """Mean CPU use as a fraction of the subject's allocated CPUs (0..1)."""
    cpu_max: Mapped[float | None] = mapped_column(Float)
    cpus: Mapped[float | None] = mapped_column(Float)
    mem_used: Mapped[int | None] = mapped_column(BigInteger)
    mem_used_max: Mapped[int | None] = mapped_column(BigInteger)
    mem_total: Mapped[int | None] = mapped_column(BigInteger)
    disk_used: Mapped[int | None] = mapped_column(BigInteger)
    disk_total: Mapped[int | None] = mapped_column(BigInteger)
    net_in: Mapped[float | None] = mapped_column(Float)
    """Bytes per second, bucket mean."""
    net_out: Mapped[float | None] = mapped_column(Float)
    disk_read: Mapped[float | None] = mapped_column(Float)
    disk_write: Mapped[float | None] = mapped_column(Float)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    """Source-specific means (iowait, pressure, load) without a column of their own."""
