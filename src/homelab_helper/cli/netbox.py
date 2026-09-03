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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from homelab_helper.adapters.netbox import (
    NetBoxAdapter,
    NetBoxAPIError,
    NetBoxConfig,
    NetBoxConfigError,
    PartPlacement,
    SyncInventoryResult,
)
from homelab_helper.config import database_url as _database_url
from homelab_helper.db.models import Cluster, Host, PhysicalPart, Placement
from homelab_helper.db.session import make_engine, make_sessionmaker
from homelab_helper.engine.netbox_vm_sync import sync_cluster_to_netbox

netbox_app = typer.Typer(
    name="netbox",
    help="NetBox bridge: bootstrap custom fields and sync harness state.",
    no_args_is_help=True,
)

console = Console()


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


async def _current_placements(session: AsyncSession, host: Host) -> list[PartPlacement]:
    """Read open Placement rows for ``host`` and project onto ``PartPlacement``."""
    rows = (
        await session.execute(
            select(Placement, PhysicalPart)
            .join(PhysicalPart, Placement.part_id == PhysicalPart.id)
            .where(Placement.host_id == host.id, Placement.to_date.is_(None))
            .order_by(Placement.slot)
        )
    ).all()
    out: list[PartPlacement] = []
    for placement, part in rows:
        out.append(
            PartPlacement(
                slot=placement.slot,
                part_kind=part.kind.value,
                serial=part.serial,
                wwid=part.wwid,
                manufacturer=part.manufacturer,
                model=part.model,
                capacity_bytes=part.capacity_bytes,
            )
        )
    return out


def _print_inventory_result(result: SyncInventoryResult, *, dry_run: bool) -> None:
    header = "[dim](dry run)[/dim] " if dry_run else ""
    console.print(
        f"{header}[bold]inventory[/bold]: "
        f"[green]{len(result.created)}[/green] created, "
        f"[yellow]{len(result.updated)}[/yellow] updated, "
        f"[red]{len(result.deleted)}[/red] deleted, "
        f"[dim]{len(result.unchanged)}[/dim] unchanged"
    )
    for slot in result.created:
        console.print(f"  [green]+[/green] {slot}")
    for slot in result.updated:
        console.print(f"  [yellow]~[/yellow] {slot}")
    for slot in result.deleted:
        console.print(f"  [red]-[/red] {slot}")


@netbox_app.command(name="sync-host")
def netbox_sync_host(
    hostname: str = typer.Argument(..., help="Harness hostname (must exist locally)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would happen without sending writes to NetBox."
    ),
    skip_fields: bool = typer.Option(
        False, "--skip-fields", help="Skip the cf_* custom-field PATCH."
    ),
    skip_inventory: bool = typer.Option(
        False, "--skip-inventory", help="Skip the InventoryItem placement sync."
    ),
) -> None:
    """Push a harness Host row's CF values + part placements onto NetBox.

    Matches by ``Device.name == Host.hostname``. The harness never creates
    Devices — operator owns Device creation in NetBox first. Missing Devices
    are reported with a clear "create the Device, then re-run" message.

    By default this runs two passes:

    1. ``custom_fields`` PATCH on the Device (power policy, arch, capabilities…).
    2. ``InventoryItem`` sync — current open ``Placement`` rows are mirrored
       under the Device. Items with ``discovered=True`` not in the harness's
       active set are deleted; new placements create; matching slots with
       diverged fields update.

    ``--skip-fields`` / ``--skip-inventory`` constrain either pass.
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

                placements = await _current_placements(session, host)

                adapter = _load_adapter()
                try:
                    return await _run_sync_passes(
                        adapter,
                        host,
                        placements,
                        skip_fields=skip_fields,
                        skip_inventory=skip_inventory,
                        dry_run=dry_run,
                    )
                finally:
                    await adapter.aclose()
        finally:
            await db_engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@netbox_app.command(name="sync-cluster")
def netbox_sync_cluster(
    name: str = typer.Argument(..., help="Harness cluster name (must exist locally)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview NetBox writes; don't persist id write-back."
    ),
) -> None:
    """Sync a harness cluster's VirtualMachine rows into NetBox; write NetBox ids back.

    Reads the cluster + VMs already persisted by ``helper discover proxmox
    --persist`` and upserts them into the matching NetBox cluster (which the
    operator owns — a missing one is reported, not created). The resulting
    ``netbox_cluster_id`` / ``netbox_vm_id`` are written back onto the harness
    rows so later syncs match by id. ``--dry-run`` previews and rolls back.
    """

    async def _go() -> int:
        db_engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(db_engine)
            async with sm() as session:
                cluster = (
                    await session.execute(select(Cluster).where(Cluster.name == name))
                ).scalar_one_or_none()
                if cluster is None:
                    console.print(f"[red]no harness cluster named {name!r}[/red]")
                    return 2

                adapter = _load_adapter()
                try:
                    result = await sync_cluster_to_netbox(
                        session, adapter, cluster, when=datetime.now(UTC), dry_run=dry_run
                    )
                finally:
                    await adapter.aclose()

                if not result.found:
                    console.print(f"[yellow]netbox:[/yellow] {result.reason}")
                    return 1
                if not dry_run:
                    await session.commit()
                diverged = (
                    f", [yellow]{len(result.diverged)} diverged (skipped)[/yellow]"
                    if result.diverged
                    else ""
                )
                console.print(
                    f"[green]synced[/green] {name}: {len(result.created)} created, "
                    f"{len(result.updated)} updated, {len(result.unchanged)} unchanged"
                    + diverged
                    + (" [dim](dry-run, rolled back)[/dim]" if dry_run else " (ids written back)")
                )
                return 0
        finally:
            await db_engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


async def _run_sync_passes(
    adapter: NetBoxAdapter,
    host: Host,
    placements: list[PartPlacement],
    *,
    skip_fields: bool,
    skip_inventory: bool,
    dry_run: bool,
) -> int:
    """Lookup → optional CF PATCH → optional inventory sync. Returns exit code."""
    try:
        device = await adapter.get_device_by_name(host.hostname)
    except NetBoxAPIError as exc:
        console.print(f"[red]NetBox API error:[/red] {exc}")
        return 1
    if device is None:
        console.print(
            f"[yellow]skipped:[/yellow] no NetBox Device named {host.hostname!r}. "
            "Create the Device in NetBox first, then re-run."
        )
        return 0
    device_id = device["id"]
    header = "[dim](dry run)[/dim] " if dry_run else ""

    if not skip_fields:
        try:
            result = await adapter.sync_host(host, dry_run=dry_run)
        except NetBoxAPIError as exc:
            console.print(f"[red]NetBox API error:[/red] {exc}")
            return 1
        console.print(f"{header}[green]synced[/green] {host.hostname} → NetBox Device #{device_id}")
        table = Table(show_header=True, header_style="bold")
        table.add_column("custom field", no_wrap=True)
        table.add_column("value", overflow="fold")
        for k, v in result.patch.get("custom_fields", {}).items():
            table.add_row(k, repr(v))
        console.print(table)

    if not skip_inventory:
        try:
            inv_result = await adapter.sync_inventory_items(device_id, placements, dry_run=dry_run)
        except NetBoxAPIError as exc:
            console.print(f"[red]NetBox API error:[/red] {exc}")
            return 1
        _print_inventory_result(inv_result, dry_run=dry_run)
    return 0
