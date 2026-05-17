"""Probe — plugin registry row.

The DB row is "what's available right now"; the module on disk is the source
of behavior. Probes register on framework startup via Python entry points
(``homelab_helper.probes``); ``helper probes register`` re-syncs manually
during development.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import PrivilegeLevel


class Probe(Base):
    __tablename__ = "probe"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # "kernel.cpu", "kernel.memory.dmidecode", "network.subnet-scan",
    # "proxmox.cluster", "unifi.controller"

    version: Mapped[str] = mapped_column(String(32))
    module_path: Mapped[str] = mapped_column(String(512))
    # e.g. "homelab_helper.probes.kernel.cpu:CPUProbe"

    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    # Bumps only when probe OUTPUT shape changes.

    required_privilege: Mapped[PrivilegeLevel] = mapped_column(
        SAEnum(PrivilegeLevel), default=PrivilegeLevel.USER
    )

    # Observation keys this probe can produce — for capability matching.
    produces_keys: Mapped[list[str]] = mapped_column(JSON, default=list)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)
