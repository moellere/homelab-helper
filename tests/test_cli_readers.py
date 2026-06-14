"""CLI reader tests — ``helper findings``, ``helper host show``, ``helper audit``.

Each test seeds a fresh file-backed SQLite DB (tmp_path) via the same models
the reconciler uses, then runs the CLI through Typer's ``CliRunner``. We use
file-backed (not in-memory) SQLite because each CLI invocation builds its
own engine — an in-memory DB would be empty by the time the verb opens it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    Architecture,
    FindingKind,
    FindingSeverity,
    FindingStatus,
    PartKind,
)
from homelab_helper.db.models import (
    Host,
    PhysicalPart,
    Placement,
    ReconciliationFinding,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope


@pytest.fixture
async def seeded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Build a small but realistic seed: 2 hosts, 1 DIMM placement, 2 findings."""
    db_path = tmp_path / "cli-readers.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)

    async with session_scope(sm) as s:
        host_a = Host(
            hostname="bmax0",
            primary_ip="10.250.6.20",
            arch=Architecture.AMD64,
            capabilities={"kernel": "6.8.0-49-generic", "cpu_cores": 6},
        )
        host_b = Host(hostname="bmax1", primary_ip="10.250.6.21", arch=Architecture.AMD64)
        s.add_all([host_a, host_b])
        await s.flush()

        dimm = PhysicalPart(
            kind=PartKind.DIMM,
            serial="DIMM-AAA",
            manufacturer="Crucial",
            model="CT16G",
            capacity_bytes=16 * 1024**3,
        )
        s.add(dimm)
        await s.flush()
        s.add(
            Placement(
                part_id=dimm.id,
                host_id=host_a.id,
                slot="DIMM_A1",
                from_date=datetime(2026, 5, 1, tzinfo=UTC),
            )
        )

        s.add(
            ReconciliationFinding(
                kind=FindingKind.INVENTORY_GAP,
                severity=FindingSeverity.LOW,
                fingerprint="abc123def4567890",
                title="DIMM in DIMM_A2 has no serial",
                description="…",
                affected=[
                    {"target_type": "host", "target_id": str(host_a.id)},
                    {"target_type": "inventory-category", "target_id": "dimm"},
                ],
                status=FindingStatus.OPEN,
                first_seen=datetime(2026, 5, 1, tzinfo=UTC),
                last_seen=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        s.add(
            ReconciliationFinding(
                kind=FindingKind.INVENTORY_GAP,
                severity=FindingSeverity.LOW,
                fingerprint="0011223344556677",
                title="Storage device /dev/sda has no stable identity",
                description="…",
                affected=[
                    {"target_type": "host", "target_id": str(host_b.id)},
                    {"target_type": "inventory-category", "target_id": "storage"},
                ],
                status=FindingStatus.RESOLVED,
                first_seen=datetime(2026, 5, 1, tzinfo=UTC),
                last_seen=datetime(2026, 5, 15, tzinfo=UTC),
                resolved_at=datetime(2026, 5, 15, tzinfo=UTC),
            )
        )

    await engine.dispose()
    return url


runner = CliRunner()


# ---------------------------------------------------------------------------
# helper findings list
# ---------------------------------------------------------------------------


def test_findings_list_default_shows_open(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["findings", "list"])
    assert result.exit_code == 0
    assert "DIMM_A2" in result.stdout
    # The resolved one is filtered out by the default (status=open).
    assert "/dev/sda" not in result.stdout


def test_findings_list_all_shows_resolved(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["findings", "list", "--status", "all"])
    assert result.exit_code == 0
    # Both fingerprint prefixes show up — table cell wrapping under CliRunner's
    # narrow virtual terminal makes title-substring matches flaky, but the
    # fingerprint column is fixed-width and never wraps.
    assert "abc123def456" in result.stdout
    assert "001122334455" in result.stdout
    assert "2 finding(s)" in result.stdout


def test_findings_list_unknown_status_is_rejected(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["findings", "list", "--status", "explosive"])
    assert result.exit_code != 0


def test_findings_list_filters_by_severity(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["findings", "list", "--severity", "high"])
    assert result.exit_code == 0
    # Only low-severity findings were seeded; high filter ⇒ empty.
    assert "no findings match" in result.stdout.lower()


def test_findings_list_filters_by_host(seeded_db_url: str) -> None:
    # bmax1 only has the resolved storage finding — with default open filter
    # there's nothing for it.
    result = runner.invoke(app, ["findings", "list", "--host", "nonexistent-uuid"])
    assert result.exit_code == 0
    assert "no findings match" in result.stdout.lower()


# ---------------------------------------------------------------------------
# helper findings show
# ---------------------------------------------------------------------------


def test_findings_show_by_full_fingerprint(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["findings", "show", "abc123def4567890"])
    assert result.exit_code == 0
    assert "DIMM_A2" in result.stdout
    assert "abc123def4567890" in result.stdout


def test_findings_show_by_prefix(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["findings", "show", "abc123"])
    assert result.exit_code == 0
    assert "DIMM_A2" in result.stdout


def test_findings_show_unknown_prefix_errors(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["findings", "show", "deadbeef"])
    assert result.exit_code == 2
    assert "no finding matches" in result.stdout.lower()


# ---------------------------------------------------------------------------
# helper findings ack / resolve / suppress (mutations)
# ---------------------------------------------------------------------------


def test_findings_ack_marks_acknowledged(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["findings", "ack", "abc123", "--by", "enoch"])
    assert result.exit_code == 0
    assert "ack" in result.stdout.lower()

    # Re-list with status=acknowledged proves the mutation stuck.
    listed = runner.invoke(app, ["findings", "list", "--status", "acknowledged"])
    assert listed.exit_code == 0
    assert "DIMM_A2" in listed.stdout


def test_findings_resolve_marks_resolved(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["findings", "resolve", "abc123"])
    assert result.exit_code == 0
    assert "resolved" in result.stdout.lower()

    listed = runner.invoke(app, ["findings", "list", "--status", "resolved"])
    assert "DIMM_A2" in listed.stdout


def test_findings_suppress_with_until(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["findings", "suppress", "abc123", "--until", "2027-01-01"])
    assert result.exit_code == 0
    assert "2027-01-01" in result.stdout


def test_findings_suppress_bad_date_is_rejected(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["findings", "suppress", "abc123", "--until", "not-a-date"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# helper host show
# ---------------------------------------------------------------------------


def test_host_show_emits_identity_capabilities_parts_findings(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["host", "show", "bmax0"])
    assert result.exit_code == 0
    assert "bmax0" in result.stdout
    assert "10.250.6.20" in result.stdout
    assert "amd64" in result.stdout
    # capability bag rendered
    assert "kernel" in result.stdout
    # the placed DIMM
    assert "DIMM-AAA" in result.stdout
    assert "DIMM_A1" in result.stdout
    # the open finding affecting this host
    assert "DIMM_A2" in result.stdout


def test_host_show_unknown_hostname_errors(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["host", "show", "ghost-host"])
    assert result.exit_code == 2
    assert "no host" in result.stdout.lower()


def test_host_show_bare_host_with_no_capabilities_or_parts(seeded_db_url: str) -> None:
    """bmax1 has no capabilities, no placements, no open findings."""
    result = runner.invoke(app, ["host", "show", "bmax1"])
    assert result.exit_code == 0
    assert "bmax1" in result.stdout
    assert "no capabilities" in result.stdout.lower()
    assert "no current part placements" in result.stdout.lower()
    assert "no open findings" in result.stdout.lower()


# ---------------------------------------------------------------------------
# helper audit
# ---------------------------------------------------------------------------


def test_audit_prints_inventory_and_breakdown(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["audit"])
    assert result.exit_code == 0
    assert "hosts:" in result.stdout
    assert "2" in result.stdout  # 2 hosts seeded
    assert "findings" in result.stdout.lower()
    # The severity/status table mentions LOW with two findings (open + resolved).
    assert "low" in result.stdout.lower()


def test_audit_top_limit_is_honored(seeded_db_url: str) -> None:
    result = runner.invoke(app, ["audit", "--top", "0"])
    assert result.exit_code == 0
    # With --top 0 there's no "most recent open" section but the roll-up still prints.
    assert "hosts:" in result.stdout
    assert "most recent open" not in result.stdout.lower()


# ---------------------------------------------------------------------------
# Help text registration (no DB required)
# ---------------------------------------------------------------------------


def test_top_level_help_lists_new_verbs() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "findings" in result.stdout
    assert "host" in result.stdout
    assert "audit" in result.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ["findings", "--help"],
        ["findings", "list", "--help"],
        ["findings", "show", "--help"],
        ["findings", "ack", "--help"],
        ["findings", "resolve", "--help"],
        ["findings", "suppress", "--help"],
        ["host", "--help"],
        ["host", "show", "--help"],
        ["audit", "--help"],
    ],
)
def test_subcommand_help_loads(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0
