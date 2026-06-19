"""Tests for the external network-scan importer.

Pure parser/classifier tests plus DB-backed import behavior: host upsert by
ip/short-hostname, agentless findings for unprobeable hosts, skip-when-probed,
and idempotent re-runs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import DiscoverySource, FindingKind, FindingStatus
from homelab_helper.db.models import Host, ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.scan_import import (
    ProbeStrategy,
    ScanImporter,
    ScanRow,
    classify,
    parse_scan_csv,
)

_WHEN = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

_CSV = """ip,hostname,os_class,os_detail,ssh_banner,ttl,mac,nic_vendor
10.0.6.20,node0.example.lan,Linux/Unix,Linux (Debian),SSH-2.0-OpenSSH_10.0p2 Debian-7,64,02:00:03:b5:68:4b,unknown OUI
10.0.4.124,wled-paintedcloud.example.lan,network/embedded,network/embedded,,255,02:00:09:00:00:01,Espressif
10.0.5.54,fishcam.example.lan,Linux/Unix,Linux (embedded/dropbear),SSH-2.0-dropbear_2022.83,64,00:11:22:33:44:55,unknown OUI
10.0.0.202,DESKTOP-NG53E9F.example.lan,Windows,Windows,,128,aa:bb:cc:dd:ee:ff,Intel
10.0.0.30,-,Linux/Unix,Linux/Unix,,64,,unknown OUI
"""


@pytest.fixture
async def engine():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine):
    return make_sessionmaker(engine)


def test_parse_skips_blank_ip_and_normalizes_dashes() -> None:
    rows = parse_scan_csv(_CSV)
    assert len(rows) == 5
    dorkprint = next(r for r in rows if r.ip == "10.0.0.30")
    assert dorkprint.hostname is None  # "-" -> None
    assert dorkprint.mac is None


def test_classify_strategies() -> None:
    assert classify(ScanRow(ip="1", ssh_banner="SSH-2.0-OpenSSH_9.2")) is ProbeStrategy.SSH
    assert classify(ScanRow(ip="1", os_class="Windows")) is ProbeStrategy.WINDOWS
    # dropbear is embedded SSH the host probes can't drive -> agentless
    assert (
        classify(ScanRow(ip="1", os_class="Linux/Unix", ssh_banner="SSH-2.0-dropbear_2022"))
        is ProbeStrategy.AGENTLESS
    )
    assert classify(ScanRow(ip="1", os_class="network/embedded")) is ProbeStrategy.AGENTLESS


async def test_import_creates_hosts_and_agentless_findings(sessionmaker) -> None:
    rows = parse_scan_csv(_CSV)
    async with session_scope(sessionmaker) as s:
        result = await ScanImporter().import_rows(s, rows, when=_WHEN)

    assert result.hosts_created == 5
    assert result.hosts_updated == 0
    # 1 ssh (node0) -> no finding; 4 agentless/windows -> 4 findings
    assert len(result.findings_opened) == 4
    assert result.by_strategy[ProbeStrategy.SSH] == 1
    assert result.by_strategy[ProbeStrategy.WINDOWS] == 1
    assert result.by_strategy[ProbeStrategy.AGENTLESS] == 3

    async with sessionmaker() as s:
        hosts = (await s.execute(select(Host))).scalars().all()
        assert len(hosts) == 5
        # Dashed hostname falls back to the IP.
        assert any(h.hostname == "10.0.0.30" for h in hosts)
        node0 = next(h for h in hosts if h.hostname == "node0.example.lan")
        assert node0.capabilities["scan_strategy"] == "ssh"
        assert node0.capabilities["primary_mac"] == "02:00:03:b5:68:4b"

        findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert all(f.kind is FindingKind.DISCOVERY_AGENTLESS_NEEDED for f in findings)
        # node0 (SSH) is not flagged.
        assert not any("node0" in f.title for f in findings)


async def test_import_enriches_probed_host_without_flagging(sessionmaker) -> None:
    # A host we've already deep-probed over SSH (kernel-probe source).
    async with session_scope(sessionmaker) as s:
        s.add(
            Host(
                hostname="nas0",
                primary_ip="10.0.6.29",
                discovery_source=DiscoverySource.KERNEL_PROBE,
                capabilities={"cpu_model": "Ryzen 5 5500"},
            )
        )

    # Scan reports it as embedded-looking (no OpenSSH banner) — must NOT be flagged,
    # and its existing capabilities must survive.
    rows = [ScanRow(ip="10.0.6.29", hostname="nas0.example.lan", os_class="network/embedded")]
    async with session_scope(sessionmaker) as s:
        result = await ScanImporter().import_rows(s, rows, when=_WHEN)

    assert result.hosts_created == 0
    assert result.hosts_updated == 1
    assert result.already_probed_skipped == 1
    assert result.findings_opened == []

    async with sessionmaker() as s:
        host = (await s.execute(select(Host).where(Host.hostname == "nas0"))).scalar_one()
        assert host.capabilities["cpu_model"] == "Ryzen 5 5500"  # preserved
        assert host.capabilities["scan_os_class"] == "network/embedded"  # enriched
        findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert findings == []


async def test_agentless_finding_resolves_once_host_is_probed(sessionmaker) -> None:
    # First import: a Talos-looking node with no SSH banner is flagged agentless.
    rows = [ScanRow(ip="10.0.6.17", hostname="cp1.example.lan", os_class="Linux/Unix")]
    async with session_scope(sessionmaker) as s:
        first = await ScanImporter().import_rows(s, rows, when=_WHEN)
    assert len(first.findings_opened) == 1

    # The operator deep-probes it (e.g. via Talos) — discovery_source flips.
    async with session_scope(sessionmaker) as s:
        host = (await s.execute(select(Host).where(Host.hostname.like("cp1%")))).scalar_one()
        host.discovery_source = DiscoverySource.KERNEL_PROBE

    # Re-import: the stale agentless finding auto-resolves.
    async with session_scope(sessionmaker) as s:
        second = await ScanImporter().import_rows(s, rows, when=_WHEN)
    assert len(second.findings_resolved) == 1
    assert second.findings_opened == []

    async with sessionmaker() as s:
        finding = (await s.execute(select(ReconciliationFinding))).scalar_one()
        assert finding.status is FindingStatus.RESOLVED


async def test_import_is_idempotent(sessionmaker) -> None:
    rows = parse_scan_csv(_CSV)
    async with session_scope(sessionmaker) as s:
        first = await ScanImporter().import_rows(s, rows, when=_WHEN)
    async with session_scope(sessionmaker) as s:
        second = await ScanImporter().import_rows(s, rows, when=_WHEN)

    assert first.hosts_created == 5
    assert second.hosts_created == 0
    assert second.hosts_updated == 5
    assert second.findings_opened == []  # all re-seen, none newly opened
    assert len(second.findings_reseen) == 4

    async with sessionmaker() as s:
        assert len((await s.execute(select(Host))).scalars().all()) == 5
        assert len((await s.execute(select(ReconciliationFinding))).scalars().all()) == 4
