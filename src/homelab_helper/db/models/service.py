"""Service + ServiceEndpoint models — DNS/service resolution (Phase 3).

A ``Service`` is the harness's logical identity for something that resolves by
name (``home-assistant``, ``nas``). NetBox's Service becomes a thin pointer to
it via ``netbox_service_id``; the harness owns the interesting part — the set
of ``ServiceEndpoint`` rows that capture *how a name resolves on each side of
the network*. Internal (UniFi) and external (Cloudflare) resolvers commonly map
the same hostname to different IPs; that DNS split-brain is exactly what a
single-valued ``IPAddress.dns_name`` can't express, so the harness models it.

Each Service has 0..N endpoints, keyed within a service by
``(scope, resolver, hostname)`` so a re-sync from a DNS source updates in place
rather than duplicating.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import DiscoverySource, ResolutionScope


class Service(Base):
    """A logical, name-resolvable service the harness tracks endpoints for."""

    __tablename__ = "service"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    netbox_service_id: Mapped[int | None] = mapped_column(unique=True, index=True)

    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    endpoints: Mapped[list[ServiceEndpoint]] = relationship(
        back_populates="service", cascade="all, delete-orphan"
    )


class ServiceEndpoint(Base):
    """One resolution path for a Service: a (scope, resolver, hostname) → ip."""

    __tablename__ = "service_endpoint"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("service.id"), index=True)

    scope: Mapped[ResolutionScope] = mapped_column(SAEnum(ResolutionScope), index=True)
    hostname: Mapped[str] = mapped_column(String(255), index=True)
    ip: Mapped[str | None] = mapped_column(String(45))
    resolver: Mapped[str] = mapped_column(String(32))  # "unifi" | "cloudflare" | "consul" | ...
    tls_provider: Mapped[str | None] = mapped_column(String(64))

    discovery_source: Mapped[DiscoverySource] = mapped_column(
        SAEnum(DiscoverySource), default=DiscoverySource.UNIFI
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    service: Mapped[Service] = relationship(back_populates="endpoints")

    __table_args__ = (
        UniqueConstraint(
            "service_id", "scope", "resolver", "hostname", name="uq_endpoint_identity"
        ),
        Index("ix_endpoint_scope_host", "scope", "hostname"),
    )


__all__ = ["Service", "ServiceEndpoint"]
