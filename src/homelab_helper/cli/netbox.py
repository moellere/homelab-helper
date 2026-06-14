"""``helper netbox ...`` subcommands — bridge into a live NetBox instance.

Reads credentials from the environment (``HOMELAB_HELPER_NETBOX_URL`` and
``HOMELAB_HELPER_NETBOX_TOKEN``). Three verbs today:

- ``health``       — ping NetBox, print version + plugin list.
- ``bootstrap``    — idempotently create the harness's custom fields.
- ``sync-host``    — push one harness Host row's CF values onto the matching
                     NetBox Device.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from homelab_helper.adapters.netbox import (
    NetBoxAdapter,
    NetBoxAPIError,
    NetBoxConfig,
    NetBoxConfigError,
)
from homelab_helper.db.models import Host
from homelab_helper.db.session import make_engine, make_sessionmaker

netbox_app = typer.Typer(
    name="netbox",
    help="NetBox bridge: bootstrap custom fields and sync harness state.",
    no_args_is_help=True,
)

console = Console()


def _database_url() -> str:
    return os.environ.get("HOMELAB_HELPER_DATABASE_URL") or "sqlite+aiosqlite:///./homelab.db"


def _load_adapter() -> NetBoxAdapter:
    try:
        return NetBoxAdapter(NetBoxConfig.from_env())
    except NetBoxConfigError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


@netbox_app.command(name="health")
def netbox_health() -> None:
    """Ping NetBox at ``/api/status/`` and print the version banner."""

    async def _go() -> int:
        adapter = _load_adapter()
        try:
            try:
                status = await adapter.health_check()
            except (NetBoxAPIError, httpx.HTTPError) as exc:
                console.print(f"[red]NetBox unreachable:[/red] {exc}")
                return 1
        finally:
            await adapter.aclose()
        version = status.get("netbox-version") or status.get("version") or "?"
        console.print(f"[green]NetBox reachable[/green]: version [bold]{version}[/bold]")
        plugins = status.get("plugins") or {}
        if plugins:
            console.print(f"[dim]plugins:[/dim] {', '.join(sorted(plugins))}")
        return 0

    raise typer.Exit(code=asyncio.run(_go()))


@netbox_app.command(name="bootstrap")
def netbox_bootstrap(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would be created without making any API calls beyond list.",
    ),
) -> None:
    """Ensure the harness's custom fields exist on the configured NetBox.

    Idempotent: lists existing CFs first, only creates the ones the harness
    needs that aren't already present. Safe to re-run after upstream NetBox
    upgrades.
    """

    async def _go() -> int:
        adapter = _load_adapter()
        try:
            try:
                result = await adapter.bootstrap_custom_fields(dry_run=dry_run)
            except NetBoxAPIError as exc:
                console.print(f"[red]NetBox API error:[/red] {exc}")
                return 1
        finally:
            await adapter.aclose()

        header = "[dim](dry run)[/dim] " if dry_run else ""
        console.print(
            f"{header}[green]{len(result.created)}[/green] created, "
            f"[dim]{len(result.already_present)}[/dim] already present, "
            f"[red]{len(result.failed)}[/red] failed "
            f"[dim](of {result.total} CFs)[/dim]"
        )
        for name in result.created:
            console.print(f"  [green]+[/green] {name}")
        for name in result.already_present:
            console.print(f"  [dim]=[/dim] {name}")
        for name, reason in result.failed:
            console.print(f"  [red]![/red] {name}  [dim]{reason}[/dim]")
        return 1 if result.failed else 0

    raise typer.Exit(code=asyncio.run(_go()))


@netbox_app.command(name="sync-host")
def netbox_sync_host(
    hostname: str = typer.Argument(..., help="Harness hostname (must exist locally)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the PATCH body without sending it to NetBox."
    ),
) -> None:
    """Push a harness Host row's CF values onto the matching NetBox Device.

    Matches by ``Device.name == Host.hostname``. The harness never creates
    Devices — operator owns Device creation in NetBox first. Missing Devices
    are reported with a clear "create the Device, then re-run" message.
    """

    async def _go() -> int:
        db_engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(db_engine)
            async with sm() as session:
                host = (
                    await session.execute(select(Host).where(Host.hostname == hostname))
                ).scalar_one_or_none()
                if host is None:
                    console.print(f"[red]no harness Host named {hostname!r}[/red]")
                    return 2

                adapter = _load_adapter()
                try:
                    try:
                        result = await adapter.sync_host(host, dry_run=dry_run)
                    except NetBoxAPIError as exc:
                        console.print(f"[red]NetBox API error:[/red] {exc}")
                        return 1
                finally:
                    await adapter.aclose()
        finally:
            await db_engine.dispose()

        if not result.found:
            console.print(
                f"[yellow]skipped:[/yellow] {result.skipped_reason}. "
                "Create the Device in NetBox first, then re-run."
            )
            return 0

        header = "[dim](dry run)[/dim] " if dry_run else ""
        console.print(
            f"{header}[green]synced[/green] {hostname} → NetBox Device #{result.device_id}"
        )
        table = Table(show_header=True, header_style="bold")
        table.add_column("custom field", no_wrap=True)
        table.add_column("value", overflow="fold")
        for k, v in result.patch.get("custom_fields", {}).items():
            table.add_row(k, repr(v))
        console.print(table)
        return 0

    raise typer.Exit(code=asyncio.run(_go()))
