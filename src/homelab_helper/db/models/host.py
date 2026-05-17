"""Host model — harness-side projection of a NetBox Device.

NetBox is canonical for the inventory facts it natively models (model, location,
primary_ip, status). This row owns harness-only fields: capabilities, discovery
state, power policy, and the link to a credentials_ref in the secrets store.

See ``harness-schema-slice1.md`` § Host for the design notes — especially
the distinction between ``last_verified`` (operator-facing) and
``discovery_last_run`` (system-facing).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import Architecture, DiscoverySource, PowerPolicy, PowerState

if TYPE_CHECKING:
    from .discovery import DiscoveryRun
    from .placement import Placement


class Host(Base):
    __tablename__ = "host"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    netbox_device_id: Mapped[int | None] = mapped_column(unique=True, index=True)

    hostname: Mapped[str] = mapped_column(String(255), index=True)
    primary_ip: Mapped[str | None] = mapped_column(String(45))
    arch: Mapped[Architecture] = mapped_column(SAEnum(Architecture), default=Architecture.OTHER)

    power_policy: Mapped[PowerPolicy] = mapped_column(
        SAEnum(PowerPolicy), default=PowerPolicy.ALWAYS_ON
    )
    expected_power_state: Mapped[PowerState] = mapped_column(
        SAEnum(PowerState), default=PowerState.ON
    )

    discovery_source: Mapped[DiscoverySource] = mapped_column(
        SAEnum(DiscoverySource), default=DiscoverySource.MANUAL
    )
    discovery_last_run: Mapped[datetime | None] = mapped_column(index=True)
    last_verified: Mapped[datetime | None] = mapped_column()

    # Capability bag — open during MVP; promote hot keys to typed columns later.
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Opaque reference into the secrets store; NEVER store credentials here.
    credentials_ref: Mapped[str | None] = mapped_column(String(255))

    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    placements: Mapped[list[Placement]] = relationship(back_populates="host")
    discovery_runs: Mapped[list[DiscoveryRun]] = relationship(back_populates="host")

    __table_args__ = (Index("ix_host_hostname_lower", "hostname"),)
