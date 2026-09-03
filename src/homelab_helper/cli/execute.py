"""``helper exec ...`` — the L2 execution surface (Phase 6).

``list`` shows pending proposals whose artifact is an executable action
manifest. ``run`` routes exactly one of them through the trust gate: parse →
``decide()`` → (confirm if required) → capture rollback state → dispatch →
receipt. BLOCK/PROPOSE decisions refuse with the full reason trace; nothing
is ever dispatched past the gate.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from homelab_helper.adapters.proxmox import ProxmoxAdapter, ProxmoxAPIError, ProxmoxConfigError
from homelab_helper.config import database_url
from homelab_helper.db.enums import ProposalOutcome
from homelab_helper.db.models import ExecutionReceipt, ProposalLog
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.escalation import (
    EscalationResult,
    record_clean_outcome,
    record_rejection,
)
from homelab_helper.engine.executor import (
    ExecutionRefused,
    ExecutionResult,
    ManifestError,
    OverrideGrant,
    execute_proposal,
    parse_manifest,
    rollback_receipt,
)
from homelab_helper.engine.rollback import RollbackError
from homelab_helper.engine.trust import operator_identity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.engine.executor import ActionManifest
    from homelab_helper.engine.trust import Decision

exec_app = typer.Typer(
    name="exec",
    help="Execute action proposals through the trust gate (L2).",
    no_args_is_help=True,
)

console = Console()


def _build_adapter() -> ProxmoxAdapter:
    """Adapter factory — module-level so tests can inject a MockTransport."""
    return ProxmoxAdapter.from_env()


async def _pending_action_proposals(session: AsyncSession) -> list[tuple[ProposalLog, str | None]]:
    rows = (
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
    out: list[tuple[ProposalLog, str | None]] = []
    for p in rows:
        if (p.artifact or {}).get("kind") != "action":
            continue
        try:
            manifest = parse_manifest(p)
        except ManifestError as exc:
            out.append((p, f"invalid: {exc}"))
        else:
            out.append((p, f"{manifest.cell_key} → {manifest.target_label}"))
    return out


async def _resolve_pending(session: AsyncSession, prefix: str) -> ProposalLog | None:
    """One PENDING proposal by id prefix, or None with the reason printed."""
    candidates = (
        (
            await session.execute(
                select(ProposalLog).where(ProposalLog.outcome == ProposalOutcome.PENDING)
            )
        )
        .scalars()
        .all()
    )
    matches = [p for p in candidates if str(p.id).startswith(prefix.lower())]
    if not matches:
        console.print(f"[red]no pending proposal matches[/red] {escape(prefix)}")
        return None
    if len(matches) > 1:
        console.print(f"[red]ambiguous prefix[/red] — {len(matches)} matches")
        return None
    return matches[0]


def _report_escalation(result: EscalationResult) -> None:
    if result.promoted:
        console.print(
            f"[green]cell promoted[/green] {result.previous_level.value} → {result.level.value}: "
            f"{escape(result.reason)}"
        )
    elif result.demoted:
        console.print(f"[red]cell demoted[/red] — {escape(result.reason)}")
    else:
        console.print(f"[dim]{escape(result.reason)}[/dim]")


@exec_app.command(name="list")
def exec_list() -> None:
    """List pending, executable action proposals."""

    async def _go() -> None:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                pending = await _pending_action_proposals(session)
        finally:
            await engine.dispose()

        table = Table(title="pending action proposals")
        for col in ("id", "title", "action"):
            table.add_column(col, no_wrap=col == "id")
        for proposal, summary in pending:
            table.add_row(str(proposal.id)[:8], escape(proposal.title), escape(summary or ""))
        console.print(table)
        console.print(f"{len(pending)} proposal(s)")

    asyncio.run(_go())


def _report_run(result: ExecutionResult, *, used_override: bool) -> int:
    """Print the outcome of one run; returns the CLI exit code."""
    if result.outcome == "succeeded":
        console.print(
            f"[green]executed[/green] at {result.decision.level.value} "
            f"in {result.duration_ms} ms — receipt {result.receipt_id}"
        )
    else:
        console.print(
            f"[red]dispatch failed:[/red] {escape(result.error or 'unknown')} "
            f"— receipt {result.receipt_id}; proposal left pending"
        )
    if used_override:
        console.print(
            "[red]override was used[/red] and logged"
            if result.override_used
            else "[dim]override was not needed — policy already allowed it[/dim]"
        )
    if result.escalation is not None:
        _report_escalation(result.escalation)
    return 0 if result.outcome == "succeeded" else 4


@exec_app.command(name="run")
def exec_run(
    proposal_id: str = typer.Argument(..., help="Proposal id (or unique prefix)."),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Consent to THIS proposal up front instead of at the interactive prompt.",
    ),
    override: bool = typer.Option(
        False,
        "--override",
        help="Cross the soft-hard floors for THIS action (owner-only, logged).",
    ),
    override_reason: str = typer.Option(
        "", "--override-reason", help="Required with --override: why you accept it."
    ),
) -> None:
    """Run one pending action proposal through the trust gate.

    ``--override`` crosses the *soft-hard* floors — the unverified-rollback
    degrade and a non-absolute host ceiling — for this one action. It never
    crosses an absolute floor, and it never makes a propose-only cell execute.
    The gesture is deliberately high-friction: you retype the cell key.
    """

    async def _confirm(manifest: ActionManifest, decision: Decision) -> bool:
        console.print(
            f"[yellow]confirm required[/yellow] — {escape(manifest.cell_key)} "
            f"targeting {escape(manifest.target_label)}"
        )
        for reason in decision.reasons:
            console.print(f"  [dim]· {escape(reason)}[/dim]")
        if yes:
            console.print("[dim]pre-consented with --yes[/dim]")
            return True
        return bool(typer.confirm("dispatch?", default=False))

    def _collect_override(manifest: ActionManifest) -> OverrideGrant | None:
        """High-friction, interactive, owner-only. Retyping the cell key is the
        "I accept this" — a bare y/n is too easy to fire off by reflex."""
        console.print(
            f"[red]override[/red] crosses the soft-hard floors for "
            f"{escape(manifest.cell_key)} on {escape(manifest.target_label)}"
        )
        console.print("[dim]absolute floors are unaffected; this covers one action only[/dim]")
        typed = typer.prompt(f"type the cell key to accept ({manifest.cell_key})")
        if typed.strip() != manifest.cell_key:
            console.print("[yellow]override not confirmed[/yellow] — cell key did not match")
            return None
        return OverrideGrant(reason=override_reason, actor=operator_identity())

    def _prepare_override(proposal: ProposalLog) -> tuple[bool, OverrideGrant | None]:
        """``(ok, grant)`` — ok is False only when an override was asked for
        and could not be granted, which aborts the run."""
        if not override:
            return True, None
        if not override_reason.strip():
            console.print("[red]--override requires --override-reason[/red]")
            return False, None
        try:
            manifest = parse_manifest(proposal)
        except ManifestError as exc:
            console.print(f"[red]invalid manifest:[/red] {escape(str(exc))}")
            return False, None
        grant = _collect_override(manifest)
        return grant is not None, grant

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                proposal = await _resolve_pending(session, proposal_id)
                if proposal is None:
                    return 1

                ok, grant = _prepare_override(proposal)
                if not ok:
                    return 2

                try:
                    adapter = _build_adapter()
                except ProxmoxConfigError as exc:
                    console.print(f"[red]adapter config:[/red] {escape(str(exc))}")
                    return 2
                try:
                    result = await execute_proposal(
                        session,
                        proposal,
                        adapter,
                        actor=operator_identity(),
                        confirm_cb=_confirm,
                        override=grant,
                    )
                except ManifestError as exc:
                    console.print(f"[red]invalid manifest:[/red] {escape(str(exc))}")
                    return 2
                except ExecutionRefused as exc:
                    console.print(f"[yellow]refused:[/yellow] {escape(str(exc))}")
                    if exc.decision is not None:
                        for reason in exc.decision.reasons:
                            console.print(f"  [dim]· {escape(reason)}[/dim]")
                    return 3
                finally:
                    await adapter.aclose()

                return _report_run(result, used_override=grant is not None)
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@exec_app.command(name="accept")
def exec_accept(
    proposal_id: str = typer.Argument(..., help="Proposal id (or unique prefix)."),
    note: str = typer.Option("", "--note", help="Why you accepted, for the record."),
) -> None:
    """Record that you applied a proposal by hand (the PROPOSE-rung evidence).

    This is how a propose-only cell earns its first rung: each accepted
    proposal is one clean approval toward `PROMOTION_STREAK`.
    """

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                proposal = await _resolve_pending(session, proposal_id)
                if proposal is None:
                    return 1
                actor = operator_identity()
                proposal.outcome = ProposalOutcome.USER_ACCEPTED
                proposal.outcome_at = datetime.now(UTC)
                proposal.outcome_by = actor
                proposal.outcome_notes = note or "applied by hand"
                console.print(f"[green]accepted[/green] {escape(proposal.title)}")

                try:
                    manifest = parse_manifest(proposal)
                except ManifestError:
                    console.print("[dim]not an executable action — no cell to credit[/dim]")
                    return 0
                _report_escalation(
                    await record_clean_outcome(
                        session,
                        domain=manifest.domain,
                        action_kind=manifest.action_kind,
                        blast_radius=manifest.blast_radius,
                        actor=actor,
                        proposal_id=proposal.id,
                    )
                )
                return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@exec_app.command(name="reject")
def exec_reject(
    proposal_id: str = typer.Argument(..., help="Proposal id (or unique prefix)."),
    note: str = typer.Option("", "--note", help="Why you rejected, for the record."),
) -> None:
    """Reject a proposal: breaks the cell's clean streak, never demotes it."""

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                proposal = await _resolve_pending(session, proposal_id)
                if proposal is None:
                    return 1
                proposal.outcome = ProposalOutcome.USER_REJECTED
                proposal.outcome_at = datetime.now(UTC)
                proposal.outcome_by = operator_identity()
                proposal.outcome_notes = note or None
                console.print(f"[yellow]rejected[/yellow] {escape(proposal.title)}")

                try:
                    manifest = parse_manifest(proposal)
                except ManifestError:
                    return 0
                _report_escalation(
                    await record_rejection(
                        session,
                        domain=manifest.domain,
                        action_kind=manifest.action_kind,
                        blast_radius=manifest.blast_radius,
                    )
                )
                return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@exec_app.command(name="rollback")
def exec_rollback(
    receipt_id: str = typer.Argument(..., help="Execution receipt id (or unique prefix)."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Undo one executed action, using the state captured before it ran.

    Operator-initiated and not gated by the trust gradient: the gradient
    governs what the framework does on its own, and a safety valve the policy
    could lock shut is not a safety valve. The undo writes its own receipt.
    """

    async def _resolve(session: AsyncSession) -> ExecutionReceipt | None:
        rows = (await session.execute(select(ExecutionReceipt))).scalars().all()
        matches = [r for r in rows if str(r.id).startswith(receipt_id.lower())]
        if not matches:
            console.print(f"[red]no receipt matches[/red] {escape(receipt_id)}")
            return None
        if len(matches) > 1:
            console.print(f"[red]ambiguous prefix[/red] — {len(matches)} matches")
            return None
        return matches[0]

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                receipt = await _resolve(session)
                if receipt is None:
                    return 1

                state = receipt.rollback_state or {}
                console.print(
                    f"rolling back receipt [bold]{str(receipt.id)[:8]}[/bold] — "
                    f"strategy {escape(str(state.get('strategy', 'unknown')))}, "
                    f"captured {escape(str(state.get('captured_at', 'unknown')))}"
                )
                if not yes and not typer.confirm("restore?", default=False):
                    console.print("[dim]left as-is[/dim]")
                    return 3

                try:
                    adapter = _build_adapter()
                except ProxmoxConfigError as exc:
                    console.print(f"[red]adapter config:[/red] {escape(str(exc))}")
                    return 2
                try:
                    result = await rollback_receipt(
                        session, receipt, adapter, actor=operator_identity()
                    )
                except RollbackError as exc:
                    console.print(f"[red]cannot roll back:[/red] {escape(str(exc))}")
                    return 2
                except ProxmoxAPIError as exc:
                    console.print(f"[red]restore failed:[/red] {escape(str(exc))}")
                    return 4
                finally:
                    await adapter.aclose()

                console.print(
                    f"[green]rolled back[/green] {escape(result.detail)} "
                    f"in {result.duration_ms} ms — receipt {result.receipt_id}"
                )
                return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@exec_app.command(name="receipts")
def exec_receipts() -> None:
    """List execution receipts, newest first."""

    async def _go() -> None:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                rows = (
                    (
                        await session.execute(
                            select(ExecutionReceipt).order_by(ExecutionReceipt.executed_at.desc())
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

        table = Table(title="execution receipts")
        for col in ("id", "executed", "actor", "level", "outcome", "rollback"):
            table.add_column(col, no_wrap=col in {"id", "outcome", "rollback"})
        for r in rows:
            style = "green" if r.outcome == "succeeded" else "red"
            state = r.rollback_state or {}
            if r.rolled_back_at is not None:
                rollback = "[yellow]rolled back[/yellow]"
            elif (r.action or {}).get("kind") == "rollback":
                rollback = "[dim]is an undo[/dim]"
            elif state.get("verified"):
                rollback = f"[dim]{escape(str(state.get('strategy', '')))}[/dim]"
            else:
                rollback = "[dim]unverified[/dim]"
            table.add_row(
                str(r.id)[:8],
                r.executed_at.strftime("%Y-%m-%d %H:%M"),
                escape(r.actor),
                r.decision_level.value,
                f"[{style}]{r.outcome}[/{style}]",
                rollback,
            )
        console.print(table)
        console.print(f"{len(rows)} receipt(s)")

    asyncio.run(_go())


__all__ = ["exec_app"]
