"""PhysicalPart — first-class identity for parts that survive moves.

Serial-keyed where possible; identity-only where not. See the schema doc's
"reconciler needs a best-effort identity merge" note for the cp USB-SSD
case where serials are reused or garbage.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, BigInteger, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import PartKind

if TYPE_CHECKING:
    from .placement import Placement


class PhysicalPart(Base):
    __tablename__ = "physical_part"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    kind: Mapped[PartKind] = mapped_column(SAEnum(PartKind), index=True)

    manufacturer: Mapped[str | None] = mapped_column(String(255))
    model: Mapped[str | None] = mapped_column(String(255))
    serial: Mapped[str | None] = mapped_column(String(255), index=True)
    wwid: Mapped[str | None] = mapped_column(String(255), index=True)  # storage

    capacity_bytes: Mapped[int | None] = mapped_column(BigInteger)
    speed_mts: Mapped[int | None] = mapped_column(Integer)  # DIMM MT/s
    speed_mbps: Mapped[int | None] = mapped_column(Integer)  # storage / NIC

    # Open-ended part attributes (ECC, rank, NVMe gen, encoder generation, ...).
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    placements: Mapped[list[Placement]] = relationship(back_populates="part")

    __table_args__ = (
        # When serial is present it's globally unique within a kind — but
        # serials may be missing or reused for generic USB devices, so this
        # is an index, not a UniqueConstraint. The reconciler handles merges.
        Index("ix_part_kind_serial", "kind", "serial"),
        Index("ix_part_kind_wwid", "kind", "wwid"),
    )
