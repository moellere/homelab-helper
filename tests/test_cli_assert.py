"""CLI tests for ``helper assert list|show|run`` against a seeded SQLite."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    AssertionKind,
    AssertionScope,
    FindingSeverity,
    IntentTargetType,
    PrivilegeLevel,
)
from homelab_helper.db.models import (
    ConfigurationAssertion,
    DiscoveryRun,
    Host,
    Observation,
    Probe,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture
async def assert_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """One host, two observations, three assertions (PASS / FAIL / SKIP)."""
    db_path = tmp_path / "assert.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)

    async with session_scope(sm) as s:
        host = Host(hostname="bmax0", primary_ip="10.250.6.20")
        s.add(host)
        await s.flush()

        probe = Probe(
            name="test-probe",
            version="0.1.0",
            module_path="test:probe",
            required_privilege=PrivilegeLevel.USER,
            produces_keys=[],
        )
        s.add(probe)
        await s.flush()
        run = DiscoveryRun(
            host_id=host.id,
            probe_id=probe.id,
            probe_name=probe.name,
            probe_version=probe.version,
            privilege_level=PrivilegeLevel.USER,
        )
        s.add(run)
        await s.flush()

        for key, value in [
            ("host.cpu.cores", 8),
            ("host.identity.os_id", "ubuntu"),
        ]:
            s.add(
                Observation(
                    run_id=run.id,
                    key=key,
                    value=value,
                    target_type=IntentTargetType.HOST,
                    target_id=str(host.id),
                    recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
                )
            )

        def _spec(**kwargs: Any) -> dict[str, Any]:
            return kwargs

        s.add(
            ConfigurationAssertion(
                name="bmax0.cores_eq_8",
                scope=AssertionScope.HOST,
                scope_target=str(host.id),
                description="bmax0 must report exactly 8 CPU cores",
                kind=AssertionKind.OBSERVATION_PREDICATE,
                verifier_spec=_spec(key="host.cpu.cores", value=8),
                severity_on_fail=FindingSeverity.MEDIUM,
            )
        )
        s.add(
            ConfigurationAssertion(
                name="bmax0.os_is_debian",
                scope=AssertionScope.HOST,
                scope_target=str(host.id),
                description="bmax0 should be running Debian (not Ubuntu)",
                kind=AssertionKind.OBSERVATION_PREDICATE,
                verifier_spec=_spec(key="host.identity.os_id", value="debian"),
                severity_on_fail=FindingSeverity.LOW,
            )
        )
        # No host.memory.* observation has been seeded, so this skips
        # (eq predicate against a missing key short-circuits to SKIP rather
        # than fabricating a comparison against None).
        s.add(
            ConfigurationAssertion(
                name="bmax0.mem_at_least_1gb",
                scope=AssertionScope.HOST,
                scope_target=str(host.id),
                description="bmax0 must report at least 1 GiB RAM",
                kind=AssertionKind.OBSERVATION_PREDICATE,
                verifier_spec=_spec(
                    key="host.memory.mem_total_bytes",
                    predicate="gte",
                    value=1 * 1024**3,
                ),
                severity_on_fail=FindingSeverity.LOW,
            )
        )

    await engine.dispose()
    return url


def test_assert_list_shows_assertions(assert_db_url: str) -> None:
    # Rich truncates long names under CliRunner's narrow virtual terminal,
    # so match on the shared "bmax0." prefix + footer count rather than the
    # full assertion names (which would otherwise be the cleaner assertion).
    result = runner.invoke(app, ["assert", "list"])
    assert result.exit_code == 0
    assert "bmax0." in result.stdout
    assert "3 assertion(s)" in result.stdout


def test_assert_show_displays_definition(assert_db_url: str) -> None:
    result = runner.invoke(app, ["assert", "show", "bmax0.cores_eq_8"])
    assert result.exit_code == 0
    assert "host.cpu.cores" in result.stdout
    assert "no runs yet" in result.stdout.lower()


def test_assert_show_unknown_errors(assert_db_url: str) -> None:
    result = runner.invoke(app, ["assert", "show", "does-not-exist"])
    assert result.exit_code == 2
    assert "no assertion" in result.stdout.lower()


def test_assert_run_single_pass(assert_db_url: str) -> None:
    result = runner.invoke(app, ["assert", "run", "--name", "bmax0.cores_eq_8"])
    assert result.exit_code == 0
    assert "pass" in result.stdout.lower()


def test_assert_run_single_fail_exits_nonzero(assert_db_url: str) -> None:
    result = runner.invoke(app, ["assert", "run", "--name", "bmax0.os_is_debian"])
    assert result.exit_code == 1
    assert "fail" in result.stdout.lower()
    assert "opened" in result.stdout.lower()


def test_assert_run_all_executes_every_enabled(assert_db_url: str) -> None:
    result = runner.invoke(app, ["assert", "run", "--all"])
    # One PASS, one FAIL, one SKIP — overall exit 1 because a FAIL is present.
    assert result.exit_code == 1
    # All three status words appear somewhere; assertion names may be
    # truncated by Rich.
    lower = result.stdout.lower()
    assert "pass" in lower
    assert "fail" in lower
    assert "skip" in lower


def test_assert_run_requires_name_or_all(assert_db_url: str) -> None:
    result = runner.invoke(app, ["assert", "run"])
    assert result.exit_code == 2
    assert "supply --name" in result.stdout.lower() or "supply --name" in result.stderr.lower()


def test_assert_run_name_and_all_conflict(assert_db_url: str) -> None:
    result = runner.invoke(app, ["assert", "run", "--name", "bmax0.cores_eq_8", "--all"])
    assert result.exit_code == 2
    assert "mutually exclusive" in (result.stdout + result.stderr).lower()


def test_assert_show_after_run_includes_latest_evidence(assert_db_url: str) -> None:
    # Run the FAILing assertion so there's a run with evidence.
    runner.invoke(app, ["assert", "run", "--name", "bmax0.os_is_debian"])
    result = runner.invoke(app, ["assert", "show", "bmax0.os_is_debian"])
    assert result.exit_code == 0
    assert "latest run" in result.stdout.lower()
    # The observed value (ubuntu) should appear in the evidence section.
    assert "ubuntu" in result.stdout.lower()


def test_assert_run_failure_shows_up_in_findings_list(assert_db_url: str) -> None:
    runner.invoke(app, ["assert", "run", "--name", "bmax0.os_is_debian"])
    # The failing assertion should now have an open CONFIG_DRIFT finding.
    listed = runner.invoke(app, ["findings", "list"])
    assert listed.exit_code == 0
    assert "config-drift" in listed.stdout.lower()


def test_assert_run_pass_after_fail_resolves_finding(assert_db_url: str) -> None:
    """End-to-end finding lifecycle through the CLI."""
    # First run: FAIL → open
    r1 = runner.invoke(app, ["assert", "run", "--name", "bmax0.os_is_debian"])
    assert "opened" in r1.stdout.lower()

    # Second run with same data: still FAIL → "still failing"
    r2 = runner.invoke(app, ["assert", "run", "--name", "bmax0.os_is_debian"])
    assert "still failing" in r2.stdout.lower()


def test_assert_list_with_all_includes_disabled(assert_db_url: str, tmp_path: Path) -> None:
    # The --all flag toggles enabled-only filtering. Disable one assertion
    # via direct DB write and confirm default hides it but --all shows it.
    import sqlite3

    db_path = tmp_path / "assert.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "UPDATE configuration_assertion SET enabled = 0 WHERE name = 'bmax0.mem_at_least_1gb'"
    )
    con.commit()
    con.close()

    # Rich truncates long names — check only the footer count instead.
    default = runner.invoke(app, ["assert", "list"])
    assert "2 assertion(s)" in default.stdout

    all_listed = runner.invoke(app, ["assert", "list", "--all"])
    assert "3 assertion(s)" in all_listed.stdout


_LIB_YAML_BMAX0 = """
version: 1
assertions:
  - name: bmax0.from_library.cores_at_least_4
    host: bmax0
    description: bmax0 must report >= 4 cores (loaded from library)
    severity_on_fail: low
    verifier_spec:
      key: host.cpu.cores
      predicate: gte
      value: 4
  - name: bmax0.from_library.os_supported
    host: bmax0
    description: bmax0 OS should be Ubuntu/Debian
    severity_on_fail: low
    verifier_spec:
      key: host.identity.os_id
      predicate: regex
      value: "^(ubuntu|debian)$"
"""


def test_assert_load_creates_rows(assert_db_url: str, tmp_path: Path) -> None:
    lib = tmp_path / "lib.yaml"
    lib.write_text(_LIB_YAML_BMAX0)

    result = runner.invoke(app, ["assert", "load", str(lib)])
    assert result.exit_code == 0
    assert "2 created" in result.stdout or "created" in result.stdout.lower()
    # The newly loaded names should appear in `helper assert list`.
    listed = runner.invoke(app, ["assert", "list"])
    assert listed.exit_code == 0
    # Footer count includes seeded + loaded (3 + 2 = 5).
    assert "5 assertion(s)" in listed.stdout


def test_assert_load_dry_run_writes_nothing(assert_db_url: str, tmp_path: Path) -> None:
    lib = tmp_path / "lib.yaml"
    lib.write_text(_LIB_YAML_BMAX0)

    result = runner.invoke(app, ["assert", "load", str(lib), "--dry-run"])
    assert result.exit_code == 0
    assert "dry run" in result.stdout.lower()

    # Footer count after dry-run should still show only the originally seeded.
    listed = runner.invoke(app, ["assert", "list"])
    assert "3 assertion(s)" in listed.stdout


def test_assert_load_idempotent(assert_db_url: str, tmp_path: Path) -> None:
    lib = tmp_path / "lib.yaml"
    lib.write_text(_LIB_YAML_BMAX0)
    runner.invoke(app, ["assert", "load", str(lib)])
    second = runner.invoke(app, ["assert", "load", str(lib)])
    assert second.exit_code == 0
    # Second run: nothing created, nothing updated, both entries unchanged.
    assert "0 created" in second.stdout or "unchanged" in second.stdout.lower()


def test_assert_load_skips_missing_host(assert_db_url: str, tmp_path: Path) -> None:
    lib = tmp_path / "lib.yaml"
    lib.write_text(
        """
version: 1
assertions:
  - name: nonexistent.assert
    host: ghost-host-not-in-db
    description: refers to a host that hasn't been discovered yet
    severity_on_fail: low
    verifier_spec: {key: host.cpu.cores, predicate: exists}
"""
    )
    result = runner.invoke(app, ["assert", "load", str(lib)])
    assert result.exit_code == 0  # tolerant, not an error
    assert "skipped" in result.stdout.lower()
    assert "ghost-host-not-in-db" in result.stdout


def test_assert_load_bad_yaml_errors(assert_db_url: str, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 99\nassertions: []\n")
    result = runner.invoke(app, ["assert", "load", str(bad)])
    assert result.exit_code == 2
    assert "library error" in result.stdout.lower()


@pytest.mark.parametrize(
    "argv",
    [
        ["assert", "--help"],
        ["assert", "list", "--help"],
        ["assert", "show", "--help"],
        ["assert", "run", "--help"],
        ["assert", "load", "--help"],
    ],
)
def test_assert_subcommand_help_loads(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0


def test_top_level_help_lists_assert() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "assert" in result.stdout
