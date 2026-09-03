"""Elevation windows, the kill switch, and per-host boundaries (P6-AC5/AC6).

The load-bearing assertions: a window is scoped, expiring and never
auto-renewing; it lifts the soft-hard floors and *only* those; absolute floors
(the `secrets` domain, a boundary marked absolute) reject every window; and
the kill switch closes everything at once, with in-flight work halting at the
executor's pre-dispatch checkpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import AutonomyLevel, TrustDomain
from homelab_helper.db.models import ElevationWindow, Host, TrustHistory
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.trust import (
    MAX_WINDOW_MINUTES,
    ActionRequest,
    WindowError,
    decide,
    grant_cell,
    kill_switch,
    load_trust_context,
    open_window,
    open_windows,
    revoke_window,
    seed_domains,
    set_boundary,
    window_is_open,
)

ACTION = ActionRequest(
    domain=TrustDomain.STORAGE,
    action_kind="restart",
    blast_radius="single-host",
    hostnames=("node2",),
    rollback_verified=False,
)


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


async def seeded(session, *, hostname: str = "node2") -> Host:
    await seed_domains(session)
    host = Host(hostname=hostname)
    session.add(host)
    await session.flush()
    return host


# ---------------------------------------------------------------------------
# Opening: scoped, bounded, logged
# ---------------------------------------------------------------------------


async def test_open_window_records_scope_and_expiry(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        w = await open_window(
            s,
            reason="migrating the array",
            minutes=60,
            actor="enoch",
            domains=[TrustDomain.STORAGE],
            hosts=["node2"],
        )
        assert w.scope["domains"] == ["storage"]
        assert w.scope["hosts"] == ["node2"]
        assert 59 <= (w.expires_at - w.opened_at).total_seconds() / 60 <= 61

        event = (
            (await s.execute(select(TrustHistory).where(TrustHistory.event == "window-open")))
            .scalars()
            .one()
        )
        assert event.detail["reason"] == "migrating the array"
        assert event.window_id == w.id


async def test_blanket_window_is_refused(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        with pytest.raises(WindowError, match="blanket"):
            await open_window(s, reason="just in case", minutes=60, actor="enoch")


async def test_window_duration_is_capped(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        with pytest.raises(WindowError, match="duration"):
            await open_window(
                s,
                reason="forever",
                minutes=MAX_WINDOW_MINUTES + 1,
                actor="enoch",
                hosts=["node2"],
            )


async def test_window_needs_a_reason(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        with pytest.raises(WindowError, match="reason"):
            await open_window(s, reason="   ", minutes=10, actor="enoch", hosts=["node2"])


async def test_absolute_domain_rejects_a_window(sessionmaker) -> None:
    """AC6: secrets is unreachable by any runtime gesture."""
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        with pytest.raises(WindowError, match="absolute"):
            await open_window(
                s,
                reason="rotate a key",
                minutes=30,
                actor="enoch",
                domains=[TrustDomain.SECRETS],
            )


# ---------------------------------------------------------------------------
# Effect on decide()
# ---------------------------------------------------------------------------


async def test_window_lifts_the_verified_rollback_floor(sessionmaker) -> None:
    """AC5: autonomous crosses the reversibility floor while the window is open."""
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        await grant_cell(
            s,
            TrustDomain.STORAGE,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )

        before = decide(ACTION, await load_trust_context(s, ACTION))
        assert before.level is AutonomyLevel.CONFIRM, "no window: degrades on unverified rollback"

        w = await open_window(
            s, reason="array work", minutes=60, actor="enoch", domains=[TrustDomain.STORAGE]
        )
        during = decide(ACTION, await load_trust_context(s, ACTION))
        assert during.level is AutonomyLevel.AUTONOMOUS
        assert during.window_id == str(w.id)
        assert any("best-effort snapshot" in r for r in during.reasons)


async def test_absolute_boundary_is_window_proof(sessionmaker) -> None:
    """AC6: a host marked absolute rejects the window's lift."""
    async with session_scope(sessionmaker) as s:
        host = await seeded(s)
        await grant_cell(
            s,
            TrustDomain.STORAGE,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )
        await set_boundary(
            s, host, AutonomyLevel.PROPOSE, absolute=True, actor="enoch", notes="the NAS"
        )
        await open_window(
            s, reason="array work", minutes=60, actor="enoch", domains=[TrustDomain.STORAGE]
        )

        decision = decide(ACTION, await load_trust_context(s, ACTION))
        assert decision.level is AutonomyLevel.PROPOSE
        assert any("window-proof" in r for r in decision.reasons)


async def test_non_absolute_boundary_lifts_under_a_window(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await seeded(s)
        await grant_cell(
            s,
            TrustDomain.STORAGE,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )
        await set_boundary(s, host, AutonomyLevel.PROPOSE, absolute=False, actor="enoch")

        capped = decide(ACTION, await load_trust_context(s, ACTION))
        assert capped.level is AutonomyLevel.PROPOSE

        await open_window(
            s, reason="array work", minutes=60, actor="enoch", domains=[TrustDomain.STORAGE]
        )
        lifted = decide(ACTION, await load_trust_context(s, ACTION))
        assert lifted.level is AutonomyLevel.AUTONOMOUS


async def test_window_out_of_scope_does_not_apply(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        await grant_cell(
            s,
            TrustDomain.STORAGE,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )
        await open_window(
            s, reason="dns work", minutes=60, actor="enoch", domains=[TrustDomain.DNS]
        )
        decision = decide(ACTION, await load_trust_context(s, ACTION))
        assert decision.level is AutonomyLevel.CONFIRM
        assert decision.window_id is None


async def test_expired_window_stops_applying(sessionmaker) -> None:
    """Hard expiry, no auto-renew."""
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        await grant_cell(
            s,
            TrustDomain.STORAGE,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )
        w = await open_window(
            s, reason="array work", minutes=60, actor="enoch", domains=[TrustDomain.STORAGE]
        )
        w.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await s.flush()

        decision = decide(ACTION, await load_trust_context(s, ACTION))
        assert decision.level is AutonomyLevel.CONFIRM
        assert await open_windows(s) == []
        assert not await window_is_open(s, str(w.id))


# ---------------------------------------------------------------------------
# Revoking and the kill switch
# ---------------------------------------------------------------------------


async def test_revoke_closes_one_window_and_logs_it(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        w = await open_window(
            s, reason="array work", minutes=60, actor="enoch", domains=[TrustDomain.STORAGE]
        )
        assert await revoke_window(s, w, actor="enoch")
        assert not await revoke_window(s, w, actor="enoch"), "closing twice is a no-op"

        event = (
            (await s.execute(select(TrustHistory).where(TrustHistory.event == "window-revoke")))
            .scalars()
            .one()
        )
        assert event.detail["cause"] == "revoked"
        assert not await window_is_open(s, str(w.id))


async def test_kill_switch_closes_every_open_window(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        for scope in (TrustDomain.STORAGE, TrustDomain.DNS, TrustDomain.CONTAINERS):
            await open_window(
                s, reason=f"{scope.value} work", minutes=60, actor="enoch", domains=[scope]
            )
        already = await open_window(
            s, reason="done already", minutes=60, actor="enoch", hosts=["node2"]
        )
        await revoke_window(s, already, actor="enoch")

        closed = await kill_switch(s, actor="enoch")
        assert closed == 3, "only the still-open ones are counted"
        assert await open_windows(s) == []

        causes = [
            h.detail.get("cause")
            for h in (
                (await s.execute(select(TrustHistory).where(TrustHistory.event == "window-revoke")))
                .scalars()
                .all()
            )
        ]
        assert causes.count("kill-switch") == 3


async def test_kill_switch_on_a_quiet_system_is_a_noop(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        assert await kill_switch(s, actor="enoch") == 0


async def test_window_is_open_rejects_garbage_ids(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        assert not await window_is_open(s, "not-a-uuid")


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


async def test_set_boundary_is_idempotent_and_logged(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await seeded(s)
        first = await set_boundary(s, host, AutonomyLevel.CONFIRM, absolute=False, actor="enoch")
        second = await set_boundary(s, host, AutonomyLevel.PROPOSE, absolute=True, actor="enoch")
        assert first.id == second.id, "one boundary per host, updated in place"
        assert second.max_agent_authority is AutonomyLevel.PROPOSE
        assert second.absolute

        events = (
            (await s.execute(select(TrustHistory).where(TrustHistory.event == "boundary-set")))
            .scalars()
            .all()
        )
        assert len(events) == 2
        assert events[-1].detail["hostname"] == "node2"


async def test_windows_are_listed_only_while_live(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        live = await open_window(s, reason="live", minutes=60, actor="enoch", hosts=["node2"])
        stale = await open_window(s, reason="stale", minutes=60, actor="enoch", hosts=["node2"])
        stale.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await s.flush()

        ids = {w.id for w in await open_windows(s)}
        assert ids == {live.id}


async def test_open_windows_ignores_naive_expiry_timezones(sessionmaker) -> None:
    """SQLite hands back naive datetimes; the helpers must not trip on that."""
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        w = await open_window(s, reason="live", minutes=60, actor="enoch", hosts=["node2"])
        w.expires_at = w.expires_at.replace(tzinfo=None)
        await s.flush()

        assert [x.id for x in await open_windows(s)] == [w.id]
        assert await window_is_open(s, str(w.id))


async def test_stored_window_round_trips_through_the_db(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seeded(s)
        w = await open_window(
            s,
            reason="array work",
            minutes=30,
            actor="enoch",
            cells=["storage/restart/single-host"],
        )
        window_id = w.id

    async with sessionmaker() as s:
        stored = (await s.execute(select(ElevationWindow))).scalar_one()
        assert stored.id == window_id
        assert stored.scope["cells"] == ["storage/restart/single-host"]
