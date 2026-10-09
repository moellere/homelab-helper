"""Weekly digest (Phase 8.6) — one summary instead of a stream.

Phases 1 to 8.4 each grew their own voice: findings accumulate, playbooks draft
proposals, the executor writes receipts, the phone buzzes after unattended
runs. The digest is the answer to "do not tell me nine times a week": what
changed, what is recommended, and what was done, once.

**Contents are chosen here, deterministically, from rows.** Per roadmap
P8-AC6 an LLM may *narrate* a digest but never decides what is in one, so this
module reads findings, proposals, receipts and trust history and nothing else;
a regression test asserts it never imports ``homelab_helper.llm``.

The window runs from the end of the last recorded digest to now, so
consecutive digests tile the timeline — no change reported twice, none missed
in a gap. With no prior digest the window is the last
:data:`DEFAULT_WINDOW_DAYS` days, and ``--days`` overrides either way.

Cadence belongs to the caller (``helper digest send`` from cron, or the
daemon's ``digest`` job), the same way discovery passes work. What this module
guarantees is that two digests never disagree about a window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import FindingSeverity, FindingStatus, ProposalOutcome
from homelab_helper.db.models import (
    DigestRun,
    ExecutionReceipt,
    ProposalLog,
    ReconciliationFinding,
    TrustHistory,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_WINDOW_DAYS = 7
MAX_LINES_PER_SECTION = 12
"""A digest is a summary; long sections are truncated with a counted tail."""

_SEVERITY_ORDER = [
    FindingSeverity.CRITICAL,
    FindingSeverity.HIGH,
    FindingSeverity.MEDIUM,
    FindingSeverity.LOW,
    FindingSeverity.INFO,
]
_AUTHORITY_EVENTS = {
    "grant": "granted",
    "auto-promote": "promoted",
    "demote": "demoted",
    "override": "overridden",
    "approval": "approved",
    "window-open": "window opened",
    "window-revoke": "window revoked",
    "boundary-set": "boundary set",
}


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    basis: str
    """How the window was chosen — printed, so a short digest explains itself."""

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400


@dataclass(frozen=True)
class FindingLine:
    severity: FindingSeverity
    kind: str
    title: str
    fingerprint: str

    @property
    def short(self) -> str:
        return self.fingerprint[:12]


@dataclass(frozen=True)
class ReceiptLine:
    cell: str
    target: str
    level: str
    outcome: str
    actor: str
    rolled_back: bool
    short_id: str


@dataclass(frozen=True)
class AuthorityLine:
    event: str
    actor: str
    detail: str


@dataclass(frozen=True)
class Digest:
    window: Window
    opened: list[FindingLine] = field(default_factory=list)
    resolved: list[FindingLine] = field(default_factory=list)
    open_by_severity: dict[str, int] = field(default_factory=dict)
    recommended: list[FindingLine] = field(default_factory=list)
    pending_proposals: list[str] = field(default_factory=list)
    executed: list[ReceiptLine] = field(default_factory=list)
    authority: list[AuthorityLine] = field(default_factory=list)

    @property
    def executed_ok(self) -> int:
        return sum(1 for r in self.executed if r.outcome == "succeeded")

    @property
    def executed_failed(self) -> int:
        return sum(1 for r in self.executed if r.outcome != "succeeded")

    @property
    def open_total(self) -> int:
        return sum(self.open_by_severity.values())

    @property
    def quiet(self) -> bool:
        """Nothing happened *in the window*. Standing open findings don't count:
        a digest that fires every week because the lab has one known LOW
        finding is the stream this slice exists to replace."""
        return not (self.opened or self.resolved or self.executed or self.authority)

    def counts(self) -> dict[str, Any]:
        return {
            "opened": len(self.opened),
            "resolved": len(self.resolved),
            "open_total": self.open_total,
            "executed_ok": self.executed_ok,
            "executed_failed": self.executed_failed,
            "authority": len(self.authority),
            "pending_proposals": len(self.pending_proposals),
            "quiet": self.quiet,
        }


async def last_digest(session: AsyncSession) -> DigestRun | None:
    rows = (
        await session.execute(select(DigestRun).order_by(DigestRun.window_end.desc()).limit(1))
    ).scalars()
    return rows.one_or_none()


async def resolve_window(
    session: AsyncSession, *, days: int | None = None, now: datetime | None = None
) -> Window:
    """Since the last digest, or the last ``days`` days when told to."""
    end = _as_utc(now or datetime.now(UTC))
    if days is not None:
        return Window(end - timedelta(days=days), end, f"last {days} day(s), as asked")
    previous = await last_digest(session)
    if previous is None:
        start = end - timedelta(days=DEFAULT_WINDOW_DAYS)
        return Window(start, end, f"last {DEFAULT_WINDOW_DAYS} days (no previous digest)")
    start = _as_utc(previous.window_end)
    if start >= end:
        return Window(end, end, "nothing since the last digest")
    return Window(start, end, f"since the last digest ({start:%Y-%m-%d %H:%M} UTC)")


def _line(finding: ReconciliationFinding) -> FindingLine:
    return FindingLine(
        severity=finding.severity,
        kind=finding.kind.value,
        title=finding.title,
        fingerprint=finding.fingerprint,
    )


def _severity_rank(line: FindingLine) -> int:
    try:
        return _SEVERITY_ORDER.index(line.severity)
    except ValueError:  # a severity added without updating the order
        return len(_SEVERITY_ORDER)


async def build_digest(
    session: AsyncSession, *, days: int | None = None, now: datetime | None = None
) -> Digest:
    """Assemble one digest from rows. No network, no model, no randomness."""
    window = await resolve_window(session, days=days, now=now)
    findings = (await session.execute(select(ReconciliationFinding))).scalars().all()

    opened, resolved, still_open = [], [], []
    for finding in findings:
        if window.start <= _as_utc(finding.first_seen) < window.end:
            opened.append(_line(finding))
        if finding.resolved_at is not None and window.start <= _as_utc(finding.resolved_at) < (
            window.end
        ):
            resolved.append(_line(finding))
        if finding.status is FindingStatus.OPEN:
            still_open.append(_line(finding))

    open_by_severity: dict[str, int] = {}
    for line in still_open:
        open_by_severity[line.severity.value] = open_by_severity.get(line.severity.value, 0) + 1

    receipts = (
        (
            await session.execute(
                select(ExecutionReceipt)
                .where(
                    ExecutionReceipt.executed_at >= window.start,
                    ExecutionReceipt.executed_at < window.end,
                )
                .order_by(ExecutionReceipt.executed_at)
            )
        )
        .scalars()
        .all()
    )
    executed = [
        ReceiptLine(
            cell=str((r.action or {}).get("domain", "?"))
            + "/"
            + str((r.action or {}).get("action_kind", "?")),
            target=str((r.action or {}).get("target", {}).get("node", "?"))
            + "/"
            + str((r.action or {}).get("target", {}).get("vmid", "?")),
            level=r.decision_level.value,
            outcome=r.outcome,
            actor=r.actor,
            rolled_back=r.rolled_back_at is not None,
            short_id=str(r.id)[:8],
        )
        for r in receipts
        if (r.action or {}).get("kind") != "rollback"
    ]

    history = (
        (
            await session.execute(
                select(TrustHistory)
                .where(TrustHistory.at >= window.start, TrustHistory.at < window.end)
                .order_by(TrustHistory.at)
            )
        )
        .scalars()
        .all()
    )
    authority = [
        AuthorityLine(
            event=_AUTHORITY_EVENTS.get(row.event, row.event),
            actor=row.actor,
            detail=_authority_detail(row),
        )
        for row in history
        if row.event in _AUTHORITY_EVENTS
    ]

    pending = (
        (
            await session.execute(
                select(ProposalLog)
                .where(ProposalLog.outcome == ProposalOutcome.PENDING)
                .order_by(ProposalLog.proposed_at)
            )
        )
        .scalars()
        .all()
    )

    return Digest(
        window=window,
        opened=sorted(opened, key=_severity_rank),
        resolved=sorted(resolved, key=_severity_rank),
        open_by_severity=open_by_severity,
        recommended=sorted(still_open, key=_severity_rank)[:MAX_LINES_PER_SECTION],
        pending_proposals=[p.title for p in pending],
        executed=executed,
        authority=authority,
    )


def _authority_detail(row: TrustHistory) -> str:
    detail = row.detail or {}
    cell = detail.get("cell") or "/".join(
        str(detail[k]) for k in ("action_kind", "blast_radius") if detail.get(k)
    )
    if row.event in {"auto-promote", "demote"}:
        return f"{cell}: {detail.get('from')} → {detail.get('to')}".strip()
    if row.event == "grant":
        return f"{cell} = {detail.get('level')}".strip()
    if row.event == "override":
        return f"{cell}: {detail.get('without_override')} → {detail.get('with_override')}"
    if row.event == "approval":
        return str(detail.get("answer") or detail.get("cell") or "")
    return str(detail.get("reason") or cell or "")


def _bullets(lines: list[str]) -> list[str]:
    shown = lines[:MAX_LINES_PER_SECTION]
    if len(lines) > MAX_LINES_PER_SECTION:
        shown.append(f"… and {len(lines) - MAX_LINES_PER_SECTION} more")
    return shown


def render_markdown(digest: Digest) -> str:
    """The readable page. Deterministic: same rows in, same bytes out."""
    w = digest.window
    out = [
        f"# homelab-helper digest — {w.end:%Y-%m-%d}",
        "",
        f"Window: {w.start:%Y-%m-%d %H:%M} → {w.end:%Y-%m-%d %H:%M} UTC ({w.basis}).",
        "",
    ]
    if digest.quiet:
        out += ["Nothing changed, ran, or moved in this window.", ""]

    out += ["## What was done", ""]
    if digest.executed:
        out.append(f"{digest.executed_ok} succeeded, {digest.executed_failed} failed.")
        out.append("")
        out += [
            f"- `{r.short_id}` {r.cell} → {r.target} at {r.level} by {r.actor}: "
            f"**{r.outcome}**{' (rolled back)' if r.rolled_back else ''}"
            for r in digest.executed[:MAX_LINES_PER_SECTION]
        ]
        if len(digest.executed) > MAX_LINES_PER_SECTION:
            out.append(f"- … and {len(digest.executed) - MAX_LINES_PER_SECTION} more")
    else:
        out.append("Nothing executed.")
    out.append("")

    if digest.authority:
        out += ["## Authority changes", ""]
        out += _bullets([f"- {a.event} by {a.actor} — {a.detail}" for a in digest.authority])
        out.append("")

    out += ["## What changed", ""]
    out.append(f"{len(digest.opened)} finding(s) opened, {len(digest.resolved)} resolved.")
    out.append("")
    if digest.opened:
        out += ["**Opened**", ""]
        out += _bullets(
            [
                f"- {f.severity.value.upper()} {f.kind} — {f.title} (`{f.short}`)"
                for f in digest.opened
            ]
        )
        out.append("")
    if digest.resolved:
        out += ["**Resolved**", ""]
        out += _bullets([f"- {f.kind} — {f.title} (`{f.short}`)" for f in digest.resolved])
        out.append("")

    out += ["## What is recommended", ""]
    if digest.open_total:
        tally = ", ".join(
            f"{digest.open_by_severity[s.value]} {s.value}"
            for s in _SEVERITY_ORDER
            if digest.open_by_severity.get(s.value)
        )
        out.append(f"{digest.open_total} open finding(s): {tally}.")
        out.append("")
        out += [
            f"- {f.severity.value.upper()} {f.kind} — {f.title} (`{f.short}`)"
            for f in digest.recommended
        ]
        if digest.open_total > len(digest.recommended):
            out.append(f"- … and {digest.open_total - len(digest.recommended)} more")
        out.append("")
    else:
        out += ["No open findings.", ""]
    if digest.pending_proposals:
        out += [
            f"{len(digest.pending_proposals)} proposal(s) awaiting a decision "
            "(`helper exec list`):",
            "",
        ]
        out += _bullets([f"- {t}" for t in digest.pending_proposals])
        out.append("")

    out += ["---", "", "`helper digest show` for this page · `helper exec receipts` for detail"]
    return "\n".join(out).rstrip() + "\n"


def render_notification(digest: Digest) -> tuple[str, str]:
    """``(title, message)`` short enough to read on a lock screen."""
    title = f"homelab-helper: week to {digest.window.end:%b %d}"
    parts: list[str] = []
    if digest.executed:
        ran = f"{digest.executed_ok} action(s) ran"
        if digest.executed_failed:
            ran += f", {digest.executed_failed} failed"
        parts.append(ran + ".")
    if digest.opened or digest.resolved:
        parts.append(f"{len(digest.opened)} finding(s) opened, {len(digest.resolved)} resolved.")
    if digest.authority:
        parts.append(f"{len(digest.authority)} authority change(s).")
    if digest.open_total:
        worst = next(
            (s for s in _SEVERITY_ORDER if digest.open_by_severity.get(s.value)),
            None,
        )
        parts.append(
            f"{digest.open_total} open finding(s)" + (f", worst {worst.value}." if worst else ".")
        )
    if digest.pending_proposals:
        parts.append(f"{len(digest.pending_proposals)} proposal(s) waiting.")
    if not parts:
        parts.append("Nothing changed this week.")
    parts.append("helper digest show")
    return title, " ".join(parts)


async def record_digest(
    session: AsyncSession, digest: Digest, *, delivery: str, detail: str | None = None
) -> DigestRun:
    """Persist the window so the next digest starts where this one stopped."""
    run = DigestRun(
        window_start=digest.window.start,
        window_end=digest.window.end,
        counts=digest.counts(),
        delivery=delivery,
        delivery_detail=detail,
        quiet=digest.quiet,
    )
    session.add(run)
    await session.flush()
    return run


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "AuthorityLine",
    "Digest",
    "FindingLine",
    "ReceiptLine",
    "Window",
    "build_digest",
    "last_digest",
    "record_digest",
    "render_markdown",
    "render_notification",
    "resolve_window",
]
