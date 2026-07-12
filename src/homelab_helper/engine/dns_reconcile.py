"""DNS reconcile — resolution records → Service + ServiceEndpoint rows.

Projects a DNS source's A/AAAA records onto the harness's Service model. Each
record's hostname becomes a ``Service`` (created on demand) with one
``ServiceEndpoint`` at the given scope, tagged with the resolver that produced
it (``unifi`` internally, ``cloudflare`` externally). The pair
(service, scope, resolver, hostname) is the idempotency key, so a re-sync
updates the IP in place.

Scope discipline: a reconcile only ever creates/updates/removes endpoints for
the exact ``(scope, resolver)`` pair it was called for. Endpoints from any other
scope/resolver are invisible to it — so the DNS-split-brain pairing of
internal-vs-external for one hostname is preserved across independent syncs
(an internal UniFi sync never touches an external Cloudflare endpoint, and vice
versa).

Only ``A`` / ``AAAA`` records map to endpoints — a CNAME/TXT carries no IP.
Records the source no longer reports are removed (the operator deleted them);
empty Services left behind by the last removal are cleaned up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import DiscoverySource, ResolutionScope
from homelab_helper.db.models import Service, ServiceEndpoint

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

_ADDRESS_RECORD_TYPES = {"A", "AAAA"}


@dataclass
class EndpointReconcileResult:
    resolver: str
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.created) + len(self.updated) + len(self.removed)


def _address_records(dns_records: list[dict[str, Any]]) -> dict[str, str]:
    """Return ``{hostname_lower: ip}`` for enabled A/AAAA records only.

    On duplicate hostnames the first enabled record wins (deterministic on the
    source's own ordering).
    """
    out: dict[str, str] = {}
    for r in dns_records:
        if not r.get("enabled", True):
            continue
        if (r.get("record_type") or "A") not in _ADDRESS_RECORD_TYPES:
            continue
        host = r.get("hostname")
        ip = r.get("value")
        if not isinstance(host, str) or not host or not isinstance(ip, str) or not ip:
            continue
        out.setdefault(host.strip().lower(), ip)
    return out


async def _get_or_create_service(session: AsyncSession, name: str) -> Service:
    svc = (await session.execute(select(Service).where(Service.name == name))).scalar_one_or_none()
    if svc is None:
        svc = Service(name=name)
        session.add(svc)
        await session.flush()
    return svc


async def reconcile_endpoints(
    session: AsyncSession,
    dns_records: list[dict[str, Any]],
    *,
    scope: ResolutionScope,
    resolver: str,
    source: DiscoverySource,
    when: datetime | None = None,
) -> EndpointReconcileResult:
    """Upsert ServiceEndpoints for one ``(scope, resolver)`` from address records."""
    result = EndpointReconcileResult(resolver=resolver)
    desired = _address_records(dns_records)  # hostname_lower -> ip

    # Existing endpoints for exactly this (scope, resolver) slice.
    existing_rows = (
        (
            await session.execute(
                select(ServiceEndpoint).where(
                    ServiceEndpoint.scope == scope,
                    ServiceEndpoint.resolver == resolver,
                )
            )
        )
        .scalars()
        .all()
    )
    existing = {ep.hostname: ep for ep in existing_rows}

    for hostname, ip in desired.items():
        ep = existing.get(hostname)
        if ep is None:
            service = await _get_or_create_service(session, hostname)
            session.add(
                ServiceEndpoint(
                    service_id=service.id,
                    scope=scope,
                    hostname=hostname,
                    ip=ip,
                    resolver=resolver,
                    discovery_source=source,
                )
            )
            result.created.append(hostname)
        elif ep.ip != ip:
            ep.ip = ip
            if when is not None:
                ep.updated_at = when
            result.updated.append(hostname)
        else:
            result.unchanged.append(hostname)

    # Remove endpoints the source no longer reports; clean up orphaned Services.
    for hostname, ep in existing.items():
        if hostname in desired:
            continue
        service_id = ep.service_id
        await session.delete(ep)
        result.removed.append(hostname)
        await session.flush()
        remaining = (
            (
                await session.execute(
                    select(ServiceEndpoint).where(ServiceEndpoint.service_id == service_id)
                )
            )
            .scalars()
            .first()
        )
        if remaining is None:
            orphan = await session.get(Service, service_id)
            if orphan is not None:
                await session.delete(orphan)

    await session.flush()
    return result


async def reconcile_internal_endpoints(
    session: AsyncSession,
    dns_records: list[dict[str, Any]],
    *,
    resolver: str = "unifi",
    source: DiscoverySource = DiscoverySource.UNIFI,
    when: datetime | None = None,
) -> EndpointReconcileResult:
    """Upsert internal ServiceEndpoints (UniFi by default) — internal DNS."""
    return await reconcile_endpoints(
        session,
        dns_records,
        scope=ResolutionScope.INTERNAL,
        resolver=resolver,
        source=source,
        when=when,
    )


async def reconcile_external_endpoints(
    session: AsyncSession,
    dns_records: list[dict[str, Any]],
    *,
    resolver: str = "cloudflare",
    source: DiscoverySource = DiscoverySource.CLOUDFLARE,
    when: datetime | None = None,
) -> EndpointReconcileResult:
    """Upsert external ServiceEndpoints (Cloudflare by default) — public DNS."""
    return await reconcile_endpoints(
        session,
        dns_records,
        scope=ResolutionScope.EXTERNAL,
        resolver=resolver,
        source=source,
        when=when,
    )


__all__ = [
    "EndpointReconcileResult",
    "reconcile_endpoints",
    "reconcile_external_endpoints",
    "reconcile_internal_endpoints",
]
