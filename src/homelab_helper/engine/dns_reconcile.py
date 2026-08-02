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

``A``/``AAAA`` records map to an endpoint's ``ip``; ``CNAME`` records map to its
``target``. CNAMEs matter because a tunnel-published service has no public
address record — its external name aliases ``<uuid>.cfargotunnel.com`` — so
address-only import hid exactly the services worth comparing. MX/TXT/SRV carry
no resolution for a client connecting by name and stay out.
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
_ALIAS_RECORD_TYPES = {"CNAME"}
_RESOLVING_RECORD_TYPES = _ADDRESS_RECORD_TYPES | _ALIAS_RECORD_TYPES


@dataclass(frozen=True)
class Resolution:
    """Where one DNS name points: an address, or an alias target."""

    record_type: str
    value: str

    @property
    def ip(self) -> str | None:
        return self.value if self.record_type in _ADDRESS_RECORD_TYPES else None

    @property
    def target(self) -> str | None:
        return self.value if self.record_type in _ALIAS_RECORD_TYPES else None


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


def _resolving_records(dns_records: list[dict[str, Any]]) -> dict[str, Resolution]:
    """Return ``{hostname_lower: Resolution}`` for enabled A/AAAA/CNAME records.

    CNAMEs are included because a tunnel-fronted service has no public address
    record at all — its external name is a CNAME to ``<uuid>.cfargotunnel.com``.
    Dropping those made precisely the interesting services invisible to
    split-brain detection. MX/TXT/SRV still carry no resolution for a client
    connecting by name, so they stay out.

    On duplicate hostnames the first enabled record wins (deterministic on the
    source's own ordering).
    """
    out: dict[str, Resolution] = {}
    for r in dns_records:
        if not r.get("enabled", True):
            continue
        record_type = (r.get("record_type") or "A").upper()
        if record_type not in _RESOLVING_RECORD_TYPES:
            continue
        host = r.get("hostname")
        value = r.get("value")
        if not isinstance(host, str) or not host or not isinstance(value, str) or not value:
            continue
        out.setdefault(host.strip().lower(), Resolution(record_type, value.strip()))
    return out


@dataclass(frozen=True)
class SplitBrain:
    """One service resolving differently inside vs outside.

    ``kind`` separates two situations that look identical to a set comparison
    but mean different things to an operator:

    ``tunnel-fronted``
        The external name is a CNAME to a Cloudflare Tunnel. Internal and
        external *always* differ here, and that is the intended design — not a
        misconfiguration. Reporting it as plain drift would turn a correct
        architecture into noise.
    ``divergent-address``
        Both sides resolve to addresses, and they disagree. This is the classic
        split-brain worth looking at.
    """

    internal: list[str]
    external: list[str]
    kind: str

    @property
    def is_expected(self) -> bool:
        return self.kind == "tunnel-fronted"


def analyze_split_brain(endpoints: list[ServiceEndpoint]) -> SplitBrain | None:
    """Compare a service's internal vs external resolution.

    Compares *resolutions* (an IP, or a CNAME target) rather than IPs alone —
    an endpoint with only a CNAME has no IP, and skipping it is what hid
    tunnel-published services entirely.
    """
    internal = sorted(
        {e.resolution for e in endpoints if e.scope == ResolutionScope.INTERNAL and e.resolution}
    )
    external = sorted(
        {e.resolution for e in endpoints if e.scope == ResolutionScope.EXTERNAL and e.resolution}
    )
    if not internal or not external or internal == external:
        return None
    tunnel = any(e.scope == ResolutionScope.EXTERNAL and e.is_tunnel for e in endpoints)
    return SplitBrain(
        internal=internal,
        external=external,
        kind="tunnel-fronted" if tunnel else "divergent-address",
    )


def service_key(hostname: str) -> str:
    """Canonical service name shared across resolvers and domains.

    Internal and external DNS suffix the same service differently (``ha.lan``
    inside, ``ha.example.com`` outside). Both should attach to one ``Service``,
    so the key is the leftmost DNS label, lowercased — the part that actually
    names the service. Two distinct services that happen to share a short name
    will merge onto one ``Service``; that's an acceptable trade for a homelab,
    and an explicit alias map can override it later (see backlog).
    """
    return hostname.strip().lower().split(".", 1)[0]


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
    desired = _resolving_records(dns_records)  # hostname_lower -> Resolution

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

    for hostname, res in desired.items():
        ep = existing.get(hostname)
        if ep is None:
            service = await _get_or_create_service(session, service_key(hostname))
            session.add(
                ServiceEndpoint(
                    service_id=service.id,
                    scope=scope,
                    hostname=hostname,
                    ip=res.ip,
                    target=res.target,
                    record_type=res.record_type,
                    resolver=resolver,
                    discovery_source=source,
                )
            )
            result.created.append(hostname)
        elif (ep.ip, ep.target, ep.record_type) != (res.ip, res.target, res.record_type):
            # A record flipping A -> CNAME (or its value changing) is a real
            # change even when one side of the pair stays None.
            ep.ip, ep.target, ep.record_type = res.ip, res.target, res.record_type
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
    "Resolution",
    "SplitBrain",
    "analyze_split_brain",
    "reconcile_endpoints",
    "reconcile_external_endpoints",
    "reconcile_internal_endpoints",
    "service_key",
]
