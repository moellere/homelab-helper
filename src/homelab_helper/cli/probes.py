"""`helper probes ...` subcommands — probe plugin inspection and registration."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from homelab_helper.cli._probe_sync import sync_probes_sync
from homelab_helper.config import database_url as _database_url
from homelab_helper.db.models import Probe as ProbeRow
from homelab_helper.db.session import make_engine, make_sessionmaker

probes_app = typer.Typer(
    name="probes",
    help="Probe plugin inspection and registration.",
    no_args_is_help=True,
)

console = Console()


@probes_app.command(name="list")
def probes_list() -> None:
    """List Probe rows registered in the DB."""

    async def _go() -> list[ProbeRow]:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                rows = (
                    (await session.execute(select(ProbeRow).order_by(ProbeRow.name.asc())))
                    .scalars()
                    .all()
                )
                return list(rows)
        finally:
            await engine.dispose()

    rows = asyncio.run(_go())
    if not rows:
        console.print(
            "[yellow]no probes registered.[/yellow] "
            "Run [bold]helper probes register[/bold] to sync entry points."
        )
        return

    table = Table(title=f"Registered probes ({len(rows)})", show_lines=False)
    table.add_column("name", style="cyan", no_wrap=True)
    table.add_column("version")
    table.add_column("privilege")
    table.add_column("keys")
    table.add_column("module", style="dim")
    for r in rows:
        table.add_row(
            r.name,
            r.version,
            str(r.required_privilege.value),
            ", ".join(r.produces_keys),
            r.module_path,
        )
    console.print(table)


@probes_app.command(name="register")
def probes_register() -> None:
    """Re-sync probe entry points to the DB. Idempotent."""
    console.print("[cyan]syncing probe entry points...[/cyan]")
    inserted, updated = sync_probes_sync(_database_url())
    console.print(f"[green]done[/green] - inserted: {inserted}, updated: {updated}")
