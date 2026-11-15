"""Probe history is pruned past its horizon without losing what is load-bearing.

The reconciler's winning observation, an assertion's latest reading and the
schedule's last run per probe all survive, however old they are.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from homelab_helper.db.enums import Confidence, IntentTargetType, PrivilegeLevel
from homelab_helper.db.models import DiscoveryRun, Host, Observation
from homelab_helper.db.models import Probe as ProbeRow
from homelab_helper.db.session import session_scope
from homelab_helper.engine.reconciler import Reconciler
from homelab_helper.engine.retention import OBSERVATION_DAYS_ENV, RUN_DAYS_ENV, prune_history
from tests.test_node_update import sessionmaker_  # noqa: F401 - fixture

if TYPE_CHECKING:
    import uuid

NOW = datetime(2026, 6, 1, tzinfo=UTC)
KERNEL = "host.identity.kernel"


async def _host(s: Any) -> tuple[Host, ProbeRow]:
    host = Host(hostname="nas0")
    probe = ProbeRow(
        name="host.identity",
        version="0.1.0",
        module_path="t",
        required_privilege=PrivilegeLevel.USER,
        produces_keys=[],
    )
    s.add_all([host, probe])
    await s.flush()
    return host, probe


async def _reading(
    s: Any,
    host: Host,
    probe: ProbeRow,
    days_ago: int,
    value: str | None,
    *,
    confidence: Confidence = Confidence.VERIFIED,
    target_type: IntentTargetType | None = IntentTargetType.HOST,
) -> uuid.UUID:
    """One run ``days_ago`` days back, carrying one kernel reading unless ``value`` is None."""
    when = NOW - timedelta(days=days_ago)
    run = DiscoveryRun(
        host_id=host.id,
        probe_id=probe.id,
        probe_name=probe.name,
        probe_version="0.1.0",
        privilege_level=PrivilegeLevel.USER,
        started_at=when,
        success=value is not None,
    )
    s.add(run)
    await s.flush()
    if value is not None:
        s.add(
            Observation(
                run_id=run.id,
                key=KERNEL,
                value=value,
                confidence=confidence,
                recorded_at=when,
                target_type=target_type,
                target_id=str(host.id),
            )
        )
        await s.flush()
    return run.id


async def _values(s: Any) -> list[str]:
    rows = await s.execute(select(Observation.value).order_by(Observation.recorded_at))
    return [str(v) for v in rows.scalars()]


async def test_old_readings_go_and_the_newest_stays(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        host, probe = await _host(s)
        old = await _reading(s, host, probe, 300, "6.1")
        await _reading(s, host, probe, 200, "6.5")
        await _reading(s, host, probe, 10, "6.8")

        removed = await prune_history(s, now=NOW)

        assert removed == {"observations": 2, "runs": 2}
        assert await _values(s) == ["6.8"]
        assert await s.get(DiscoveryRun, old) is None


async def test_a_host_that_stopped_reporting_keeps_its_last_known_facts(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        host, probe = await _host(s)
        await _reading(s, host, probe, 400, "6.1")
        last = await _reading(s, host, probe, 300, "6.5")

        removed = await prune_history(s, now=NOW)

        assert removed == {"observations": 1, "runs": 1}
        assert await _values(s) == ["6.5"]
        assert await s.get(DiscoveryRun, last) is not None  # the schedule's cadence anchor
        await Reconciler().reconcile_host(s, host.id)
        assert host.capabilities["kernel"] == "6.5"


async def test_the_reconcilers_winner_survives_newer_lower_confidence_rows(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        host, probe = await _host(s)
        await _reading(s, host, probe, 300, "kernel-probe")
        await _reading(s, host, probe, 5, "from-proxmox", confidence=Confidence.INFERRED)

        await prune_history(s, now=NOW)

        assert await _values(s) == ["kernel-probe", "from-proxmox"]
        await Reconciler().reconcile_host(s, host.id)
        assert host.capabilities["kernel"] == "kernel-probe"


async def test_rows_without_a_target_type_are_grouped_not_kept_forever(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        host, probe = await _host(s)
        await _reading(s, host, probe, 300, "a", target_type=None)
        await _reading(s, host, probe, 200, "b", target_type=None)

        assert (await prune_history(s, now=NOW))["observations"] == 1
        assert await _values(s) == ["b"]


async def test_a_failed_run_with_no_readings_is_pruned_but_never_the_newest(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        host, probe = await _host(s)
        await _reading(s, host, probe, 300, None)
        newest = await _reading(s, host, probe, 200, None)

        assert (await prune_history(s, now=NOW))["runs"] == 1
        assert [r.id for r in (await s.execute(select(DiscoveryRun))).scalars()] == [newest]


@pytest.mark.parametrize("var", [OBSERVATION_DAYS_ENV, RUN_DAYS_ENV])
async def test_zero_turns_a_half_off(sessionmaker_, monkeypatch, var: str) -> None:  # noqa: F811
    monkeypatch.setenv(var, "0")
    async with session_scope(sessionmaker_) as s:
        host, probe = await _host(s)
        await _reading(s, host, probe, 300, "6.1")
        await _reading(s, host, probe, 10, "6.8")

        removed = await prune_history(s, now=NOW)

        half = "observations" if var == OBSERVATION_DAYS_ENV else "runs"
        assert removed[half] == 0


async def test_the_horizon_never_cuts_into_smart_history(sessionmaker_, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setenv(OBSERVATION_DAYS_ENV, "7")
    async with session_scope(sessionmaker_) as s:
        host, probe = await _host(s)
        await _reading(s, host, probe, 80, "6.1")
        await _reading(s, host, probe, 10, "6.8")

        assert (await prune_history(s, now=NOW))["observations"] == 0
