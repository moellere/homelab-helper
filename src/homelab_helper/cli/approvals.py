"""``helper approvals`` — the phone-tap side of Phase 7, from the operator's seat.

``show`` answers three questions: is an approval channel configured (and where
would the question go), what would each pending action proposal get from
policy right now (and so whether triggering it would ask you), and who
answered what recently. Read-only: the answers themselves live on
``TrustHistory`` and the receipts.
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from homelab_helper.config import database_url
from homelab_helper.db.enums import ProposalOutcome
from homelab_helper.db.models import ProposalLog, TrustHistory
from homelab_helper.db.session import make_engine, make_sessionmaker
from homelab_helper.engine.approval import ApprovalConfigError, HomeAssistantApprovalConfig
from homelab_helper.engine.executor import ManifestError, parse_manifest
from homelab_helper.engine.trust import ActionRequest, decide, load_trust_context

approvals_app = typer.Typer(
    name="approvals",
    help="Phase 7 approval channel: configuration, what would ask you, who answered.",
    no_args_is_help=True,
)
console = Console()


def _channel_line() -> str:
    try:
        cfg = HomeAssistantApprovalConfig.from_env()
    except ApprovalConfigError as exc:
        return f"[yellow]not configured[/yellow] — {escape(str(exc))}"
    return (
        f"[green]home-assistant[/green] → {escape(cfg.notify_service)} "
        f"(timeout {cfg.timeout_s}s, {escape(cfg.url)})"
    )


async def _pending_rows(session: Any) -> list[tuple[str, str, str, str]]:
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
    out: list[tuple[str, str, str, str]] = []
    for p in rows:
        if (p.artifact or {}).get("kind") != "action":
            continue
        try:
            m = parse_manifest(p)
        except ManifestError as exc:
            out.append((str(p.id)[:8], "invalid manifest", str(exc)[:60], ""))
            continue
        action = ActionRequest(
            domain=m.domain,
            action_kind=m.action_kind,
            blast_radius=m.blast_radius,
            hostnames=m.hostnames,
            rollback_verified=False,
            provenance=p.proposed_by,
        )
        decision = decide(action, await load_trust_context(session, action))
        would = {
            "confirm": "asks you (tap)",
            "autonomous": "runs unattended",
            "propose": "refused",
            "block": "refused",
        }[decision.level.value]
        out.append((str(p.id)[:8], m.cell_key, m.target_label, would))
    return out


@approvals_app.command(name="show")
def approvals_show(
    limit: int = typer.Option(10, "--limit", help="Recent answers to show."),
) -> None:
    """Channel status, what each pending proposal would get, and recent answers."""

    async def _go() -> None:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                pending = await _pending_rows(session)
                answers = (
                    (
                        await session.execute(
                            select(TrustHistory)
                            .where(TrustHistory.event == "approval")
                            .order_by(TrustHistory.at.desc())
                            .limit(max(1, limit))
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

        console.print(f"channel: {_channel_line()}")

        table = Table(title="pending action proposals — if triggered now")
        for col in ("id", "cell", "target", "would"):
            table.add_column(col, no_wrap=col in {"id", "would"})
        for row in pending:
            table.add_row(*(escape(c) for c in row))
        console.print(table)
        if not pending:
            console.print("[dim]nothing pending[/dim]")

        table = Table(title="recent answers")
        for col in ("at", "proposal", "cell", "answer", "channel", "responder"):
            table.add_column(col, no_wrap=col in {"at", "answer"})
        for h in answers:
            d = h.detail or {}
            table.add_row(
                h.at.strftime("%Y-%m-%d %H:%M"),
                str(h.proposal_id)[:8] if h.proposal_id else "",
                escape(str(d.get("cell", ""))),
                "[green]approved[/green]" if d.get("approved") else "[red]declined[/red]",
                escape(str(d.get("channel", ""))),
                escape(str(d.get("responder") or d.get("reason") or "")),
            )
        console.print(table)
        console.print(f"{len(pending)} pending, {len(answers)} recent answer(s)")

    asyncio.run(_go())


__all__ = ["approvals_app"]
