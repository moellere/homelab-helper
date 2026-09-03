"""Auto-escalation policy tests — the ladder rises slowly and falls instantly.

The load-bearing assertions mirror `docs/architecture.md`: one rung per N clean
approvals, eligibility limited to reversible + low-blast cells, one bad outcome
straight to PROPOSE with probation, and probation clearable only by an explicit
operator grant.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import AutonomyLevel, TrustDomain
from homelab_helper.db.models import CellTrust, Domain, TrustHistory
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.escalation import (
    PROMOTION_STREAK,
    is_promotable,
    next_level,
    record_bad_outcome,
    record_clean_outcome,
    record_rejection,
)
from homelab_helper.engine.trust import grant_cell, seed_domains

CELL = {"domain": TrustDomain.CONTAINERS, "action_kind": "restart", "blast_radius": "single-host"}


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


async def get_cell(session, **cell) -> CellTrust:
    return (
        await session.execute(
            select(CellTrust).where(
                CellTrust.domain == cell["domain"],
                CellTrust.action_kind == cell["action_kind"],
                CellTrust.blast_radius == cell["blast_radius"],
            )
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------


def test_eligibility_needs_reversible_action_and_low_blast() -> None:
    assert is_promotable("restart", "single-host")
    assert is_promotable("start", "metadata-only")
    assert not is_promotable("restart", "cluster"), "wide blast radius is grant-only"
    assert not is_promotable("recreate", "single-host"), "irreversible kinds are grant-only"
    assert not is_promotable("restart", "everything")


def test_ladder_is_one_rung_at_a_time() -> None:
    assert next_level(AutonomyLevel.PROPOSE) is AutonomyLevel.CONFIRM
    assert next_level(AutonomyLevel.CONFIRM) is AutonomyLevel.AUTONOMOUS
    assert next_level(AutonomyLevel.AUTONOMOUS) is None
    assert next_level(AutonomyLevel.BLOCK) is None, "BLOCK is off the ladder entirely"


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


async def test_promotes_exactly_at_the_streak_and_resets(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        for i in range(PROMOTION_STREAK - 1):
            result = await record_clean_outcome(s, **CELL, actor="enoch")
            assert result.event == "streak"
            assert result.level is AutonomyLevel.PROPOSE, f"no promotion at {i + 1} approvals"

        result = await record_clean_outcome(s, **CELL, actor="enoch")
        assert result.promoted
        assert result.previous_level is AutonomyLevel.PROPOSE
        assert result.level is AutonomyLevel.CONFIRM
        assert result.clean_streak == 0, "the streak resets at each rung"

        cell = await get_cell(s, **CELL)
        assert cell.level is AutonomyLevel.CONFIRM
        assert cell.granted_by is None, "auto-promotion is not an operator grant"


async def test_second_rung_needs_another_full_streak(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        for _ in range(PROMOTION_STREAK * 2):
            result = await record_clean_outcome(s, **CELL, actor="enoch")
        assert result.level is AutonomyLevel.AUTONOMOUS
        assert result.promoted

        top = await record_clean_outcome(s, **CELL, actor="enoch")
        for _ in range(PROMOTION_STREAK):
            top = await record_clean_outcome(s, **CELL, actor="enoch")
        assert top.level is AutonomyLevel.AUTONOMOUS
        assert not top.promoted, "AUTONOMOUS is the top of the ladder"


async def test_ineligible_cell_accrues_streak_but_never_promotes(sessionmaker) -> None:
    wide = {**CELL, "blast_radius": "cluster"}
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        for _ in range(PROMOTION_STREAK * 2):
            result = await record_clean_outcome(s, **wide, actor="enoch")
        assert result.event == "noop"
        assert result.level is AutonomyLevel.PROPOSE
        assert "not auto-promotable" in result.reason
        assert (await get_cell(s, **wide)).clean_streak >= PROMOTION_STREAK


async def test_irreversible_action_kind_never_promotes(sessionmaker) -> None:
    destructive = {**CELL, "action_kind": "recreate"}
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        for _ in range(PROMOTION_STREAK):
            result = await record_clean_outcome(s, **destructive, actor="enoch")
        assert result.level is AutonomyLevel.PROPOSE
        assert result.event == "noop"


async def test_promotion_never_exceeds_domain_max(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        domain = await s.get(Domain, TrustDomain.CONTAINERS)
        domain.max_level = AutonomyLevel.CONFIRM
        await s.flush()

        for _ in range(PROMOTION_STREAK):
            await record_clean_outcome(s, **CELL, actor="enoch")
        for _ in range(PROMOTION_STREAK):
            result = await record_clean_outcome(s, **CELL, actor="enoch")

        assert result.level is AutonomyLevel.CONFIRM
        assert "exceeds domain" in result.reason


async def test_absolute_domain_never_auto_promotes(sessionmaker) -> None:
    secrets = {**CELL, "domain": TrustDomain.SECRETS}
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        for _ in range(PROMOTION_STREAK * 2):
            result = await record_clean_outcome(s, **secrets, actor="enoch")
        assert result.level is AutonomyLevel.PROPOSE
        assert "exceeds domain" in result.reason or "absolute" in result.reason


async def test_promotion_writes_trust_history(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        for _ in range(PROMOTION_STREAK):
            await record_clean_outcome(s, **CELL, actor="enoch")
        rows = (
            (await s.execute(select(TrustHistory).where(TrustHistory.event == "auto-promote")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].detail["from"] == "propose"
        assert rows[0].detail["to"] == "confirm"


# ---------------------------------------------------------------------------
# Demotion — trust evaporates instantly
# ---------------------------------------------------------------------------


async def test_one_bad_outcome_demotes_to_propose_with_probation(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        for _ in range(PROMOTION_STREAK * 2):
            await record_clean_outcome(s, **CELL, actor="enoch")
        assert (await get_cell(s, **CELL)).level is AutonomyLevel.AUTONOMOUS

        result = await record_bad_outcome(s, **CELL, actor="enoch", reason="guest never came back")
        assert result.demoted
        assert result.previous_level is AutonomyLevel.AUTONOMOUS
        assert result.level is AutonomyLevel.PROPOSE, "demotion goes to the floor, not one rung"
        assert result.on_probation

        cell = await get_cell(s, **CELL)
        assert cell.clean_streak == 0
        assert cell.on_probation


async def test_demotion_applies_to_explicitly_granted_cells(sessionmaker) -> None:
    """A grant is not a shield — the one bad outcome still drops the floor."""
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(s, *CELL.values(), AutonomyLevel.AUTONOMOUS, actor="enoch")
        result = await record_bad_outcome(s, **CELL, actor="enoch", reason="boom")
        assert result.level is AutonomyLevel.PROPOSE
        assert (await get_cell(s, **CELL)).granted_by is None


async def test_probation_blocks_promotion_until_a_grant_clears_it(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await record_bad_outcome(s, **CELL, actor="enoch", reason="boom")

        for _ in range(PROMOTION_STREAK * 2):
            result = await record_clean_outcome(s, **CELL, actor="enoch")
        assert result.level is AutonomyLevel.PROPOSE
        assert "probation" in result.reason

        await grant_cell(s, *CELL.values(), AutonomyLevel.CONFIRM, actor="enoch")
        assert not (await get_cell(s, **CELL)).on_probation

        for _ in range(PROMOTION_STREAK):
            result = await record_clean_outcome(s, **CELL, actor="enoch")
        assert result.promoted
        assert result.level is AutonomyLevel.AUTONOMOUS


async def test_probation_banks_nothing(sessionmaker) -> None:
    """Clean runs during probation must not accumulate credit to cash later."""
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await record_bad_outcome(s, **CELL, actor="enoch", reason="boom")
        for _ in range(PROMOTION_STREAK * 3):
            result = await record_clean_outcome(s, **CELL, actor="enoch")
            assert result.clean_streak == 0
        assert (await get_cell(s, **CELL)).clean_streak == 0

        await grant_cell(s, *CELL.values(), AutonomyLevel.CONFIRM, actor="enoch")
        first = await record_clean_outcome(s, **CELL, actor="enoch")
        assert first.clean_streak == 1, "the clock starts at zero after the grant"
        assert not first.promoted


async def test_blocked_cell_holds_at_the_threshold(sessionmaker) -> None:
    """An ineligible cell holds at N, so lifting a block never backdates credit."""
    wide = {**CELL, "blast_radius": "cluster"}
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        for _ in range(PROMOTION_STREAK * 4):
            await record_clean_outcome(s, **wide, actor="enoch")
        assert (await get_cell(s, **wide)).clean_streak == PROMOTION_STREAK


async def test_demotion_writes_trust_history_with_cause(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await record_bad_outcome(s, **CELL, actor="enoch", reason="Proxmox 500")
        row = (
            (await s.execute(select(TrustHistory).where(TrustHistory.event == "demote")))
            .scalars()
            .one()
        )
        assert row.detail["cause"] == "Proxmox 500"
        assert row.detail["to"] == "propose"


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------


async def test_rejection_breaks_the_streak_without_demoting(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(s, *CELL.values(), AutonomyLevel.CONFIRM, actor="enoch")
        for _ in range(PROMOTION_STREAK - 1):
            await record_clean_outcome(s, **CELL, actor="enoch")

        result = await record_rejection(s, **CELL)
        assert result.event == "streak-reset"
        assert result.level is AutonomyLevel.CONFIRM, "rejection never lowers the floor"

        cell = await get_cell(s, **CELL)
        assert cell.clean_streak == 0
        assert not cell.on_probation
        assert (
            await s.execute(select(TrustHistory).where(TrustHistory.event == "streak-reset"))
        ).scalars().all() == [], "the audit spine is for authority changes only"


async def test_rejection_after_reset_needs_a_full_streak_again(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        for _ in range(PROMOTION_STREAK - 1):
            await record_clean_outcome(s, **CELL, actor="enoch")
        await record_rejection(s, **CELL)
        result = await record_clean_outcome(s, **CELL, actor="enoch")
        assert result.event == "streak"
        assert result.clean_streak == 1
        assert result.level is AutonomyLevel.PROPOSE
