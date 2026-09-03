"""``helper window ...`` — time-boxed lifts of the soft-hard floors (Phase 6).

Windows are the deliberate, logged way to let the framework cross the
verified-rollback floor and non-absolute host ceilings for a while: scoped to
specific domains, hosts, or cells (never blanket), hard expiry, no auto-renew.
``kill`` is the reactive backstop — one gesture closes every open window, and
any in-flight run re-checks its window immediately before dispatch.

Absolute floors (`secrets`, a host boundary marked absolute) are not reachable
from here at all; they move only by editing policy config.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from homelab_helper.config import database_url
from homelab_helper.db.enums import TrustDomain
from homelab_helper.db.models import ElevationWindow
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.trust import (
    MAX_WINDOW_MINUTES,
    WindowError,
    kill_switch,
    open_window,
    operator_identity,
    revoke_window,
)

window_app = typer.Typer(
    name="window",
    help="Elevation windows: scoped, expiring lifts of the soft-hard floors.",
    no_args_is_help=True,
)

console = Console()


def _remaining(window: ElevationWindow, now: datetime) -> str:
    expires = (
        window.expires_at.replace(tzinfo=UTC)
        if window.expires_at.tzinfo is None
        else window.expires_at
    )
    if window.revoked_at is not None:
        return "[red]revoked[/red]"
    if now >= expires:
        return "[dim]expired[/dim]"
    minutes = int((expires - now).total_seconds() // 60)
    return f"[green]{minutes}m left[/green]"


def _scope_label(window: ElevationWindow) -> str:
    scope = window.scope or {}
    parts = [
        f"{key}: {', '.join(scope.get(key) or [])}"
        for key in ("domains", "hosts", "cells")
        if scope.get(key)
    ]
    return " | ".join(parts)


@window_app.command(name="open")
def window_open(
    reason: str = typer.Option(..., "--reason", help="Why this window exists (audit record)."),
    minutes: int = typer.Option(60, "--minutes", help=f"Duration, max {MAX_WINDOW_MINUTES}."),
    domain: list[str] = typer.Option([], "--domain", help="Scope to a trust domain."),
    host: list[str] = typer.Option([], "--host", help="Scope to a hostname."),
    cell: list[str] = typer.Option([], "--cell", help="Scope to domain/action/blast."),
) -> None:
    """Open a scoped, expiring window. At least one scope flag is required."""
    domains = []
    for name in domain:
        try:
            domains.append(TrustDomain(name.lower()))
        except ValueError as exc:
            allowed = ", ".join(d.value for d in TrustDomain)
            raise typer.BadParameter(f"domain must be one of: {allowed}") from exc

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                try:
                    window = await open_window(
                        session,
                        reason=reason,
                        minutes=minutes,
                        actor=operator_identity(),
                        domains=domains,
                        hosts=host,
                        cells=cell,
                    )
                except WindowError as exc:
                    console.print(f"[red]refused:[/red] {escape(str(exc))}")
                    return 2
                console.print(
                    f"[yellow]window open[/yellow] {window.id} — expires "
                    f"{window.expires_at:%Y-%m-%d %H:%M} UTC, no auto-renew"
                )
                console.print(f"[dim]scope: {escape(_scope_label(window))}[/dim]")
                return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@window_app.command(name="list")
def window_list(
    all_windows: bool = typer.Option(False, "--all", help="Include expired and revoked windows."),
) -> None:
    """Show elevation windows, newest first."""

    async def _go() -> None:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                rows = (
                    (
                        await session.execute(
                            select(ElevationWindow).order_by(ElevationWindow.opened_at.desc())
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

        now = datetime.now(UTC)
        shown = [
            w
            for w in rows
            if all_windows
            or (
                w.revoked_at is None
                and now
                < (
                    w.expires_at.replace(tzinfo=UTC)
                    if w.expires_at.tzinfo is None
                    else w.expires_at
                )
            )
        ]
        table = Table(title="elevation windows")
        for col in ("id", "state", "opened by", "reason", "scope"):
            table.add_column(col, no_wrap=col in {"id", "state"})
        for w in shown:
            table.add_row(
                str(w.id)[:8],
                _remaining(w, now),
                escape(w.opened_by),
                escape(w.reason),
                escape(_scope_label(w)),
            )
        console.print(table)
        if not shown:
            console.print("[dim]no open windows — standing floors apply[/dim]")
        console.print(f"{len(shown)} window(s)")

    asyncio.run(_go())


@window_app.command(name="revoke")
def window_revoke(
    window_id: str = typer.Argument(..., help="Window id (or unique prefix)."),
) -> None:
    """Close one window now."""

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                rows = (await session.execute(select(ElevationWindow))).scalars().all()
                matches = [w for w in rows if str(w.id).startswith(window_id.lower())]
                if not matches:
                    console.print(f"[red]no window matches[/red] {escape(window_id)}")
                    return 1
                if len(matches) > 1:
                    console.print(f"[red]ambiguous prefix[/red] — {len(matches)} matches")
                    return 1
                if await revoke_window(session, matches[0], actor=operator_identity()):
                    console.print(f"[green]revoked[/green] window {matches[0].id}")
                    return 0
                console.print("[dim]window was already closed[/dim]")
                return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@window_app.command(name="kill")
def window_kill(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Kill switch: revoke every open window at once.

    In-flight autonomous work stops at its next checkpoint — the executor
    re-tests its window immediately before dispatch.
    """

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                if not yes and not typer.confirm(
                    "revoke ALL open elevation windows?", default=False
                ):
                    console.print("[dim]left as-is[/dim]")
                    return 3
                closed = await kill_switch(session, actor=operator_identity())
                console.print(
                    f"[red]kill switch[/red] — {closed} window(s) revoked; "
                    "standing floors apply again"
                )
                return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


__all__ = ["window_app"]
