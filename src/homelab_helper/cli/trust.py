"""`helper trust ...` — the authorization surface (Phase 6).

``show`` prints the gradient's current state: domain policy, granted cells,
host boundaries, and open windows. ``grant`` sets one cell's level
(operator-attributed, refused above the domain ceiling, always recorded in
``TrustHistory``). With no grants, every cell sits at PROPOSE — the L1 floor.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from homelab_helper.config import database_url
from homelab_helper.db.enums import AutonomyLevel, TrustDomain
from homelab_helper.db.models import CellTrust, Domain, ElevationWindow, TrustBoundary
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.trust import GrantError, grant_cell, operator_identity

trust_app = typer.Typer(
    name="trust",
    help="Trust gradient: show and grant per-cell action authority.",
    no_args_is_help=True,
)

console = Console()

_LEVEL_STYLE = {
    "block": "red",
    "propose": "dim",
    "confirm": "yellow",
    "autonomous": "green",
}


def _chip(level: AutonomyLevel) -> str:
    style = _LEVEL_STYLE.get(level.value, "white")
    return f"[{style}]{level.value}[/{style}]"


@trust_app.command(name="show")
def trust_show() -> None:
    """Print domains, granted cells, boundaries, and windows."""

    async def _go() -> None:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                domains = (
                    (await session.execute(select(Domain).order_by(Domain.name))).scalars().all()
                )
                cells = (
                    (await session.execute(select(CellTrust).order_by(CellTrust.domain)))
                    .scalars()
                    .all()
                )
                boundaries = (await session.execute(select(TrustBoundary))).scalars().all()
                windows = (await session.execute(select(ElevationWindow))).scalars().all()
        finally:
            await engine.dispose()

        dt = Table(title="trust domains")
        for col in ("domain", "default", "max", "absolute"):
            dt.add_column(col, no_wrap=True)
        for d in domains:
            dt.add_row(
                d.name.value,
                _chip(d.default_level),
                _chip(d.max_level),
                "[red]yes[/red]" if d.is_absolute else "no",
            )
        console.print(dt)

        if cells:
            ct = Table(title="granted cells")
            for col in ("domain", "action", "blast radius", "level", "granted by", "streak"):
                ct.add_column(col, no_wrap=True)
            for c in cells:
                streak = str(c.clean_streak) + (" [red](probation)[/red]" if c.on_probation else "")
                ct.add_row(
                    c.domain.value,
                    c.action_kind,
                    c.blast_radius,
                    _chip(c.level),
                    c.granted_by or "[dim]auto[/dim]",
                    streak,
                )
            console.print(ct)
        else:
            console.print(
                "[dim]no cells granted — every action is propose-only (the L1 floor)[/dim]"
            )
        for b in boundaries:
            absolute = " [red]absolute[/red]" if b.absolute else ""
            console.print(
                f"[dim]boundary host={b.host_id} ceiling={b.max_agent_authority.value}"
                f"{absolute}[/dim]"
            )
        now = datetime.now(UTC)
        for w in windows:
            expires = (
                w.expires_at.replace(tzinfo=UTC) if w.expires_at.tzinfo is None else w.expires_at
            )
            state = "open" if (w.revoked_at is None and now < expires) else "closed"
            console.print(f"[dim]window {w.id} ({state}): {w.reason} — scope {w.scope}[/dim]")

    asyncio.run(_go())


@trust_app.command(name="grant")
def trust_grant(
    domain: str = typer.Argument(..., help="Trust domain (e.g. hypervisor, containers)."),
    action_kind: str = typer.Argument(..., help='Action kind (e.g. "restart").'),
    blast_radius: str = typer.Argument(..., help='Blast radius (e.g. "single-host").'),
    level: str = typer.Argument(..., help="block, propose, confirm, or autonomous."),
) -> None:
    """Grant one cell's authority level (recorded in TrustHistory)."""
    try:
        parsed_domain = TrustDomain(domain.lower())
    except ValueError as exc:
        allowed = ", ".join(d.value for d in TrustDomain)
        raise typer.BadParameter(f"domain must be one of: {allowed}") from exc
    try:
        parsed_level = AutonomyLevel(level.lower())
    except ValueError as exc:
        allowed = ", ".join(a.value for a in AutonomyLevel)
        raise typer.BadParameter(f"level must be one of: {allowed}") from exc

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                try:
                    cell = await grant_cell(
                        session,
                        parsed_domain,
                        action_kind,
                        blast_radius,
                        parsed_level,
                        actor=operator_identity(),
                    )
                except GrantError as exc:
                    console.print(f"[red]refused:[/red] {exc}")
                    return 2
                console.print(
                    f"[green]granted[/green] {cell.domain.value}/{cell.action_kind}/"
                    f"{cell.blast_radius} = {cell.level.value} "
                    f"(by {cell.granted_by})"
                )
                return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


__all__ = ["trust_app"]
