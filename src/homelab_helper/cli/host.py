"""`helper host ...` subcommands — per-host views and the retire verb.

``show`` is the operator-facing summary of one host (typed columns,
capability bag, current placements, recent findings). ``retire`` is the
cleanup path for decommissioned or reflashed hardware: it records a
DECOMMISSIONING intent, closes the host's open placements explicitly, and
resolves its findings — the operator-attributed closure the reconciler's
absence rule deliberately refuses to infer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from homelab_helper.config import database_url as _database_url
from homelab_helper.db.enums import FindingStatus
from homelab_helper.db.models import (
    Host,
    PhysicalPart,
    Placement,
    ReconciliationFinding,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.retire import is_retired, retire_host
from homelab_helper.engine.trust import operator_identity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

host_app = typer.Typer(
    name="host",
    help="Per-host views and operations.",
    no_args_is_help=True,
)

console = Console()


async def _load_host(session: AsyncSession, hostname: str) -> Host:
    host = (
        await session.execute(select(Host).where(Host.hostname == hostname))
    ).scalar_one_or_none()
    if host is None:
        console.print(f"[red]no host named {hostname!r}[/red]")
        raise typer.Exit(code=2)
    return host


_BYTES_UNITS: tuple[tuple[int, str], ...] = (
    (1024**4, "TiB"),
    (1024**3, "GiB"),
    (1024**2, "MiB"),
    (1024, "KiB"),
)
_MAX_CAPABILITY_LIST_PREVIEW = 8


def _format_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    for boundary, unit in _BYTES_UNITS:
        if n >= boundary:
            return f"{n / boundary:.1f} {unit}"
    return f"{n} B"


def _format_iso(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.isoformat()


@host_app.command(name="show")
def host_show(
    hostname: str = typer.Argument(..., help="Hostname (must already exist in the harness DB)."),
) -> None:
    """Print everything the harness knows about a host."""

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                host = await _load_host(session, hostname)
                _print_identity(host)
                if await is_retired(session, host.id):
                    console.print("[yellow]RETIRED[/yellow] — decommissioning intent recorded")
                _print_capabilities(host)
                await _print_placements(session, host)
                await _print_recent_findings(session, host)
        finally:
            await engine.dispose()

    asyncio.run(_go())


@host_app.command(name="retire")
def host_retire(
    hostname: str = typer.Argument(..., help="Hostname (must already exist in the harness DB)."),
    rationale: str | None = typer.Option(
        None, "--rationale", "-r", help="Why (recorded on the intent)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Mark a host decommissioned: record the intent, close its placements, resolve its findings."""
    if not yes:
        confirmed = typer.confirm(
            f"Retire {hostname}? This closes its open placements and resolves its findings.",
            default=False,
        )
        if not confirmed:
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(code=1)

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                host = await _load_host(session, hostname)
                result = await retire_host(
                    session, host, declared_by=operator_identity(), rationale=rationale
                )
        finally:
            await engine.dispose()
        state = "already retired" if result.already_retired else "retired"
        console.print(
            f"[green]{state}[/green] {result.hostname}: "
            f"{len(result.placements_closed)} placement(s) closed, "
            f"{len(result.findings_resolved)} finding(s) resolved"
        )
        for serial, slot in result.placements_closed:
            console.print(f"  [dim]- placement[/dim] {serial or '?'} @ {slot}")
        for fingerprint in result.findings_resolved:
            console.print(f"  [dim]- finding[/dim] {fingerprint}")

    asyncio.run(_go())


def _print_identity(host: Host) -> None:
    console.print(f"[bold]{host.hostname}[/bold]  [dim]({host.id})[/dim]")
    console.print(f"[dim]primary_ip:[/dim]  {host.primary_ip or '—'}")
    console.print(f"[dim]arch:[/dim]        {host.arch.value}")
    console.print(
        f"[dim]power policy:[/dim] {host.power_policy.value}  "
        f"[dim]expected:[/dim] {host.expected_power_state.value}"
    )
    console.print(
        f"[dim]discovery:[/dim]   {host.discovery_source.value}  "
        f"[dim]last run:[/dim] {_format_iso(host.discovery_last_run)}  "
        f"[dim]last verified:[/dim] {_format_iso(host.last_verified)}"
    )


def _print_capabilities(host: Host) -> None:
    caps = host.capabilities or {}
    if not caps:
        console.print("\n[dim]no capabilities recorded yet[/dim]")
        return
    console.print("\n[bold]capabilities[/bold]")
    # Group by prefix for readability.
    groups: dict[str, list[tuple[str, object]]] = {}
    for k in sorted(caps.keys()):
        prefix = k.split("_", 1)[0] if "_" in k else "other"
        groups.setdefault(prefix, []).append((k, caps[k]))
    for prefix, entries in groups.items():
        console.print(f"  [cyan]{prefix}[/cyan]")
        for k, v in entries:
            if isinstance(v, list) and len(v) > _MAX_CAPABILITY_LIST_PREVIEW:
                shown: object = f"{v[:_MAX_CAPABILITY_LIST_PREVIEW]}… ({len(v)} items)"
            else:
                shown = v
            console.print(f"    [dim]{k}[/dim] = {shown!r}")


async def _print_placements(session: AsyncSession, host: Host) -> None:
    stmt = (
        select(Placement, PhysicalPart)
        .join(PhysicalPart, Placement.part_id == PhysicalPart.id)
        .where(Placement.host_id == host.id, Placement.to_date.is_(None))
        .order_by(PhysicalPart.kind, Placement.slot)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        console.print("\n[dim]no current part placements[/dim]")
        return
    console.print("\n[bold]parts (current placements)[/bold]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("kind", no_wrap=True)
    table.add_column("slot", no_wrap=True)
    table.add_column("identity", overflow="fold")
    table.add_column("manufacturer")
    table.add_column("model", overflow="fold")
    table.add_column("size")
    for placement, part in rows:
        identity = part.wwid or part.serial or "—"
        size_str = _format_bytes(part.capacity_bytes) if part.capacity_bytes else "—"
        table.add_row(
            part.kind.value,
            placement.slot,
            identity,
            part.manufacturer or "—",
            part.model or "—",
            size_str,
        )
    console.print(table)


async def _print_recent_findings(session: AsyncSession, host: Host) -> None:
    """Open or acknowledged findings affecting this host."""
    stmt = (
        select(ReconciliationFinding)
        .where(ReconciliationFinding.status.in_([FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED]))
        .order_by(ReconciliationFinding.last_seen.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    host_id_str = str(host.id)
    affecting = [
        f
        for f in rows
        if any(
            a.get("target_type") == "host" and a.get("target_id") == host_id_str
            for a in (f.affected or [])
        )
    ]
    if not affecting:
        console.print("\n[dim]no open findings for this host[/dim]")
        return
    console.print("\n[bold]open findings[/bold]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("fingerprint", style="dim", no_wrap=True)
    table.add_column("sev")
    table.add_column("status")
    table.add_column("title", overflow="fold")
    table.add_column("last seen", no_wrap=True)
    for f in affecting:
        table.add_row(
            f.fingerprint,
            f.severity.value,
            f.status.value,
            f.title,
            f.last_seen.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)
