"""External network-scan ingest — turn a host-scan CSV into tracked inventory.

The live ``network.subnet-scan`` probe sweeps a CIDR the harness can reach; this
module ingests an *external* sweep an operator already ran (nmap/arp/banner-grab
exported to CSV). Every row becomes (or updates) a ``Host`` row, and any host
whose scan signature shows no full SSH server — embedded/dropbear IoT, network
gear, Windows — gets a ``DISCOVERY_AGENTLESS_NEEDED`` finding, the enum reserved
for the "we can see it but no kernel probe can reach it" case.

Hosts already in the DB (matched by ``primary_ip`` or short hostname) are
enriched, never clobbered: a box we've deep-probed over SSH keeps its
``discovery_source`` and capabilities and is considered covered, so it gets no
agentless finding. Re-running is idempotent — hosts upsert by identity and
findings upsert by deterministic fingerprint.

Expected CSV columns: ``ip,hostname,os_class,os_detail,ssh_banner,ttl,mac,
nic_vendor``.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel
from sqlalchemy import or_, select

from homelab_helper.db.enums import (
    DiscoverySource,
    FindingKind,
    FindingSeverity,
    FindingStatus,
)
from homelab_helper.db.models import Host, ReconciliationFinding
from homelab_helper.engine.fingerprint import make_fingerprint

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

_EMPTY_HOSTNAME = "-"


class ProbeStrategy(StrEnum):
    """How (if at all) the harness can deep-probe a scanned host."""

    SSH = "ssh"  # full OpenSSH server — kernel-ssh probes apply
    AGENTLESS = "agentless"  # embedded/dropbear/IoT/no-banner — needs agentless discovery
    WINDOWS = "windows"  # needs a future Windows probe; agentless for now


class ScanRow(BaseModel):
    """One parsed row of the scan CSV."""

    ip: str
    hostname: str | None = None
    os_class: str = ""
    os_detail: str = ""
    ssh_banner: str = ""
    ttl: str | None = None
    mac: str | None = None
    nic_vendor: str | None = None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return None if v in ("", _EMPTY_HOSTNAME) else v


def parse_scan_csv(text: str) -> list[ScanRow]:
    """Parse scan CSV text into ``ScanRow`` objects. Rows without an IP are skipped."""
    rows: list[ScanRow] = []
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        ip = _clean(raw.get("ip"))
        if ip is None:
            continue
        rows.append(
            ScanRow(
                ip=ip,
                hostname=_clean(raw.get("hostname")),
                os_class=(raw.get("os_class") or "").strip(),
                os_detail=(raw.get("os_detail") or "").strip(),
                ssh_banner=(raw.get("ssh_banner") or "").strip(),
                ttl=_clean(raw.get("ttl")),
                mac=_clean(raw.get("mac")),
                nic_vendor=_clean(raw.get("nic_vendor")),
            )
        )
    return rows


def classify(row: ScanRow) -> ProbeStrategy:
    """Pick a probe strategy from the scan signature.

    A full ``OpenSSH`` banner is the only reliable "deep-probeable" signal — the
    ``os_class`` of ``Linux/Unix`` covers everything from a Proxmox node to a
    Chromecast, so it can't be trusted alone. Dropbear is an embedded SSH that
    the host probes can't drive, so it counts as agentless.
    """
    if "openssh" in row.ssh_banner.lower():
        return ProbeStrategy.SSH
    if row.os_class.strip().lower() == "windows":
        return ProbeStrategy.WINDOWS
    return ProbeStrategy.AGENTLESS


def _short(hostname: str) -> str:
    """``node0.example.lan`` -> ``node0``; first DNS label, lowercased."""
    return hostname.split(".", 1)[0].strip().lower()


@dataclass
class ImportResult:
    """Summary of one import run."""

    hosts_created: int = 0
    hosts_updated: int = 0
    by_strategy: dict[ProbeStrategy, int] = field(default_factory=dict)
    findings_opened: list[str] = field(default_factory=list)
    findings_reseen: list[str] = field(default_factory=list)
    findings_resolved: list[str] = field(default_factory=list)
    already_probed_skipped: int = 0


class ScanImporter:
    """Upserts Host rows + agentless coverage findings from a parsed scan."""

    async def import_rows(
        self,
        session: AsyncSession,
        rows: list[ScanRow],
        *,
        when: datetime,
        source_label: str = "network-scan",
    ) -> ImportResult:
        result = ImportResult()
        for row in rows:
            strategy = classify(row)
            result.by_strategy[strategy] = result.by_strategy.get(strategy, 0) + 1
            host, created = await self._upsert_host(session, row, when, source_label)
            if created:
                result.hosts_created += 1
            else:
                result.hosts_updated += 1

            # A host we've actually deep-probed is covered: never flag it, and
            # resolve any stale agentless finding (e.g. a Talos node that has no
            # SSH banner but is fully inventoried via its API).
            if host.discovery_source is DiscoverySource.KERNEL_PROBE:
                result.already_probed_skipped += 1
                await self._resolve_agentless_findings(session, host, when, result)
                continue
            # SSH-probeable but not yet probed — pending, not a coverage gap.
            if strategy is ProbeStrategy.SSH:
                continue
            await self._emit_agentless_finding(session, host, row, strategy, when, result)

        await session.flush()
        return result

    async def _upsert_host(
        self,
        session: AsyncSession,
        row: ScanRow,
        when: datetime,
        source_label: str,
    ) -> tuple[Host, bool]:
        """Find a Host by primary_ip or short hostname; create one if missing."""
        candidates = [row.ip]
        if row.hostname:
            candidates.append(_short(row.hostname))
        existing = (
            (
                await session.execute(
                    select(Host).where(
                        or_(
                            Host.primary_ip == row.ip,
                            Host.hostname.in_(candidates),
                        )
                    )
                )
            )
            .scalars()
            .first()
        )

        scan_caps = {
            "scan_source": source_label,
            "scan_os_class": row.os_class or None,
            "scan_os_detail": row.os_detail or None,
            "scan_ssh_banner": row.ssh_banner or None,
            "scan_nic_vendor": row.nic_vendor,
            "scan_strategy": classify(row).value,
            "primary_mac": row.mac,
        }

        if existing is not None:
            if existing.primary_ip is None:
                existing.primary_ip = row.ip
            # Merge scan_* keys without disturbing reconciler-projected caps.
            merged = dict(existing.capabilities or {})
            merged.update({k: v for k, v in scan_caps.items() if v is not None})
            existing.capabilities = merged
            existing.discovery_last_run = when
            return existing, False

        host = Host(
            hostname=row.hostname or row.ip,
            primary_ip=row.ip,
            discovery_source=DiscoverySource.MANUAL,
            discovery_last_run=when,
            capabilities={k: v for k, v in scan_caps.items() if v is not None},
        )
        session.add(host)
        await session.flush()
        return host, True

    async def _resolve_agentless_findings(
        self, session: AsyncSession, host: Host, when: datetime, result: ImportResult
    ) -> None:
        """Resolve any OPEN agentless finding for a now-covered host."""
        for strat in (ProbeStrategy.AGENTLESS, ProbeStrategy.WINDOWS):
            fp = make_fingerprint(
                FindingKind.DISCOVERY_AGENTLESS_NEEDED.value,
                "host",
                str(host.id),
                f"agentless:{strat.value}",
            )
            existing = (
                await session.execute(
                    select(ReconciliationFinding).where(ReconciliationFinding.fingerprint == fp)
                )
            ).scalar_one_or_none()
            if existing is not None and existing.status in (
                FindingStatus.OPEN,
                FindingStatus.ACKNOWLEDGED,
            ):
                existing.status = FindingStatus.RESOLVED
                existing.resolved_at = when
                result.findings_resolved.append(fp)

    async def _emit_agentless_finding(
        self,
        session: AsyncSession,
        host: Host,
        row: ScanRow,
        strategy: ProbeStrategy,
        when: datetime,
        result: ImportResult,
    ) -> None:
        """Upsert a DISCOVERY_AGENTLESS_NEEDED finding (INFO) for an unprobeable host."""
        token = f"agentless:{strategy.value}"
        fp = make_fingerprint(
            FindingKind.DISCOVERY_AGENTLESS_NEEDED.value, "host", str(host.id), token
        )
        reason = (
            "runs Windows (no kernel-ssh probe yet)"
            if strategy is ProbeStrategy.WINDOWS
            else "exposes no full SSH server (embedded/dropbear/IoT or network gear)"
        )
        title = f"{host.hostname} needs agentless discovery"
        description = (
            f"Host at {row.ip} ({row.os_detail or row.os_class or 'unknown OS'}) {reason}; "
            "the kernel-ssh host probes cannot reach it. Inventory is limited to the "
            "scan signature until an agentless adapter (SNMP/API/Windows) lands."
        )
        existing = (
            await session.execute(
                select(ReconciliationFinding).where(ReconciliationFinding.fingerprint == fp)
            )
        ).scalar_one_or_none()
        affected = [{"type": "host", "id": str(host.id)}]
        evidence = [{"type": "scan", "id": row.ip}]
        if existing is not None:
            if existing.status is FindingStatus.RESOLVED:
                existing.status = FindingStatus.OPEN
                existing.resolved_at = None
                existing.first_seen = when
                result.findings_opened.append(fp)
            else:
                result.findings_reseen.append(fp)
            existing.last_seen = when
            existing.title = title
            existing.description = description
            return

        session.add(
            ReconciliationFinding(
                kind=FindingKind.DISCOVERY_AGENTLESS_NEEDED,
                severity=FindingSeverity.INFO,
                fingerprint=fp,
                title=title,
                description=description,
                affected=affected,
                evidence_refs=evidence,
                status=FindingStatus.OPEN,
                first_seen=when,
                last_seen=when,
            )
        )
        result.findings_opened.append(fp)


__all__ = [
    "ImportResult",
    "ProbeStrategy",
    "ScanImporter",
    "ScanRow",
    "classify",
    "parse_scan_csv",
]
