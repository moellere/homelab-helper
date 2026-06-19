"""Tests for the YAML assertion-library loader + the shipped starter pack."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    AssertionKind,
    AssertionScope,
    FindingSeverity,
)
from homelab_helper.db.models import ConfigurationAssertion, Host
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.assertion_library import (
    LibraryError,
    load_library,
    parse_library_yaml,
)

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_STARTER_LIBRARY = _FIXTURES / "assertion-library-starter.yaml"


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


async def _seed_hosts(s, *names: str) -> dict[str, Host]:
    out: dict[str, Host] = {}
    for n in names:
        h = Host(hostname=n)
        s.add(h)
        await s.flush()
        out[n] = h
    return out


# ---------------------------------------------------------------------------
# parse_library_yaml — pure parsing without DB
# ---------------------------------------------------------------------------


def test_parse_rejects_non_mapping_root() -> None:
    with pytest.raises(LibraryError, match="top-level must be a mapping"):
        parse_library_yaml("- a\n- b\n")


def test_parse_rejects_wrong_version() -> None:
    with pytest.raises(LibraryError, match="unsupported library version"):
        parse_library_yaml("version: 99\nassertions: []\n")


def test_parse_rejects_assertions_not_list() -> None:
    with pytest.raises(LibraryError, match="'assertions' must be a list"):
        parse_library_yaml("version: 1\nassertions: not-a-list\n")


def test_parse_rejects_entry_not_mapping() -> None:
    with pytest.raises(LibraryError, match=r"assertions\[0\] must be a mapping"):
        parse_library_yaml("version: 1\nassertions:\n  - 'not a dict'\n")


def test_parse_accepts_empty_assertion_list() -> None:
    entries = parse_library_yaml("version: 1\nassertions: []\n")
    assert entries == []


def test_parse_returns_raw_entries_unchanged() -> None:
    text = """
    version: 1
    assertions:
      - name: a
        verifier_spec: {key: x, predicate: eq, value: 1}
    """
    entries = parse_library_yaml(text)
    assert len(entries) == 1
    assert entries[0]["name"] == "a"


# ---------------------------------------------------------------------------
# load_library — DB upsert semantics
# ---------------------------------------------------------------------------


_MINIMAL_YAML = """
version: 1
assertions:
  - name: myhost.has_cpu
    host: myhost
    description: myhost should have cpu observation
    severity_on_fail: medium
    verifier_spec:
      key: host.cpu.cores
      predicate: exists
"""


async def test_load_creates_assertion_when_host_exists(sessionmaker, tmp_path: Path) -> None:
    async with session_scope(sessionmaker) as s:
        hosts = await _seed_hosts(s, "myhost")
        path = tmp_path / "lib.yaml"
        path.write_text(_MINIMAL_YAML)

        result = await load_library(s, path)

    assert result.created == ["myhost.has_cpu"]
    assert result.skipped == []

    async with sessionmaker() as s:
        a = (
            await s.execute(
                select(ConfigurationAssertion).where(
                    ConfigurationAssertion.name == "myhost.has_cpu"
                )
            )
        ).scalar_one()
        assert a.scope == AssertionScope.HOST
        assert a.scope_target == str(hosts["myhost"].id)
        assert a.kind == AssertionKind.OBSERVATION_PREDICATE
        assert a.severity_on_fail == FindingSeverity.MEDIUM
        assert a.verifier_spec == {
            "key": "host.cpu.cores",
            "predicate": "exists",
        }


async def test_load_skips_when_host_missing(sessionmaker, tmp_path: Path) -> None:
    async with session_scope(sessionmaker) as s:
        path = tmp_path / "lib.yaml"
        path.write_text(_MINIMAL_YAML)

        result = await load_library(s, path)

    assert result.created == []
    assert result.skipped == [
        ("myhost.has_cpu", "host 'myhost' not in harness DB"),
    ]

    async with sessionmaker() as s:
        rows = (await s.execute(select(ConfigurationAssertion))).scalars().all()
        assert rows == []


async def test_load_is_idempotent_unchanged_on_second_call(sessionmaker, tmp_path: Path) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_hosts(s, "myhost")
        path = tmp_path / "lib.yaml"
        path.write_text(_MINIMAL_YAML)
        first = await load_library(s, path)
        second = await load_library(s, path)

    assert first.created == ["myhost.has_cpu"]
    assert second.created == []
    assert second.updated == []
    assert second.unchanged == ["myhost.has_cpu"]


async def test_load_updates_changed_entry(sessionmaker, tmp_path: Path) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_hosts(s, "myhost")
        path = tmp_path / "lib.yaml"
        path.write_text(_MINIMAL_YAML)
        await load_library(s, path)

        # Same name, different severity → should update.
        path.write_text(_MINIMAL_YAML.replace("severity_on_fail: medium", "severity_on_fail: high"))
        result = await load_library(s, path)

    assert result.updated == ["myhost.has_cpu"]
    assert result.created == []

    async with sessionmaker() as s:
        a = (await s.execute(select(ConfigurationAssertion))).scalar_one()
        assert a.severity_on_fail == FindingSeverity.HIGH


async def test_dry_run_does_not_write(sessionmaker, tmp_path: Path) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_hosts(s, "myhost")
        path = tmp_path / "lib.yaml"
        path.write_text(_MINIMAL_YAML)

        result = await load_library(s, path, dry_run=True)

    assert result.created == ["myhost.has_cpu"]

    async with sessionmaker() as s:
        rows = (await s.execute(select(ConfigurationAssertion))).scalars().all()
        assert rows == []  # nothing written despite the "created" report


async def test_dry_run_followed_by_real_run_creates_same_rows(sessionmaker, tmp_path: Path) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_hosts(s, "myhost")
        path = tmp_path / "lib.yaml"
        path.write_text(_MINIMAL_YAML)

        dry = await load_library(s, path, dry_run=True)
        real = await load_library(s, path)

    assert dry.created == real.created  # same plan executed


@pytest.mark.parametrize(
    ("missing_field", "expected_substr"),
    [
        ("name", "missing 'name'"),
        ("description", "missing 'description'"),
        ("verifier_spec", "missing or empty 'verifier_spec'"),
    ],
)
async def test_load_skips_entries_with_missing_required_field(
    sessionmaker, tmp_path: Path, missing_field: str, expected_substr: str
) -> None:
    """Each required field, when absent, produces a skip with a clear reason."""
    base = {
        "name": "x.y",
        "host": "myhost",
        "description": "desc",
        "severity_on_fail": "medium",
        "verifier_spec": {"key": "host.cpu.cores", "predicate": "exists"},
    }
    base.pop(missing_field)
    import yaml as _yaml

    payload = {"version": 1, "assertions": [base]}
    path = tmp_path / "lib.yaml"
    path.write_text(_yaml.safe_dump(payload))

    async with session_scope(sessionmaker) as s:
        await _seed_hosts(s, "myhost")
        result = await load_library(s, path)

    assert any(expected_substr in reason for _, reason in result.skipped)


async def test_load_skips_on_unknown_enum_values(sessionmaker, tmp_path: Path) -> None:
    path = tmp_path / "lib.yaml"
    path.write_text(
        """
version: 1
assertions:
  - name: bad.severity
    host: myhost
    description: bad severity
    severity_on_fail: imaginary
    verifier_spec: {key: x, predicate: eq, value: 1}
  - name: bad.kind
    host: myhost
    description: bad kind
    kind: psychic-link
    verifier_spec: {key: x, predicate: eq, value: 1}
"""
    )

    async with session_scope(sessionmaker) as s:
        await _seed_hosts(s, "myhost")
        result = await load_library(s, path)

    reasons = dict(result.skipped)
    assert "unknown severity_on_fail" in reasons["bad.severity"]
    assert "unknown kind" in reasons["bad.kind"]


async def test_global_scope_works_without_host(sessionmaker, tmp_path: Path) -> None:
    path = tmp_path / "lib.yaml"
    path.write_text(
        """
version: 1
assertions:
  - name: global.fleet_baseline
    description: a global-scope example
    scope: global
    verifier_spec: {key: x, predicate: exists}
"""
    )

    async with session_scope(sessionmaker) as s:
        result = await load_library(s, path)

    assert result.created == ["global.fleet_baseline"]
    async with sessionmaker() as s:
        a = (await s.execute(select(ConfigurationAssertion))).scalar_one()
        assert a.scope == AssertionScope.GLOBAL
        assert a.scope_target is None


# ---------------------------------------------------------------------------
# The shipped starter library — schema correctness + load against a real fleet
# ---------------------------------------------------------------------------


def test_starter_library_file_exists() -> None:
    assert _STARTER_LIBRARY.exists(), (
        f"expected starter library at {_STARTER_LIBRARY}; ship it from fixtures/"
    )


def test_starter_library_parses_cleanly() -> None:
    entries = parse_library_yaml(_STARTER_LIBRARY.read_text())
    assert len(entries) >= 10, "starter pack should ship at least ten assertions"
    names = [e["name"] for e in entries]
    assert len(names) == len(set(names)), "starter assertion names must be unique"


async def test_starter_library_loads_when_host_present(sessionmaker) -> None:
    """Loading the shipped starter against a fleet that has `myhost` works."""
    async with session_scope(sessionmaker) as s:
        await _seed_hosts(s, "myhost")
        result = await load_library(s, _STARTER_LIBRARY)

    assert len(result.created) >= 10
    assert result.skipped == []


async def test_starter_library_skips_all_when_host_absent(sessionmaker) -> None:
    """No fleet → every entry skips with a clean reason; nothing crashes."""
    async with session_scope(sessionmaker) as s:
        result = await load_library(s, _STARTER_LIBRARY)

    assert result.created == []
    assert all("not in harness DB" in r for _, r in result.skipped)
    assert len(result.skipped) >= 10


async def test_starter_library_round_trips_through_engine(sessionmaker) -> None:
    """End-to-end: load the starter, seed minimal observations, run all assertions.

    Doesn't assert PASS/FAIL counts (the starter has stringent baselines); just
    proves the engine accepts every loaded verifier_spec and produces a valid
    AssertionStatus for each, with no ERRORs from malformed spec.
    """
    from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
    from homelab_helper.db.models import DiscoveryRun, Observation, Probe
    from homelab_helper.engine.assertions import AssertionEngine

    async with session_scope(sessionmaker) as s:
        hosts = await _seed_hosts(s, "myhost")
        await load_library(s, _STARTER_LIBRARY)

        probe = Probe(
            name="seed",
            version="0.1.0",
            module_path="t",
            required_privilege=PrivilegeLevel.USER,
            produces_keys=[],
        )
        s.add(probe)
        await s.flush()
        run = DiscoveryRun(
            host_id=hosts["myhost"].id,
            probe_id=probe.id,
            probe_name="seed",
            probe_version="0.1.0",
            privilege_level=PrivilegeLevel.USER,
        )
        s.add(run)
        await s.flush()
        for key, value in [
            ("host.cpu.cores", 8),
            ("host.cpu.interesting_flags", ["aes", "avx", "avx2"]),
            ("host.cpu.architecture", "x86_64"),
            ("host.memory.mem_total_bytes", 32 * 1024**3),
            ("host.storage.disk_count", 2),
            ("host.network.interface_count", 3),
            ("host.identity.kernel", "6.8.0-49-generic"),
            ("host.identity.os_id", "ubuntu"),
        ]:
            s.add(
                Observation(
                    run_id=run.id,
                    key=key,
                    value=value,
                    target_type=IntentTargetType.HOST,
                    target_id=str(hosts["myhost"].id),
                )
            )
        await s.flush()

        results = await AssertionEngine().run_all(s)

    from homelab_helper.db.enums import AssertionStatus

    # Every assertion produced a defined status — no ERRORs from bad spec.
    assert all(r.status != AssertionStatus.ERROR for r in results), (
        f"ERROR results from starter library: "
        f"{[(r.assertion_name, r.error) for r in results if r.status == AssertionStatus.ERROR]}"
    )
    # With a well-equipped host seeded, MOST should pass; we don't pin the
    # exact count so the starter pack can evolve without breaking this test.
    passes = sum(1 for r in results if r.status == AssertionStatus.PASS)
    assert passes >= 8, f"expected most baselines to pass on a well-equipped host, got {passes}"
