"""Per-action owner override — crosses the soft-hard floors and nothing else.

From `docs/architecture.md`: override and elevation window "both cross
*soft-hard* floors only — never an absolute floor — and both are owner-only,
interactive, high-friction, and logged. Neither is ever available to an agent,
and neither is ever self-invoked by autonomous execution."
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import AutonomyLevel, ProposalOutcome, TrustDomain
from homelab_helper.db.models import ExecutionReceipt, Host, ProposalLog, TrustHistory
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.executor import ExecutionRefused, OverrideGrant, execute_proposal
from homelab_helper.engine.trust import (
    ActionRequest,
    TrustContext,
    decide,
    grant_cell,
    seed_domains,
    set_boundary,
)
from tests.test_executor import make_adapter, make_artifact, make_proposal

if TYPE_CHECKING:
    import httpx

GRANT = OverrideGrant(reason="I accept the risk on this one", actor="enoch")


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


def context(**kwargs) -> TrustContext:
    base = {
        "cell_level": AutonomyLevel.AUTONOMOUS,
        "cell_granted": True,
        "cell_on_probation": False,
        "domain_default": AutonomyLevel.PROPOSE,
        "domain_max": AutonomyLevel.AUTONOMOUS,
        "domain_absolute": False,
    }
    return TrustContext(**{**base, **kwargs})


ACTION = ActionRequest(
    domain=TrustDomain.CONTAINERS,
    action_kind="restart",
    blast_radius="single-host",
    hostnames=("pve1",),
    rollback_verified=False,
)


# ---------------------------------------------------------------------------
# Pure policy: what an override does and does not buy
# ---------------------------------------------------------------------------


def test_override_crosses_the_unverified_rollback_floor() -> None:
    assert decide(ACTION, context()).level is AutonomyLevel.CONFIRM
    lifted = decide(ACTION, context(override=True))
    assert lifted.level is AutonomyLevel.AUTONOMOUS
    assert any("operator override accepted" in r for r in lifted.reasons)


def test_override_never_lifts_a_propose_cell() -> None:
    """The cell level is not a soft-hard floor, so an override cannot move it."""
    decision = decide(ACTION, context(cell_level=AutonomyLevel.PROPOSE, override=True))
    assert decision.level is AutonomyLevel.PROPOSE


def test_override_never_lifts_a_blocked_cell() -> None:
    decision = decide(ACTION, context(cell_level=AutonomyLevel.BLOCK, override=True))
    assert decision.level is AutonomyLevel.BLOCK


def test_override_never_crosses_an_absolute_domain() -> None:
    decision = decide(
        ACTION,
        context(domain_absolute=True, domain_max=AutonomyLevel.PROPOSE, override=True),
    )
    assert decision.level is AutonomyLevel.PROPOSE
    assert not any("override accepted" in r for r in decision.reasons)


def test_override_never_crosses_an_absolute_boundary() -> None:
    from homelab_helper.engine.trust import BoundaryView

    boundary = BoundaryView(hostname="pve1", ceiling=AutonomyLevel.PROPOSE, absolute=True)
    decision = decide(ACTION, context(boundaries=(boundary,), override=True))
    assert decision.level is AutonomyLevel.PROPOSE
    assert any("window-proof" in r for r in decision.reasons)


def test_override_lifts_a_non_absolute_boundary() -> None:
    from homelab_helper.engine.trust import BoundaryView

    boundary = BoundaryView(hostname="pve1", ceiling=AutonomyLevel.PROPOSE, absolute=False)
    assert decide(ACTION, context(boundaries=(boundary,))).level is AutonomyLevel.PROPOSE
    lifted = decide(ACTION, context(boundaries=(boundary,), override=True))
    assert lifted.level is AutonomyLevel.AUTONOMOUS
    assert any("lifted by operator override" in r for r in lifted.reasons)


def test_override_does_not_forge_a_window_id() -> None:
    """A receipt must not look like it ran under an elevation window."""
    assert decide(ACTION, context(override=True)).window_id is None


# ---------------------------------------------------------------------------
# Through the executor
# ---------------------------------------------------------------------------


async def test_override_runs_an_otherwise_degraded_action(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests, status_code=500)  # rollback unverifiable
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )
        p = await make_proposal(s, make_artifact())

        # Without the override it needs a confirmer; with it, it runs.
        with pytest.raises(ExecutionRefused, match="no confirmer"):
            await execute_proposal(s, p, adapter, actor="enoch")

        result = await execute_proposal(s, p, adapter, actor="enoch", override=GRANT)
        assert result.decision.level is AutonomyLevel.AUTONOMOUS
        assert result.override_used
        assert result.outcome == "succeeded"

        event = (
            (await s.execute(select(TrustHistory).where(TrustHistory.event == "override")))
            .scalars()
            .one()
        )
        assert event.detail["without_override"] == "confirm"
        assert event.detail["with_override"] == "autonomous"
        assert event.detail["reason"] == GRANT.reason
        assert event.proposal_id == p.id
    await adapter.aclose()


async def test_unneeded_override_is_not_logged(sessionmaker) -> None:
    """An override that bought nothing is reported, not written to the spine."""
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)  # rollback verifies fine
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )
        p = await make_proposal(s, make_artifact())
        result = await execute_proposal(s, p, adapter, actor="enoch", override=GRANT)

        assert result.outcome == "succeeded"
        assert not result.override_used
        assert (
            await s.execute(select(TrustHistory).where(TrustHistory.event == "override"))
        ).scalars().all() == []
    await adapter.aclose()


async def test_override_cannot_execute_a_propose_only_cell(sessionmaker) -> None:
    """No grant ⇒ still refused, and the target is never touched."""
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        p = await make_proposal(s, make_artifact())
        with pytest.raises(ExecutionRefused, match="propose"):
            await execute_proposal(s, p, adapter, actor="enoch", override=GRANT)
        assert requests == []
        assert (await s.execute(select(ExecutionReceipt))).scalars().all() == []
        assert p.outcome is ProposalOutcome.PENDING
    await adapter.aclose()


async def test_override_cannot_cross_an_absolute_host_boundary(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        host = Host(hostname="pve1")
        s.add(host)
        await s.flush()
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )
        await set_boundary(s, host, AutonomyLevel.PROPOSE, absolute=True, actor="enoch")

        p = await make_proposal(s, make_artifact())
        with pytest.raises(ExecutionRefused, match="propose"):
            await execute_proposal(s, p, adapter, actor="enoch", override=GRANT)
        assert requests == []
    await adapter.aclose()


async def test_override_is_single_use(sessionmaker) -> None:
    """It covers one action; the next proposal on the same cell is on its own."""
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests, status_code=500)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="enoch",
        )
        first = await make_proposal(s, make_artifact())
        await execute_proposal(s, first, adapter, actor="enoch", override=GRANT)

        second: ProposalLog = await make_proposal(s, make_artifact())
        with pytest.raises(ExecutionRefused, match="no confirmer"):
            await execute_proposal(s, second, adapter, actor="enoch")
    await adapter.aclose()
