"""Unit + replay tests for the Reconciler — host.identity slice.

The replay-style tests materialize Observation rows directly from a fixture
dict (no probe runs, no SSH) and feed them through the reconciler. This is
the seed of the dorktool replay-test fixture; it lives in-process today and
will migrate to a YAML fixture loader once the dorktool integration lands.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.db.models import DiscoveryRun, Host, Observation, Probe
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.reconciler import HostProjectionRule, Reconciler


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


async def _seed_host(s, hostname: str = "bmax-test") -> Host:
    h = Host(hostname=hostname, primary_ip="10.250.6.99")
    s.add(h)
    await s.flush()
    return h


async def _seed_run(s, host_id: uuid.UUID, probe_name: str = "host.identity") -> DiscoveryRun:
    # Probe.name is unique — reuse the row if a prior _seed_run already inserted it.
    probe = (await s.execute(select(Probe).where(Probe.name == probe_name))).scalar_one_or_none()
    if probe is None:
        probe = Probe(
            name=probe_name,
            version="0.1.0",
            module_path=f"test:{probe_name}",
            required_privilege=PrivilegeLevel.USER,
            produces_keys=[],
        )
        s.add(probe)
        await s.flush()
    run = DiscoveryRun(
        host_id=host_id,
        probe_id=probe.id,
        probe_name=probe.name,
        probe_version=probe.version,
        privilege_level=PrivilegeLevel.USER,
    )
    s.add(run)
    await s.flush()
    return run


async def _record_observations(
    s,
    run_id: uuid.UUID,
    host_id: uuid.UUID,
    facts: dict[str, Any],
    *,
    recorded_at: datetime | None = None,
) -> None:
    """Replay-style helper: write a key→value fixture into Observation rows."""
    for key, value in facts.items():
        obs = Observation(
            run_id=run_id,
            key=key,
            value=value,
            target_type=IntentTargetType.HOST,
            target_id=str(host_id),
        )
        if recorded_at is not None:
            obs.recorded_at = recorded_at
        s.add(obs)
    await s.flush()


# ---------------------------------------------------------------------------
# Core projection
# ---------------------------------------------------------------------------


async def test_projects_identity_observations_into_capabilities(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id)
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.identity.hostname": "bmax-test",
                "host.identity.kernel": "6.8.0-49-generic",
                "host.identity.machine_id": "abc123def456",
                "host.identity.os_id": "ubuntu",
                "host.identity.os_pretty_name": "Ubuntu 24.04.1 LTS",
                "host.identity.boot_time_unix": 1_700_000_000,
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 6
    assert result.changes == {
        "capabilities.observed_hostname": "bmax-test",
        "capabilities.kernel": "6.8.0-49-generic",
        "capabilities.machine_id": "abc123def456",
        "capabilities.os_id": "ubuntu",
        "capabilities.os_pretty_name": "Ubuntu 24.04.1 LTS",
        "capabilities.boot_time_unix": 1_700_000_000,
    }

    async with sessionmaker() as s:
        h = (await s.execute(select(Host).where(Host.id == host.id))).scalar_one()
        assert h.capabilities["kernel"] == "6.8.0-49-generic"
        assert h.capabilities["os_id"] == "ubuntu"
        assert h.capabilities["boot_time_unix"] == 1_700_000_000
        assert h.discovery_last_run is not None
        assert h.last_verified is not None


async def test_reconcile_is_idempotent(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id)
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.identity.kernel": "6.8.0-49-generic", "host.identity.os_id": "ubuntu"},
        )

        first = await Reconciler().reconcile_host(s, host.id)
        second = await Reconciler().reconcile_host(s, host.id)

    assert len(first.changes) == 2
    assert second.changes == {}
    assert second.observations_seen == 2


async def test_latest_observation_wins(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id)

        older = datetime(2026, 1, 1, tzinfo=UTC)
        newer = older + timedelta(days=30)

        await _record_observations(
            s, run.id, host.id, {"host.identity.kernel": "6.5.0-old"}, recorded_at=older
        )
        await _record_observations(
            s, run.id, host.id, {"host.identity.kernel": "6.8.0-new"}, recorded_at=newer
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 1
    assert result.changes == {"capabilities.kernel": "6.8.0-new"}


async def test_unknown_keys_are_ignored(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id)
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.identity.kernel": "6.8.0-49-generic",
                # Other-slice keys; the host-identity reconciler must ignore them.
                "host.cpu.model": "Intel i5-8260U",
                "host.storage.devices[0].model": "Samsung 980",
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 1
    assert result.changes == {"capabilities.kernel": "6.8.0-49-generic"}


async def test_no_observations_means_no_changes_and_no_freshness_bump(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        assert host.discovery_last_run is None
        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 0
    assert result.changes == {}

    async with sessionmaker() as s:
        h = (await s.execute(select(Host).where(Host.id == host.id))).scalar_one()
        # No observations ⇒ no probe ran ⇒ don't lie about freshness.
        assert h.discovery_last_run is None
        assert h.last_verified is None


async def test_missing_host_raises(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        with pytest.raises(ValueError, match="no Host"):
            await Reconciler().reconcile_host(s, uuid.uuid4())


# ---------------------------------------------------------------------------
# Rule registry sanity
# ---------------------------------------------------------------------------


def test_rule_requires_exactly_one_target() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        HostProjectionRule(key="x", attr="a", capability="c")
    with pytest.raises(ValueError, match="exactly one"):
        HostProjectionRule(key="x")


# ---------------------------------------------------------------------------
# Replay-style mini-fixture: a host walked through two probe runs
# ---------------------------------------------------------------------------


async def test_replay_two_runs_promotes_to_latest_state(sessionmaker) -> None:
    """A first probe run sets initial state; a later run updates kernel + boot_time.

    Asserts the reconciler tracks the freshest facts and only emits deltas for
    fields that actually changed.
    """
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s, hostname="bmax2")

        run1 = await _seed_run(s, host.id)
        t1 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        await _record_observations(
            s,
            run1.id,
            host.id,
            {
                "host.identity.hostname": "bmax2",
                "host.identity.kernel": "6.5.0-21-generic",
                "host.identity.os_id": "ubuntu",
                "host.identity.os_pretty_name": "Ubuntu 22.04.4 LTS",
                "host.identity.boot_time_unix": 1_714_500_000,
            },
            recorded_at=t1,
        )
        first = await Reconciler().reconcile_host(s, host.id)
        assert first.observations_seen == 5

        # 30 days later — kernel upgrade + reboot.
        run2 = await _seed_run(s, host.id)
        t2 = t1 + timedelta(days=30)
        await _record_observations(
            s,
            run2.id,
            host.id,
            {
                "host.identity.hostname": "bmax2",
                "host.identity.kernel": "6.8.0-49-generic",
                "host.identity.os_id": "ubuntu",
                "host.identity.os_pretty_name": "Ubuntu 24.04.1 LTS",
                "host.identity.boot_time_unix": 1_717_092_000,
            },
            recorded_at=t2,
        )
        second = await Reconciler().reconcile_host(s, host.id)

    # Only the three fields that actually changed should appear in deltas.
    assert second.changes == {
        "capabilities.kernel": "6.8.0-49-generic",
        "capabilities.os_pretty_name": "Ubuntu 24.04.1 LTS",
        "capabilities.boot_time_unix": 1_717_092_000,
    }
