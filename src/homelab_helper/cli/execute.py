"""``helper exec ...`` — the L2 execution surface (Phase 6).

``list`` shows pending proposals whose artifact is an executable action
manifest. ``run`` routes exactly one of them through the trust gate: parse →
``decide()`` → (confirm if required) → capture rollback state → dispatch →
receipt. BLOCK/PROPOSE decisions refuse with the full reason trace; nothing
is ever dispatched past the gate.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from homelab_helper.adapters.proxmox import ProxmoxAdapter, ProxmoxConfigError
from homelab_helper.config import database_url
from homelab_helper.db.enums import ProposalOutcome
from homelab_helper.db.models import ExecutionReceipt, ProposalLog
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.executor import (
    ExecutionRefused,
    ManifestError,
    execute_proposal,
    parse_manifest,
)
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


@exec_app.command(name="run")
def exec_run(
    proposal_id: str = typer.Argument(..., help="Proposal id (or unique prefix)."),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Consent to THIS proposal up front instead of at the interactive prompt.",
    ),
) -> None:
    """Run one pending action proposal through the trust gate."""

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

    async def _resolve(session: AsyncSession) -> ProposalLog | None:
        candidates = (
            (
                await session.execute(
                    select(ProposalLog).where(ProposalLog.outcome == ProposalOutcome.PENDING)
                )
            )
            .scalars()
            .all()
        )
        matches = [p for p in candidates if str(p.id).startswith(proposal_id.lower())]
        if not matches:
            console.print(f"[red]no pending proposal matches[/red] {escape(proposal_id)}")
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
                proposal = await _resolve(session)
                if proposal is None:
                    return 1

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

                if result.outcome == "succeeded":
                    console.print(
                        f"[green]executed[/green] at {result.decision.level.value} "
                        f"in {result.duration_ms} ms — receipt {result.receipt_id}"
                    )
                    return 0
                console.print(
                    f"[red]dispatch failed:[/red] {escape(result.error or 'unknown')} "
                    f"— receipt {result.receipt_id}; proposal left pending"
                )
                return 4
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
        for col in ("id", "executed", "actor", "level", "outcome"):
            table.add_column(col, no_wrap=col in {"id", "outcome"})
        for r in rows:
            style = "green" if r.outcome == "succeeded" else "red"
            table.add_row(
                str(r.id)[:8],
                r.executed_at.strftime("%Y-%m-%d %H:%M"),
                escape(r.actor),
                r.decision_level.value,
                f"[{style}]{r.outcome}[/{style}]",
            )
        console.print(table)
        console.print(f"{len(rows)} receipt(s)")

    asyncio.run(_go())


__all__ = ["exec_app"]
