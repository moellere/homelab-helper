"""``helper digest ...`` — the weekly summary (Phase 8.6).

``show`` renders the page and changes nothing. ``send`` renders it, delivers
the short form to the phone, and records the window so the next digest starts
where this one stopped. ``history`` is what was sent and when.

Only ``send`` writes a ``DigestRun``: rendering a page to read must not move
the next digest's window, or looking would silently eat a week of changes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from homelab_helper.config import database_url
from homelab_helper.db.models import DigestRun
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.approval import ApprovalConfigError, HomeAssistantApprovalConfig
from homelab_helper.engine.digest import (
    build_digest,
    record_digest,
    render_markdown,
    render_notification,
)
from homelab_helper.engine.notify import send_digest

digest_app = typer.Typer(
    name="digest",
    help="Weekly summary of what changed, what is recommended, and what was done.",
    no_args_is_help=True,
)
console = Console(soft_wrap=True)


def _write_page(path: str, page: str) -> None:
    Path(path).expanduser().write_text(page, encoding="utf-8")
    console.print(f"[dim]written to {escape(path)}[/dim]")


@digest_app.command(name="show")
def digest_show(
    days: int = typer.Option(0, "--days", help="Window in days; 0 means since the last digest."),
    out: str = typer.Option("", "--out", help="Also write the Markdown page to this path."),
) -> None:
    """Render the digest page. Read-only — does not move the window."""

    async def _go() -> str:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                digest = await build_digest(session, days=days or None)
            return render_markdown(digest)
        finally:
            await engine.dispose()

    page = asyncio.run(_go())
    console.print(Markdown(page))
    if out:
        _write_page(out, page)
    console.print("[dim]not recorded — `helper digest send` delivers and moves the window[/dim]")
    raise typer.Exit(code=0)


@digest_app.command(name="send")
def digest_send(
    days: int = typer.Option(0, "--days", help="Window in days; 0 means since the last digest."),
    quiet_ok: bool = typer.Option(
        False, "--quiet-ok", help="Send even when nothing happened in the window."
    ),
    out: str = typer.Option("", "--out", help="Also write the Markdown page to this path."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Render and report, but neither notify nor record."
    ),
) -> None:
    """Deliver the digest and record its window.

    A quiet window is recorded but not sent unless ``--quiet-ok``: a weekly
    buzz that says "nothing happened" is the noise this slice exists to stop.
    """

    pages: list[str] = []

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                digest = await build_digest(session, days=days or None)
                page = render_markdown(digest)
                pages.append(page)
                title, message = render_notification(digest)

                if dry_run:
                    console.print(Markdown(page))
                    console.print(f"[dim]would send:[/dim] {escape(title)} — {escape(message)}")
                    console.print("[dim]dry run — nothing recorded[/dim]")
                    return 0

                if digest.quiet and not quiet_ok:
                    await record_digest(session, digest, delivery="page", detail="quiet window")
                    console.print(
                        "[dim]quiet window — recorded, not sent (--quiet-ok to send anyway)[/dim]"
                    )
                    return 0

                try:
                    config = HomeAssistantApprovalConfig.from_env()
                except ApprovalConfigError as exc:
                    await record_digest(session, digest, delivery="unconfigured", detail=str(exc))
                    console.print(f"[yellow]not sent:[/yellow] {escape(str(exc))}")
                    console.print(Markdown(page))
                    return 0

                try:
                    await send_digest(config, title, message)
                except Exception as exc:
                    await record_digest(session, digest, delivery="failed", detail=str(exc))
                    console.print(f"[red]delivery failed:[/red] {escape(str(exc))}")
                    return 4

                run = await record_digest(session, digest, delivery="sent")
                console.print(
                    f"[green]sent[/green] {escape(title)} — window "
                    f"{digest.window.start:%Y-%m-%d %H:%M} → "
                    f"{digest.window.end:%Y-%m-%d %H:%M} UTC (digest {str(run.id)[:8]})"
                )
                return 0
        finally:
            await engine.dispose()

    code = asyncio.run(_go())
    if out and pages:
        _write_page(out, pages[0])
    raise typer.Exit(code=code)


@digest_app.command(name="history")
def digest_history(
    limit: int = typer.Option(10, "--limit", help="Most recent digests to show."),
) -> None:
    """What was summarised, when, and whether it was delivered."""

    async def _go() -> None:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                rows = (
                    (
                        await session.execute(
                            select(DigestRun)
                            .order_by(DigestRun.generated_at.desc())
                            .limit(max(1, limit))
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

        table = Table(title="digests")
        for col in ("id", "generated", "window", "delivery", "headline"):
            table.add_column(col, no_wrap=col in {"id", "delivery"})
        styles = {"sent": "green", "failed": "red", "unconfigured": "yellow"}
        for row in rows:
            counts = row.counts or {}
            headline = (
                f"{counts.get('executed_ok', 0)} ran, "
                f"{counts.get('opened', 0)} opened, "
                f"{counts.get('resolved', 0)} resolved"
            )
            style = styles.get(row.delivery, "dim")
            table.add_row(
                str(row.id)[:8],
                row.generated_at.strftime("%Y-%m-%d %H:%M"),
                f"{row.window_start:%m-%d %H:%M} → {row.window_end:%m-%d %H:%M}",
                f"[{style}]{row.delivery}[/{style}]",
                "[dim]quiet[/dim]" if row.quiet else headline,
            )
        console.print(table)
        console.print(f"{len(rows)} digest(s)")

    asyncio.run(_go())


__all__ = ["digest_app"]
