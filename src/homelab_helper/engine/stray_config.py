"""Stray-config detection — configured-but-unused UniFi networks → STRAY_CONFIG.

A UniFi network/VLAN that is defined and enabled but has *no client* living in
its subnet is a stray-config candidate: config the operator carries but nothing
uses. This module cross-references the controller's network definitions against
its known clients by subnet membership (deterministic, IP-based — no reliance on
UniFi's internal id linkage) and records each empty network as a ``STRAY_CONFIG``
finding, with the same fingerprint + reopen-on-recurrence lifecycle as the rest
of the finding surface.

Structural, not temporal. The roadmap's fuller "no observed traffic in N days"
signal needs the Phase-2 time-series; until then, *zero clients in the subnet*
is the observable L1 proxy — a real stray-config smell that needs no history.
Severity is LOW: worth surfacing, rarely urgent.

Scope discipline (invariant #1): only networks the controller *reports* this run
are considered. A network that now has clients resolves its finding; a network
that dropped out of the config entirely is left untouched (absence never
auto-resolves). Networks without a routable subnet (WAN, vlan-only w/o subnet)
are skipped — they can't host clients, so "no clients" says nothing.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.engine.fingerprint import make_fingerprint

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_TARGET_TYPE = "network"
_ROOT_CAUSE = "no-clients"


@dataclass
class StrayConfigResult:
    opened: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    skipped_no_subnet: int = 0

    @property
    def stray(self) -> int:
        return len(self.opened) + len(self.reopened) + len(self.updated)


def _network_key(network: dict[str, Any]) -> str:
    """A stable label for a network — its name, falling back to the subnet."""
    return str(network.get("name") or network.get("subnet") or "?")


def _stray_fingerprint(network: dict[str, Any]) -> str:
    return make_fingerprint(
        FindingKind.STRAY_CONFIG.value, _TARGET_TYPE, _network_key(network), _ROOT_CAUSE
    )


def _parse_network(subnet: str | None) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    if not subnet:
        return None
    try:
        return ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return None


def _client_count_in(
    net: ipaddress.IPv4Network | ipaddress.IPv6Network, clients: list[dict[str, Any]]
) -> int:
    count = 0
    for c in clients:
        ip = c.get("ip")
        if not ip:
            continue
        try:
            if ipaddress.ip_address(ip) in net:
                count += 1
        except ValueError:
            continue
    return count


async def _find_by_fingerprint(
    session: AsyncSession, fingerprint: str
) -> ReconciliationFinding | None:
    return (
        await session.execute(
            select(ReconciliationFinding).where(ReconciliationFinding.fingerprint == fingerprint)
        )
    ).scalar_one_or_none()


async def reconcile_stray_config(
    session: AsyncSession,
    networks: list[dict[str, Any]],
    clients: list[dict[str, Any]],
    *,
    when: datetime | None = None,
) -> StrayConfigResult:
    """Open/resolve STRAY_CONFIG findings for enabled networks with no clients."""
    result = StrayConfigResult()
    now_ts = when or datetime.now(UTC)

    for network in networks:
        if not network.get("enabled", True):
            continue
        net = _parse_network(network.get("subnet"))
        if net is None:
            result.skipped_no_subnet += 1
            continue

        label = _network_key(network)
        fp = _stray_fingerprint(network)
        existing = await _find_by_fingerprint(session, fp)
        client_count = _client_count_in(net, clients)

        if client_count == 0:
            vlan = network.get("vlan_id")
            vlan_bit = f" (VLAN {vlan})" if vlan else ""
            title = f"Stray config: {label}"
            description = (
                f"UniFi network {label!r}{vlan_bit} — subnet {net} — is enabled but has no "
                "clients. Candidate stray config (structural signal; traffic history lands "
                "with Phase 2)."
            )
            affected = [{"target_type": _TARGET_TYPE, "target_id": label}]
            if existing is None:
                session.add(
                    ReconciliationFinding(
                        kind=FindingKind.STRAY_CONFIG,
                        severity=FindingSeverity.LOW,
                        fingerprint=fp,
                        title=title,
                        description=description,
                        affected=affected,
                        evidence_refs=[{"type": "unifi_network", "id": label}],
                        status=FindingStatus.OPEN,
                        first_seen=now_ts,
                        last_seen=now_ts,
                    )
                )
                result.opened.append(label)
            else:
                if existing.status == FindingStatus.RESOLVED:
                    existing.status = FindingStatus.OPEN
                    existing.resolved_at = None
                    existing.first_seen = now_ts
                    result.reopened.append(label)
                else:
                    result.updated.append(label)
                existing.last_seen = now_ts
                existing.severity = FindingSeverity.LOW
                existing.title = title
                existing.description = description
                existing.affected = affected
        elif existing is not None and existing.status in {
            FindingStatus.OPEN,
            FindingStatus.ACKNOWLEDGED,
        }:
            existing.status = FindingStatus.RESOLVED
            existing.resolved_at = now_ts
            result.resolved.append(label)

    await session.flush()
    return result


__all__ = ["StrayConfigResult", "reconcile_stray_config"]
