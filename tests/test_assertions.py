"""AssertionEngine tests — verifier dispatch + finding lifecycle.

Tests use the same in-memory SQLite + session_scope pattern as the reconciler
tests. They seed observations + assertions directly, run the engine, and
assert on AssertionRun rows and finding lifecycle transitions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    AssertionKind,
    AssertionScope,
    AssertionStatus,
    FindingKind,
    FindingSeverity,
    FindingStatus,
    IntentTargetType,
    PrivilegeLevel,
)
from homelab_helper.db.models import (
    AssertionRun,
    ConfigurationAssertion,
    DiscoveryRun,
    Host,
    Observation,
    Probe,
    ReconciliationFinding,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.assertions import AssertionEngine


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


async def _seed_host(s, hostname: str = "bmax0") -> Host:
    h = Host(hostname=hostname, primary_ip="10.250.6.20")
    s.add(h)
    await s.flush()
    return h


async def _seed_observation(
    s,
    host: Host,
    key: str,
    value: Any,
    *,
    recorded_at: datetime | None = None,
) -> None:
    """Materialize a probe + run + observation directly."""
    probe = (await s.execute(select(Probe).where(Probe.name == "test-probe"))).scalar_one_or_none()
    if probe is None:
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
    obs = Observation(
        run_id=run.id,
        key=key,
        value=value,
        target_type=IntentTargetType.HOST,
        target_id=str(host.id),
    )
    if recorded_at is not None:
        obs.recorded_at = recorded_at
    s.add(obs)
    await s.flush()


def _make_assertion(
    name: str,
    host: Host,
    *,
    spec: dict[str, Any],
    severity: FindingSeverity = FindingSeverity.MEDIUM,
    kind: AssertionKind = AssertionKind.OBSERVATION_PREDICATE,
) -> ConfigurationAssertion:
    return ConfigurationAssertion(
        name=name,
        scope=AssertionScope.HOST,
        scope_target=str(host.id),
        description=f"{name} should hold on {host.hostname}",
        kind=kind,
        verifier_spec=spec,
        severity_on_fail=severity,
    )


# ---------------------------------------------------------------------------
# OBSERVATION_PREDICATE — verifier semantics
# ---------------------------------------------------------------------------


async def test_observation_predicate_passes_when_observed_matches(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(s, host, "host.cpu.cores", 8)
        a = _make_assertion("bmax0.cores_eq_8", host, spec={"key": "host.cpu.cores", "value": 8})
        s.add(a)
        await s.flush()

        result = await AssertionEngine().run_assertion(s, a)

    assert result.status == AssertionStatus.PASS
    assert result.evidence["observed"] == 8


async def test_observation_predicate_fails_when_observed_differs(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(s, host, "host.cpu.cores", 4)
        a = _make_assertion("bmax0.cores_eq_8", host, spec={"key": "host.cpu.cores", "value": 8})
        s.add(a)
        await s.flush()

        result = await AssertionEngine().run_assertion(s, a)

    assert result.status == AssertionStatus.FAIL
    assert result.evidence["observed"] == 4
    assert result.evidence["expected"] == 8


@pytest.mark.parametrize(
    ("predicate", "observed", "expected", "should_pass"),
    [
        ("gte", 8, 4, True),
        ("gte", 4, 8, False),
        ("gt", 8, 4, True),
        ("gt", 4, 4, False),
        ("lt", 4, 8, True),
        ("lt", 4, 4, False),
        ("lte", 4, 4, True),
        ("ne", 4, 8, True),
        ("ne", 8, 8, False),
        ("regex", "Ubuntu 24.04.1 LTS", r"^Ubuntu", True),
        ("regex", "Ubuntu 24.04.1 LTS", r"^Debian", False),
        ("contains", ["aes", "avx", "avx2"], "avx", True),
        ("contains", ["aes"], "avx", False),
    ],
)
async def test_observation_predicate_kinds(
    sessionmaker, predicate: str, observed: Any, expected: Any, should_pass: bool
) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(s, host, "host.test.k", observed)
        a = _make_assertion(
            f"pred-{predicate}",
            host,
            spec={"key": "host.test.k", "predicate": predicate, "value": expected},
        )
        s.add(a)
        await s.flush()

        result = await AssertionEngine().run_assertion(s, a)

    expected_status = AssertionStatus.PASS if should_pass else AssertionStatus.FAIL
    assert result.status == expected_status


async def test_observation_predicate_missing_observation_skips(sessionmaker) -> None:
    """No observation row → SKIP (we don't have evidence to fail)."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        a = _make_assertion("missing", host, spec={"key": "host.unobserved.key", "value": 1})
        s.add(a)
        await s.flush()

        result = await AssertionEngine().run_assertion(s, a)

    assert result.status == AssertionStatus.SKIP
    assert "no observation" in (result.error or "")


async def test_exists_predicate_fails_when_no_observation(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        a = _make_assertion(
            "presence",
            host,
            spec={"key": "host.cpu.cores", "predicate": "exists"},
        )
        s.add(a)
        await s.flush()

        result = await AssertionEngine().run_assertion(s, a)

    assert result.status == AssertionStatus.FAIL


async def test_exists_predicate_passes_when_observation_present(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(s, host, "host.cpu.cores", 8)
        a = _make_assertion(
            "presence",
            host,
            spec={"key": "host.cpu.cores", "predicate": "exists"},
        )
        s.add(a)
        await s.flush()

        result = await AssertionEngine().run_assertion(s, a)

    assert result.status == AssertionStatus.PASS


async def test_unknown_predicate_errors(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(s, host, "host.cpu.cores", 8)
        a = _make_assertion(
            "bad-pred",
            host,
            spec={"key": "host.cpu.cores", "predicate": "near-ish", "value": 8},
        )
        s.add(a)
        await s.flush()

        result = await AssertionEngine().run_assertion(s, a)

    assert result.status == AssertionStatus.ERROR
    assert "unknown predicate" in (result.error or "")


async def test_missing_key_in_spec_errors(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        a = _make_assertion("no-key", host, spec={"value": 1})
        s.add(a)
        await s.flush()

        result = await AssertionEngine().run_assertion(s, a)

    assert result.status == AssertionStatus.ERROR


async def test_latest_observation_wins(sessionmaker) -> None:
    """Old observation says 4, newer says 8; predicate eq 8 must PASS."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(
            s, host, "host.cpu.cores", 4, recorded_at=datetime(2026, 5, 1, tzinfo=UTC)
        )
        await _seed_observation(
            s, host, "host.cpu.cores", 8, recorded_at=datetime(2026, 6, 1, tzinfo=UTC)
        )
        a = _make_assertion("latest-wins", host, spec={"key": "host.cpu.cores", "value": 8})
        s.add(a)
        await s.flush()

        result = await AssertionEngine().run_assertion(s, a)

    assert result.status == AssertionStatus.PASS


# ---------------------------------------------------------------------------
# Stubbed verifier kinds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        AssertionKind.SSH_COMMAND,
        AssertionKind.HTTP_CHECK,
        AssertionKind.API_QUERY,
        AssertionKind.FILE_HASH,
    ],
)
async def test_other_verifier_kinds_skip_with_clear_reason(
    sessionmaker, kind: AssertionKind
) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        a = _make_assertion("stub", host, spec={}, kind=kind)
        s.add(a)
        await s.flush()

        result = await AssertionEngine().run_assertion(s, a)

    assert result.status == AssertionStatus.SKIP
    assert "not yet implemented" in (result.error or "")


# ---------------------------------------------------------------------------
# AssertionRun persistence
# ---------------------------------------------------------------------------


async def test_assertion_run_row_is_recorded(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(s, host, "host.cpu.cores", 8)
        a = _make_assertion("rec-row", host, spec={"key": "host.cpu.cores", "value": 8})
        s.add(a)
        await s.flush()
        await AssertionEngine().run_assertion(s, a)

    async with sessionmaker() as s:
        runs = (await s.execute(select(AssertionRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == AssertionStatus.PASS
        assert runs[0].evidence["observed"] == 8


async def test_run_all_runs_every_enabled_assertion(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(s, host, "host.cpu.cores", 8)
        a1 = _make_assertion("alpha", host, spec={"key": "host.cpu.cores", "value": 8})
        a2 = _make_assertion("beta", host, spec={"key": "host.cpu.cores", "value": 4})
        # Disabled assertion must be skipped entirely (no run row).
        a3 = _make_assertion("gamma-disabled", host, spec={"key": "host.cpu.cores", "value": 0})
        a3.enabled = False
        s.add_all([a1, a2, a3])
        await s.flush()

        results = await AssertionEngine().run_all(s)

    assert {r.assertion_name for r in results} == {"alpha", "beta"}
    by_name = {r.assertion_name: r for r in results}
    assert by_name["alpha"].status == AssertionStatus.PASS
    assert by_name["beta"].status == AssertionStatus.FAIL


# ---------------------------------------------------------------------------
# Finding lifecycle: open / dedup / resolve / reopen
# ---------------------------------------------------------------------------


async def test_fail_emits_config_drift_finding(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(s, host, "host.cpu.cores", 4)
        a = _make_assertion(
            "wants_8",
            host,
            spec={"key": "host.cpu.cores", "value": 8},
            severity=FindingSeverity.HIGH,
        )
        s.add(a)
        await s.flush()

        result = await AssertionEngine().run_assertion(s, a)

    assert result.status == AssertionStatus.FAIL
    assert result.finding_opened is True

    async with sessionmaker() as s:
        findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == FindingKind.CONFIG_DRIFT
        assert f.severity == FindingSeverity.HIGH
        assert "wants_8" in f.title
        # Affected list carries both the scope target and an assertion marker.
        targets = {(a["target_type"], a["target_id"]) for a in f.affected}
        assert ("host", str(host.id)) in targets
        assert ("assertion", "wants_8") in targets

        run = (await s.execute(select(AssertionRun))).scalar_one()
        assert run.finding_id == f.id


async def test_failing_repeatedly_does_not_duplicate(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(s, host, "host.cpu.cores", 4)
        a = _make_assertion("wants_8", host, spec={"key": "host.cpu.cores", "value": 8})
        s.add(a)
        await s.flush()
        eng = AssertionEngine()
        r1 = await eng.run_assertion(s, a)
        r2 = await eng.run_assertion(s, a)

    assert r1.finding_opened is True
    assert r2.finding_opened is False
    assert r2.finding_updated is True

    async with sessionmaker() as s:
        findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert len(findings) == 1
        assert findings[0].last_seen > findings[0].first_seen


async def test_pass_after_fail_resolves_the_finding(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(
            s, host, "host.cpu.cores", 4, recorded_at=datetime(2026, 5, 1, tzinfo=UTC)
        )
        a = _make_assertion("wants_8", host, spec={"key": "host.cpu.cores", "value": 8})
        s.add(a)
        await s.flush()
        eng = AssertionEngine()
        await eng.run_assertion(s, a)  # FAIL → opens finding

        # World changed: now reports 8 cores.
        await _seed_observation(
            s, host, "host.cpu.cores", 8, recorded_at=datetime(2026, 6, 1, tzinfo=UTC)
        )
        r2 = await eng.run_assertion(s, a)

    assert r2.status == AssertionStatus.PASS
    assert r2.finding_resolved is True

    async with sessionmaker() as s:
        findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert len(findings) == 1
        assert findings[0].status == FindingStatus.RESOLVED
        assert findings[0].resolved_at is not None


async def test_fail_after_resolve_reopens_same_fingerprint(sessionmaker) -> None:
    """Drift → fixed → drift again must reopen the same finding row."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        a = _make_assertion("wants_8", host, spec={"key": "host.cpu.cores", "value": 8})
        s.add(a)
        await s.flush()
        eng = AssertionEngine()

        await _seed_observation(
            s, host, "host.cpu.cores", 4, recorded_at=datetime(2026, 3, 1, tzinfo=UTC)
        )
        r1 = await eng.run_assertion(s, a)  # FAIL → open

        await _seed_observation(
            s, host, "host.cpu.cores", 8, recorded_at=datetime(2026, 4, 1, tzinfo=UTC)
        )
        await eng.run_assertion(s, a)  # PASS → resolve

        await _seed_observation(
            s, host, "host.cpu.cores", 4, recorded_at=datetime(2026, 5, 1, tzinfo=UTC)
        )
        r3 = await eng.run_assertion(s, a)  # FAIL → reopen

    assert r1.finding_opened is True
    assert r3.finding_opened is True  # reopened, not "updated"

    async with sessionmaker() as s:
        findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert len(findings) == 1  # one row, status flipped twice
        assert findings[0].status == FindingStatus.OPEN


async def test_pass_without_prior_finding_is_noop(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(s, host, "host.cpu.cores", 8)
        a = _make_assertion("clean", host, spec={"key": "host.cpu.cores", "value": 8})
        s.add(a)
        await s.flush()

        result = await AssertionEngine().run_assertion(s, a)

    assert result.status == AssertionStatus.PASS
    assert result.finding_resolved is False
    async with sessionmaker() as s:
        assert len((await s.execute(select(ReconciliationFinding))).scalars().all()) == 0


async def test_error_does_not_touch_findings(sessionmaker) -> None:
    """Verifier ERROR (e.g. bad spec) must not open or resolve findings."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(s, host, "host.cpu.cores", 4)
        a = _make_assertion("wants_8", host, spec={"key": "host.cpu.cores", "value": 8})
        s.add(a)
        await s.flush()
        eng = AssertionEngine()
        await eng.run_assertion(s, a)  # FAIL → open

        # Now break the spec — should ERROR.
        a.verifier_spec = {"key": "", "value": 8}
        await s.flush()
        r2 = await eng.run_assertion(s, a)

    assert r2.status == AssertionStatus.ERROR
    assert r2.finding_resolved is False

    async with sessionmaker() as s:
        findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert len(findings) == 1
        assert findings[0].status == FindingStatus.OPEN  # untouched


async def test_skip_does_not_touch_findings(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        await _seed_observation(s, host, "host.cpu.cores", 4)
        a = _make_assertion("wants_8", host, spec={"key": "host.cpu.cores", "value": 8})
        s.add(a)
        await s.flush()
        eng = AssertionEngine()
        await eng.run_assertion(s, a)  # FAIL → open

        # Now query a key that hasn't been observed.
        a.verifier_spec = {"key": "host.never.observed", "value": 1}
        await s.flush()
        r2 = await eng.run_assertion(s, a)

    assert r2.status == AssertionStatus.SKIP

    async with sessionmaker() as s:
        findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert findings[0].status == FindingStatus.OPEN
