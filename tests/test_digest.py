"""Weekly digest (Phase 8.6) — deterministic contents, tiling windows.

P8-AC6: the digest is generated from findings and receipts; an LLM may narrate
it but does not choose what is in it. The load-bearing assertions are that
consecutive windows tile without gap or overlap, that a quiet window is
recognised as quiet, and that rendering is a pure function of the rows.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    AutonomyLevel,
    FindingKind,
    FindingSeverity,
    FindingStatus,
    ProposalOutcome,
)
from homelab_helper.db.models import (
    DigestRun,
    ExecutionReceipt,
    ProposalLog,
    ReconciliationFinding,
    TrustHistory,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.digest import (
    DEFAULT_WINDOW_DAYS,
    build_digest,
    last_digest,
    record_digest,
    render_markdown,
    render_notification,
    resolve_window,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


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


def finding(
    *,
    title: str,
    fingerprint: str,
    first_seen: datetime,
    severity: FindingSeverity = FindingSeverity.MEDIUM,
    status: FindingStatus = FindingStatus.OPEN,
    resolved_at: datetime | None = None,
) -> ReconciliationFinding:
    return ReconciliationFinding(
        kind=FindingKind.VERSION_DRIFT,
        severity=severity,
        fingerprint=fingerprint,
        title=title,
        description=title,
        affected=[{"target_type": "host", "target_id": "bmax0"}],
        status=status,
        first_seen=first_seen,
        last_seen=first_seen,
        resolved_at=resolved_at,
    )


async def receipt(
    session, *, executed_at: datetime, outcome: str = "succeeded"
) -> ExecutionReceipt:
    proposal = ProposalLog(
        title="Restart web01",
        artifact={"kind": "action"},
        blast_radius="single-host",
        outcome=ProposalOutcome.USER_ACCEPTED,
    )
    session.add(proposal)
    await session.flush()
    row = ExecutionReceipt(
        proposal_id=proposal.id,
        executed_at=executed_at,
        actor="listener",
        decision_level=AutonomyLevel.AUTONOMOUS,
        decision_reasons=["cell granted"],
        action={
            "kind": "action",
            "domain": "containers",
            "action_kind": "restart",
            "target": {"node": "bmax0", "vmid": 101},
        },
        rollback_state={},
        outcome=outcome,
        duration_ms=120,
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# Windows tile
# ---------------------------------------------------------------------------


async def test_first_digest_covers_the_default_window(sessionmaker) -> None:
    async with sessionmaker() as s:
        window = await resolve_window(s, now=NOW)
    assert window.end == NOW
    assert window.start == NOW - timedelta(days=DEFAULT_WINDOW_DAYS)
    assert "no previous digest" in window.basis


async def test_consecutive_windows_tile_without_gap_or_overlap(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        first = await build_digest(s, now=NOW)
        await record_digest(s, first, delivery="sent")

    later = NOW + timedelta(days=7)
    async with sessionmaker() as s:
        second = await resolve_window(s, now=later)

    assert second.start == first.window.end, "the next window starts where the last stopped"
    assert second.end == later
    assert "since the last digest" in second.basis


async def test_explicit_days_overrides_the_recorded_window(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        first = await build_digest(s, now=NOW)
        await record_digest(s, first, delivery="sent")
        window = await resolve_window(s, days=2, now=NOW + timedelta(days=1))
    assert (window.end - window.start) == timedelta(days=2)
    assert "as asked" in window.basis


async def test_a_second_digest_in_the_same_instant_is_empty_not_negative(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        first = await build_digest(s, now=NOW)
        await record_digest(s, first, delivery="sent")
        again = await resolve_window(s, now=NOW)
    assert again.start == again.end
    assert again.days == 0


# ---------------------------------------------------------------------------
# Contents
# ---------------------------------------------------------------------------


async def test_only_in_window_changes_are_reported(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(finding(title="inside", fingerprint="a" * 16, first_seen=NOW - timedelta(days=2)))
        s.add(finding(title="too old", fingerprint="b" * 16, first_seen=NOW - timedelta(days=30)))
        await s.flush()
        digest = await build_digest(s, now=NOW)

    titles = [f.title for f in digest.opened]
    assert titles == ["inside"]
    assert digest.open_total == 2, "both are still open, even if only one is new"


async def test_resolved_findings_are_counted_in_their_window(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(
            finding(
                title="fixed",
                fingerprint="c" * 16,
                first_seen=NOW - timedelta(days=20),
                status=FindingStatus.RESOLVED,
                resolved_at=NOW - timedelta(days=1),
            )
        )
        await s.flush()
        digest = await build_digest(s, now=NOW)

    assert [f.title for f in digest.resolved] == ["fixed"]
    assert digest.opened == [], "opened before the window"
    assert digest.open_total == 0


async def test_opened_findings_are_ordered_worst_first(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        for sev, name in (
            (FindingSeverity.LOW, "low"),
            (FindingSeverity.CRITICAL, "critical"),
            (FindingSeverity.MEDIUM, "medium"),
        ):
            s.add(
                finding(
                    title=name,
                    fingerprint=name.ljust(16, "z"),
                    first_seen=NOW - timedelta(hours=1),
                    severity=sev,
                )
            )
        await s.flush()
        digest = await build_digest(s, now=NOW)

    assert [f.title for f in digest.opened] == ["critical", "medium", "low"]


async def test_receipts_and_rollbacks(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await receipt(s, executed_at=NOW - timedelta(days=1))
        await receipt(s, executed_at=NOW - timedelta(days=2), outcome="failed")
        await receipt(s, executed_at=NOW - timedelta(days=40))  # outside
        digest = await build_digest(s, now=NOW)

    assert len(digest.executed) == 2
    assert digest.executed_ok == 1
    assert digest.executed_failed == 1


async def test_rollback_receipts_are_not_counted_as_actions(sessionmaker) -> None:
    """An undo already appears as "rolled back" on the action it reversed."""
    async with session_scope(sessionmaker) as s:
        original = await receipt(s, executed_at=NOW - timedelta(days=1))
        s.add(
            ExecutionReceipt(
                proposal_id=original.proposal_id,
                executed_at=NOW - timedelta(hours=12),
                actor="enoch",
                decision_level=AutonomyLevel.AUTONOMOUS,
                decision_reasons=["operator rollback"],
                action={"kind": "rollback", "of_receipt": str(original.id)},
                rollback_state={},
                outcome="succeeded",
                duration_ms=90,
            )
        )
        await s.flush()
        digest = await build_digest(s, now=NOW)

    assert len(digest.executed) == 1


async def test_authority_changes_are_summarised(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(
            TrustHistory(
                at=NOW - timedelta(days=1),
                actor="enoch",
                event="grant",
                detail={
                    "action_kind": "restart",
                    "blast_radius": "single-host",
                    "level": "confirm",
                },
            )
        )
        s.add(
            TrustHistory(
                at=NOW - timedelta(days=1),
                actor="enoch",
                event="auto-promote",
                detail={
                    "cell": "containers/restart/single-host",
                    "from": "confirm",
                    "to": "autonomous",
                },
            )
        )
        s.add(TrustHistory(at=NOW - timedelta(days=1), actor="enoch", event="noise", detail={}))
        await s.flush()
        digest = await build_digest(s, now=NOW)

    events = [a.event for a in digest.authority]
    assert events == ["granted", "promoted"], "unknown event kinds are left out"
    assert "confirm → autonomous" in digest.authority[1].detail


async def test_pending_proposals_are_the_recommendation_tail(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(
            ProposalLog(
                title="Resize esphome-lxc to 1 core",
                artifact={"kind": "action"},
                blast_radius="single-host",
            )
        )
        await s.flush()
        digest = await build_digest(s, now=NOW)

    assert digest.pending_proposals == ["Resize esphome-lxc to 1 core"]


# ---------------------------------------------------------------------------
# Quiet windows
# ---------------------------------------------------------------------------


async def test_an_empty_window_is_quiet(sessionmaker) -> None:
    async with sessionmaker() as s:
        digest = await build_digest(s, now=NOW)
    assert digest.quiet


async def test_a_standing_open_finding_does_not_make_a_window_busy(sessionmaker) -> None:
    """Otherwise one known LOW finding buzzes the phone every week forever."""
    async with session_scope(sessionmaker) as s:
        s.add(finding(title="old news", fingerprint="d" * 16, first_seen=NOW - timedelta(days=60)))
        await s.flush()
        digest = await build_digest(s, now=NOW)

    assert digest.quiet
    assert digest.open_total == 1


async def test_any_activity_makes_a_window_busy(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await receipt(s, executed_at=NOW - timedelta(hours=2))
        digest = await build_digest(s, now=NOW)
    assert not digest.quiet


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


async def test_rendering_is_a_pure_function_of_the_rows(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(finding(title="drift", fingerprint="e" * 16, first_seen=NOW - timedelta(days=1)))
        await receipt(s, executed_at=NOW - timedelta(days=1))
        await s.flush()
        first = render_markdown(await build_digest(s, now=NOW))
        second = render_markdown(await build_digest(s, now=NOW))
    assert first == second


async def test_page_names_what_happened(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(
            finding(
                title="bmax1 is 8 versions behind",
                fingerprint="f" * 16,
                first_seen=NOW - timedelta(days=1),
                severity=FindingSeverity.HIGH,
            )
        )
        await receipt(s, executed_at=NOW - timedelta(days=1))
        await s.flush()
        page = render_markdown(await build_digest(s, now=NOW))

    assert "## What was done" in page
    assert "## What changed" in page
    assert "## What is recommended" in page
    assert "bmax1 is 8 versions behind" in page
    assert "containers/restart" in page


async def test_notification_is_short_and_counts_things(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(finding(title="drift", fingerprint="0" * 16, first_seen=NOW - timedelta(days=1)))
        await receipt(s, executed_at=NOW - timedelta(days=1))
        await receipt(s, executed_at=NOW - timedelta(days=1), outcome="failed")
        await s.flush()
        title, message = render_notification(await build_digest(s, now=NOW))

    assert "homelab-helper" in title
    assert "1 action(s) ran, 1 failed" in message
    assert "1 finding(s) opened" in message
    assert len(message) < 400, "it has to be readable on a lock screen"


async def test_quiet_notification_says_so(sessionmaker) -> None:
    async with sessionmaker() as s:
        _, message = render_notification(await build_digest(s, now=NOW))
    assert "Nothing changed" in message


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


async def test_record_digest_stores_counts_and_delivery(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await receipt(s, executed_at=NOW - timedelta(days=1))
        digest = await build_digest(s, now=NOW)
        run = await record_digest(s, digest, delivery="sent")

        assert run.counts["executed_ok"] == 1
        assert run.delivery == "sent"
        assert not run.quiet
        assert (await last_digest(s)).id == run.id

    async with sessionmaker() as s:
        stored = (await s.execute(select(DigestRun))).scalar_one()
        assert stored.window_end is not None


def test_digest_never_imports_the_llm_package() -> None:
    """P8-AC6: an LLM may narrate a digest; it never chooses the contents."""
    code = (
        "import sys; import homelab_helper.engine.digest; "
        "bad = [m for m in sys.modules if m.startswith('homelab_helper.llm')]; "
        "assert not bad, f'LLM modules in the digest path: {bad}'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
