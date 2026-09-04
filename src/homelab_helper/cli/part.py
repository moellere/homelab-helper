"""`helper part ...` — identity operations on PhysicalPart rows.

``show`` prints one part and its placement history; ``merge`` folds a
duplicate part (the same drive reported under a second identity by another
era of a host) into its survivor. Merge is operator-driven on purpose: no
heuristic can prove two identities are one drive, so the verb takes both
references explicitly and records the merge under ``attributes.merged_from``.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from homelab_helper.config import database_url as _database_url
from homelab_helper.db.models import Host, Placement
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.retire import PartLookupError, find_part, merge_parts

part_app = typer.Typer(
    name="part", help="Physical-part identity: show and merge.", no_args_is_help=True
)

console = Console()


@part_app.command(name="show")
def part_show(
    ref: str = typer.Argument(..., help="Part id prefix, serial, or WWID."),
) -> None:
    """Print a part's identity and placement history."""

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                try:
                    part = await find_part(session, ref)
                except PartLookupError as exc:
                    console.print(f"[red]{exc}[/red]")
                    raise typer.Exit(code=2) from exc
                console.print(
                    f"[bold]{part.kind.value}[/bold] {part.manufacturer or ''} {part.model or ''}  "
                    f"[dim]({part.id})[/dim]"
                )
                console.print(
                    f"[dim]serial:[/dim] {part.serial or '—'}  [dim]wwid:[/dim] {part.wwid or '—'}"
                )
                merged = (part.attributes or {}).get("merged_from") or []
                if merged:
                    console.print(
                        f"[dim]merged from:[/dim] {', '.join(m.get('serial') or m.get('id', '?')[:8] for m in merged)}"
                    )
                rows = (
                    await session.execute(
                        select(Placement, Host)
                        .join(Host, Host.id == Placement.host_id)
                        .where(Placement.part_id == part.id)
                        .order_by(Placement.from_date)
                    )
                ).all()
                table = Table(title="placements")
                for col in ("host", "slot", "from", "to"):
                    table.add_column(col, no_wrap=True)
                for placement, host in rows:
                    table.add_row(
                        host.hostname,
                        placement.slot,
                        placement.from_date.date().isoformat(),
                        placement.to_date.date().isoformat()
                        if placement.to_date
                        else "[green]open[/green]",
                    )
                console.print(table)
        finally:
            await engine.dispose()

    asyncio.run(_go())


@part_app.command(name="merge")
def part_merge(
    duplicate: str = typer.Argument(..., help="The part to fold in (id prefix, serial, or WWID)."),
    into: str = typer.Option(
        ..., "--into", help="The surviving part (id prefix, serial, or WWID)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Fold a duplicate part into its survivor: placements move, identity gaps fill, duplicate is deleted."""

    async def _go() -> int:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                try:
                    dup = await find_part(session, duplicate)
                    keep = await find_part(session, into)
                except PartLookupError as exc:
                    console.print(f"[red]{exc}[/red]")
                    return 2
                label = f"{dup.kind.value} {dup.serial or dup.wwid or str(dup.id)[:8]} -> {keep.serial or keep.wwid or str(keep.id)[:8]}"
                if not yes and not typer.confirm(
                    f"Merge {label}? The duplicate row is deleted.", default=False
                ):
                    console.print("[yellow]aborted[/yellow]")
                    await session.rollback()
                    return 1
                try:
                    result = await merge_parts(session, dup, keep)
                except ValueError as exc:
                    console.print(f"[red]{exc}[/red]")
                    await session.rollback()
                    return 2
            console.print(
                f"[green]merged[/green] {label}: {result.placements_moved} placement(s) moved"
                + (f", filled {', '.join(result.fields_filled)}" if result.fields_filled else "")
            )
            return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


__all__ = ["part_app"]
