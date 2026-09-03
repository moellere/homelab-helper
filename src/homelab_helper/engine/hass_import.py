"""Persist a Home Assistant read into the harness DB (read-only source).

One ``Service`` row named ``home-assistant`` carries the hub's facts in its
``attributes`` (version, location, loaded components, the integrations of
interest, entity counts by domain, service domains), and one internal
``ServiceEndpoint`` records where the hub answers. Re-runs update the same
rows; nothing else in the DB is touched, so the import is idempotent and
never reaps operator data.

The service name is fixed rather than derived from the URL's DNS label, so
``get_service("home-assistant")`` is stable; merging it with the ``ha`` service
that UniFi/Cloudflare endpoints produce is the explicit alias map's job (see
the backlog).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from sqlalchemy import select

from homelab_helper.adapters.homeassistant import integrations_of_interest, summarize_states
from homelab_helper.db.enums import DiscoverySource, ResolutionScope
from homelab_helper.db.models import Service, ServiceEndpoint

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SERVICE_NAME = "home-assistant"
RESOLVER = "home-assistant"


@dataclass
class HassImportResult:
    service: str
    created: bool
    version: str | None
    entities: int
    domains: int
    components: int
    integrations: list[str] = field(default_factory=list)
    endpoint_hostname: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "created": self.created,
            "version": self.version,
            "entities": self.entities,
            "domains": self.domains,
            "components": self.components,
            "integrations": list(self.integrations),
            "endpoint_hostname": self.endpoint_hostname,
        }


def _endpoint_identity(url: str) -> tuple[str | None, str | None]:
    """(hostname, ip) for the endpoint row: an IP-literal URL fills both."""
    hostname = urlparse(url).hostname
    if not hostname:
        return None, None
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return hostname.lower(), None
    return hostname, hostname


async def import_home_assistant(
    session: AsyncSession,
    *,
    config: dict[str, Any],
    states: list[dict[str, Any]],
    service_domains: list[str],
    url: str,
    when: datetime | None = None,
) -> HassImportResult:
    """Upsert the ``home-assistant`` Service and its endpoint from one read."""
    now = when or datetime.now(UTC)
    svc = (
        await session.execute(select(Service).where(Service.name == SERVICE_NAME))
    ).scalar_one_or_none()
    created = svc is None
    if svc is None:
        svc = Service(name=SERVICE_NAME)
        session.add(svc)
        await session.flush()

    components = [str(c) for c in config.get("components") or []]
    by_domain = summarize_states(states)
    integrations = integrations_of_interest(components)
    attributes = dict(svc.attributes or {})
    attributes.update(
        {
            "kind": SERVICE_NAME,
            "version": config.get("version"),
            "location_name": config.get("location_name"),
            "time_zone": config.get("time_zone"),
            "components": components,
            "integrations": integrations,
            "entity_count": len(states),
            "entities_by_domain": by_domain,
            "service_domains": list(service_domains),
            "url": url,
            "last_seen": now.isoformat(),
        }
    )
    svc.attributes = attributes  # JSON column: reassign, never mutate in place

    hostname, ip = _endpoint_identity(url)
    if hostname is not None:
        ep = (
            await session.execute(
                select(ServiceEndpoint).where(
                    ServiceEndpoint.service_id == svc.id,
                    ServiceEndpoint.scope == ResolutionScope.INTERNAL,
                    ServiceEndpoint.resolver == RESOLVER,
                )
            )
        ).scalar_one_or_none()
        if ep is None:
            ep = ServiceEndpoint(
                service_id=svc.id,
                scope=ResolutionScope.INTERNAL,
                hostname=hostname,
                ip=ip,
                resolver=RESOLVER,
                discovery_source=DiscoverySource.HOME_ASSISTANT,
            )
            session.add(ep)
        else:
            ep.hostname = hostname
            ep.ip = ip
        ep.attributes = {"url": url, "last_seen": now.isoformat()}

    await session.flush()
    return HassImportResult(
        service=SERVICE_NAME,
        created=created,
        version=config.get("version"),
        entities=len(states),
        domains=len(by_domain),
        components=len(components),
        integrations=integrations,
        endpoint_hostname=hostname,
    )


__all__ = ["RESOLVER", "SERVICE_NAME", "HassImportResult", "import_home_assistant"]
