"""Placement — append-only history of where each part has lived.

Current placement = row with ``to_date IS NULL``. Closing out a placement is
a write to ``to_date``, never a row delete. The reconciler opens a new
placement only when slot or host changes; re-observing the same DIMM in the
same slot bumps ``last_verified`` on the part instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import Confidence, DiscoverySource

if TYPE_CHECKING:
    from .discovery import DiscoveryRun
    from .host import Host
    from .part import PhysicalPart


class Placement(Base):
    __tablename__ = "placement"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    part_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("physical_part.id"), index=True)
    host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("host.id"), index=True)

    # "DIMM_A1", "/dev/nvme0n1", "USB-port-3", "md0:member-2", ...
    slot: Mapped[str] = mapped_column(String(128))

    from_date: Mapped[datetime] = mapped_column(default=now)
    to_date: Mapped[datetime | None] = mapped_column(index=True)

    confidence: Mapped[Confidence] = mapped_column(SAEnum(Confidence), default=Confidence.VERIFIED)
    source: Mapped[DiscoverySource] = mapped_column(
        SAEnum(DiscoverySource), default=DiscoverySource.KERNEL_PROBE
    )
    discovery_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discovery_run.id"), index=True
    )

    notes: Mapped[str | None] = mapped_column(Text)

    part: Mapped[PhysicalPart] = relationship(back_populates="placements")
    host: Mapped[Host] = relationship(back_populates="placements")
    discovery_run: Mapped[DiscoveryRun | None] = relationship()

    __table_args__ = (
        # Filter ``to_date IS NULL`` is the hottest query — current placements.
        Index("ix_placement_current", "part_id", "to_date"),
        Index("ix_placement_host_current", "host_id", "to_date"),
    )
