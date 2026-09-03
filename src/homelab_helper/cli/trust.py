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
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from homelab_helper.config import database_url
from homelab_helper.db.enums import AutonomyLevel, TrustDomain
from homelab_helper.db.models import (
    CellTrust,
    Domain,
    ElevationWindow,
    Host,
    TrustBoundary,
    TrustHistory,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.escalation import PROMOTION_STREAK, is_promotable
from homelab_helper.engine.trust import (
    GrantError,
    grant_cell,
    operator_identity,
    set_boundary,
)

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
                boundaries = (
                    (
                        await session.execute(
                            select(TrustBoundary, Host.hostname).join(
                                Host, Host.id == TrustBoundary.host_id, isouter=True
                            )
                        )
                    )
                    .tuples()
                    .all()
                )
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
                streak = f"{c.clean_streak}/{PROMOTION_STREAK}"
                if c.on_probation:
                    streak += " [red](probation)[/red]"
                elif not is_promotable(c.action_kind, c.blast_radius):
                    streak += " [dim](grant-only)[/dim]"
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
        for b, hostname in boundaries:
            absolute = " [red]absolute (window-proof)[/red]" if b.absolute else ""
            console.print(
                f"[dim]boundary {escape(hostname or str(b.host_id))} ≤ "
                f"{b.max_agent_authority.value}[/dim]{absolute}"
            )
        now = datetime.now(UTC)
        for w in windows:
            expires = (
                w.expires_at.replace(tzinfo=UTC) if w.expires_at.tzinfo is None else w.expires_at
            )
            state = "open" if (w.revoked_at is None and now < expires) else "closed"
            console.print(f"[dim]window {w.id} ({state}): {w.reason} — scope {w.scope}[/dim]")

    asyncio.run(_go())


@trust_app.command(name="boundary")
def trust_boundary(
    hostname: str = typer.Argument(..., help="Host the ceiling applies to."),
    ceiling: str = typer.Argument(..., help="block, propose, confirm, or autonomous."),
    absolute: bool = typer.Option(
        False,
        "--absolute",
        help="Window-proof: no elevation window or override lifts it, ever.",
    ),
    notes: str = typer.Option("", "--notes", help="Why this host is capped."),
) -> None:
    """Cap one host's agent authority — the per-host ceiling.

    `--absolute` is the "never under any circumstances" case: it becomes
    unreachable by any runtime gesture, and only a policy-config edit
    changes it.
    """
    try:
        parsed = AutonomyLevel(ceiling.lower())
    except ValueError as exc:
        allowed = ", ".join(a.value for a in AutonomyLevel)
        raise typer.BadParameter(f"ceiling must be one of: {allowed}") from exc

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                host = (
                    await session.execute(select(Host).where(Host.hostname == hostname))
                ).scalar_one_or_none()
                if host is None:
                    console.print(f"[red]no such host[/red] {escape(hostname)}")
                    return 1
                boundary = await set_boundary(
                    session,
                    host,
                    parsed,
                    absolute=absolute,
                    actor=operator_identity(),
                    notes=notes or None,
                )
                mark = " [red](absolute — window-proof)[/red]" if boundary.absolute else ""
                console.print(
                    f"[green]boundary set[/green] {escape(hostname)} ≤ "
                    f"{boundary.max_agent_authority.value}{mark}"
                )
                return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@trust_app.command(name="history")
def trust_history(
    limit: int = typer.Option(30, "--limit", help="Most recent events to show."),
) -> None:
    """Print the append-only audit spine, newest first."""

    async def _go() -> None:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                rows = (
                    (
                        await session.execute(
                            select(TrustHistory)
                            .order_by(TrustHistory.at.desc())
                            .limit(max(limit, 1))
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

        table = Table(title="trust history")
        for col in ("at", "actor", "event", "domain", "detail"):
            table.add_column(col, no_wrap=col in {"at", "event"})
        for row in rows:
            detail = row.detail or {}
            if row.event in {"auto-promote", "demote"}:
                summary = (
                    f"{detail.get('action_kind')}/{detail.get('blast_radius')}: "
                    f"{detail.get('from')} → {detail.get('to')}"
                )
                cause = detail.get("cause")
                if cause:
                    summary += f" ({cause})"
            elif row.event == "grant":
                summary = (
                    f"{detail.get('action_kind')}/{detail.get('blast_radius')} = "
                    f"{detail.get('level')}"
                )
            else:
                summary = ", ".join(f"{k}={v}" for k, v in detail.items())
            style = {"auto-promote": "green", "demote": "red"}.get(row.event, "white")
            table.add_row(
                row.at.strftime("%Y-%m-%d %H:%M"),
                escape(row.actor),
                f"[{style}]{row.event}[/{style}]",
                row.domain.value if row.domain else "",
                escape(summary),
            )
        console.print(table)
        console.print(f"{len(rows)} event(s)")

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
