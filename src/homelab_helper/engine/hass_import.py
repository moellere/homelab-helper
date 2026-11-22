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

from homelab_helper.adapters.homeassistant import (
    extract_device_trackers,
    integrations_of_interest,
    summarize_states,
)
from homelab_helper.db.enums import DiscoverySource, PartKind, ResolutionScope
from homelab_helper.db.models import Host, PhysicalPart, Placement, Service, ServiceEndpoint

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
    device_trackers: int = 0
    matched_hosts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "service": self.service,
            "created": self.created,
            "version": self.version,
            "entities": self.entities,
            "domains": self.domains,
            "components": self.components,
            "integrations": list(self.integrations),
            "endpoint_hostname": self.endpoint_hostname,
        }
        if self.device_trackers:
            out["device_trackers"] = self.device_trackers
        if self.matched_hosts:
            out["matched_hosts"] = list(self.matched_hosts)
        return out


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


async def _correlate_device_trackers(
    session: AsyncSession, trackers: list[dict[str, Any]]
) -> list[str]:
    """Enrich known ``Host`` rows that match a Home Assistant ``device_tracker.*`` by IP, MAC, or hostname."""
    if not trackers:
        return []
    hosts = list((await session.execute(select(Host))).scalars().all())
    if not hosts:
        return []
    nic_rows = (
        await session.execute(
            select(Placement.host_id, PhysicalPart.serial)
            .join(PhysicalPart, PhysicalPart.id == Placement.part_id)
            .where(
                Placement.to_date.is_(None),
                PhysicalPart.kind == PartKind.NIC,
                PhysicalPart.serial.is_not(None),
            )
        )
    ).all()
    host_by_mac: dict[str, Host] = {}
    hosts_by_id = {h.id: h for h in hosts}
    for host_id, mac in nic_rows:
        if mac and host_id in hosts_by_id:
            host_by_mac[str(mac).lower()] = hosts_by_id[host_id]
    host_by_ip = {h.primary_ip: h for h in hosts if h.primary_ip}
    host_by_name = {h.hostname.lower(): h for h in hosts if h.hostname}

    matched: set[str] = set()
    for tracker in trackers:
        ip = tracker.get("ip")
        mac = tracker.get("mac")
        hname = (tracker.get("host_name") or "").lower()
        short_hname = hname.split(".", 1)[0] if hname else ""
        host = (
            (host_by_ip.get(ip) if ip else None)
            or (host_by_mac.get(mac) if mac else None)
            or (host_by_name.get(hname) if hname else None)
            or (host_by_name.get(short_hname) if short_hname else None)
        )
        if host is None:
            continue
        if host.primary_ip is None and ip:
            host.primary_ip = ip
        caps = dict(host.capabilities or {})
        caps["hass_device_tracker"] = {
            "entity_id": tracker.get("entity_id"),
            "state": tracker.get("state"),
            "ip": ip,
            "mac": mac,
            "host_name": tracker.get("host_name"),
        }
        host.capabilities = caps
        matched.add(host.hostname)
    return sorted(matched)


async def import_home_assistant(
    session: AsyncSession,
    *,
    config: dict[str, Any],
    states: list[dict[str, Any]],
    service_domains: list[str],
    url: str,
    when: datetime | None = None,
    service_name: str = SERVICE_NAME,
    resolver: str = RESOLVER,
) -> HassImportResult:
    """Upsert a Home Assistant ``Service`` and its endpoint from one read."""
    now = when or datetime.now(UTC)
    svc = (
        await session.execute(select(Service).where(Service.name == service_name))
    ).scalar_one_or_none()
    created = svc is None
    if svc is None:
        svc = Service(name=service_name)
        session.add(svc)
        await session.flush()

    components = [str(c) for c in config.get("components") or []]
    by_domain = summarize_states(states)
    integrations = integrations_of_interest(components)
    trackers = extract_device_trackers(states)
    matched_hosts = await _correlate_device_trackers(session, trackers)
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
    if trackers:
        attributes["device_trackers"] = trackers
    if matched_hosts:
        attributes["matched_hosts"] = matched_hosts
    svc.attributes = attributes  # JSON column: reassign, never mutate in place

    hostname, ip = _endpoint_identity(url)
    if hostname is not None:
        ep = (
            await session.execute(
                select(ServiceEndpoint).where(
                    ServiceEndpoint.service_id == svc.id,
                    ServiceEndpoint.scope == ResolutionScope.INTERNAL,
                    ServiceEndpoint.resolver == resolver,
                )
            )
        ).scalar_one_or_none()
        if ep is None:
            ep = ServiceEndpoint(
                service_id=svc.id,
                scope=ResolutionScope.INTERNAL,
                hostname=hostname,
                ip=ip,
                resolver=resolver,
                discovery_source=DiscoverySource.HOME_ASSISTANT,
            )
            session.add(ep)
        else:
            ep.hostname = hostname
            ep.ip = ip
        ep.attributes = {"url": url, "last_seen": now.isoformat()}

    await session.flush()
    return HassImportResult(
        service=service_name,
        created=created,
        version=config.get("version"),
        entities=len(states),
        domains=len(by_domain),
        components=len(components),
        integrations=integrations,
        endpoint_hostname=hostname,
        device_trackers=len(trackers),
        matched_hosts=matched_hosts,
    )


__all__ = ["RESOLVER", "SERVICE_NAME", "HassImportResult", "import_home_assistant"]
