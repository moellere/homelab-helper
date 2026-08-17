"""`helper findings ...` subcommands — view and triage reconciliation findings.

L1 stance applies: ``ack`` / ``resolve`` / ``suppress`` change the framework's
*own* state (operator triage), never anything on a managed host. The
reconciler's auto-resolve still runs on subsequent discovery passes — manual
state here informs what the operator has reviewed, not what the world is.

Fingerprints are 16 hex chars but a prefix that uniquely identifies a row is
accepted everywhere, so ``helper findings show abc1234`` is enough in
practice.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.llm import LLMRouter, RouterRefusal, router_from_env
from homelab_helper.llm.narrator import narrate_findings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

findings_app = typer.Typer(
    name="findings",
    help="View and triage reconciliation findings.",
    no_args_is_help=True,
)

console = Console()

_SEVERITY_COLOR = {
    FindingSeverity.CRITICAL: "bold red",
    FindingSeverity.HIGH: "red",
    FindingSeverity.MEDIUM: "yellow",
    FindingSeverity.LOW: "cyan",
    FindingSeverity.INFO: "dim",
}

_STATUS_COLOR = {
    FindingStatus.OPEN: "yellow",
    FindingStatus.ACKNOWLEDGED: "blue",
    FindingStatus.RESOLVED: "green",
    FindingStatus.SUPPRESSED: "dim",
}


def _database_url() -> str:
    return os.environ.get("HOMELAB_HELPER_DATABASE_URL") or "sqlite+aiosqlite:///./homelab.db"


async def _resolve_fingerprint(session: AsyncSession, prefix: str) -> ReconciliationFinding:
    """Look up exactly one finding whose fingerprint starts with ``prefix``."""
    if not prefix:
        raise typer.BadParameter("fingerprint prefix cannot be empty")
    stmt = select(ReconciliationFinding).where(ReconciliationFinding.fingerprint.like(f"{prefix}%"))
    matches = (await session.execute(stmt)).scalars().all()
    if not matches:
        console.print(f"[red]no finding matches fingerprint prefix {prefix!r}[/red]")
        raise typer.Exit(code=2)
    if len(matches) > 1:
        console.print(
            f"[red]fingerprint prefix {prefix!r} is ambiguous ({len(matches)} matches)[/red]"
        )
        for f in matches[:5]:
            console.print(f"  [dim]{f.fingerprint}[/dim]  {f.title}")
        raise typer.Exit(code=2)
    return matches[0]


def _status_chip(s: FindingStatus) -> str:
    color = _STATUS_COLOR.get(s, "white")
    return f"[{color}]{s.value}[/{color}]"


def _severity_chip(s: FindingSeverity) -> str:
    color = _SEVERITY_COLOR.get(s, "white")
    return f"[{color}]{s.value}[/{color}]"


def _affected_host_ids(f: ReconciliationFinding) -> list[str]:
    """Pull host target_ids out of the affected list — for column display."""
    return [
        a["target_id"]
        for a in (f.affected or [])
        if a.get("target_type") == "host" and a.get("target_id")
    ]


def _parse_status_filter(status: str) -> list[FindingStatus]:
    if status == "all":
        return list(FindingStatus)
    if status == "active":
        return [FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED]
    try:
        return [FindingStatus(status)]
    except ValueError as exc:
        raise typer.BadParameter(f"unknown status {status!r}") from exc


def _parse_enum[E: Enum](value: str | None, enum_cls: type[E], label: str) -> E | None:
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise typer.BadParameter(f"unknown {label} {value!r}") from exc


def _render_findings_table(rows: list[ReconciliationFinding]) -> None:
    if not rows:
        console.print("[dim]no findings match the filter[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("fingerprint", style="dim", no_wrap=True)
    table.add_column("sev")
    table.add_column("kind", no_wrap=True)
    table.add_column("status")
    table.add_column("title", overflow="fold")
    table.add_column("last seen", no_wrap=True)
    for r in rows:
        table.add_row(
            r.fingerprint,
            _severity_chip(r.severity),
            r.kind.value,
            _status_chip(r.status),
            r.title,
            r.last_seen.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)
    console.print(f"[dim]{len(rows)} finding(s)[/dim]")


@findings_app.command(name="list")
def findings_list(
    status: str = typer.Option(
        "open",
        "--status",
        help=(
            "Filter by status: open, acknowledged, resolved, suppressed, active (open+ack), or all."
        ),
    ),
    severity: str | None = typer.Option(None, "--severity"),
    kind: str | None = typer.Option(None, "--kind"),
    host: str | None = typer.Option(
        None, "--host", help="Filter to findings affecting this host id."
    ),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500),
) -> None:
    """Tabular view of findings, filtered as requested."""

    statuses = _parse_status_filter(status)
    sev_filter = _parse_enum(severity, FindingSeverity, "severity")
    kind_filter = _parse_enum(kind, FindingKind, "finding kind")

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                stmt = (
                    select(ReconciliationFinding)
                    .where(ReconciliationFinding.status.in_(statuses))
                    .order_by(ReconciliationFinding.last_seen.desc())
                    .limit(limit)
                )
                if sev_filter is not None:
                    stmt = stmt.where(ReconciliationFinding.severity == sev_filter)
                if kind_filter is not None:
                    stmt = stmt.where(ReconciliationFinding.kind == kind_filter)
                rows = (await session.execute(stmt)).scalars().all()
                if host is not None:
                    # affected JSON filter is portable only in-app.
                    rows = [r for r in rows if host in _affected_host_ids(r)]
        finally:
            await engine.dispose()

        _render_findings_table(list(rows))

    asyncio.run(_go())


@findings_app.command(name="show")
def findings_show(
    fingerprint: str = typer.Argument(
        ..., help="Fingerprint (full or unique prefix) of the finding."
    ),
) -> None:
    """Print every field of one finding."""

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                f = await _resolve_fingerprint(session, fingerprint)
                console.print(
                    f"[bold]{f.title}[/bold]  "
                    f"({_severity_chip(f.severity)} {f.kind.value} "
                    f"{_status_chip(f.status)})"
                )
                console.print(f"[dim]fingerprint:[/dim] {f.fingerprint}")
                console.print(f"[dim]first seen:[/dim] {f.first_seen.isoformat()}")
                console.print(f"[dim]last seen:[/dim]  {f.last_seen.isoformat()}")
                if f.acknowledged_at is not None:
                    console.print(
                        f"[dim]acknowledged:[/dim] {f.acknowledged_at.isoformat()}"
                        + (f" by {f.acknowledged_by}" if f.acknowledged_by else "")
                    )
                if f.resolved_at is not None:
                    console.print(f"[dim]resolved:[/dim] {f.resolved_at.isoformat()}")
                if f.suppressed_until is not None:
                    console.print(f"[dim]suppressed until:[/dim] {f.suppressed_until.isoformat()}")
                console.print()
                console.print(f.description)
                if f.affected:
                    console.print("\n[dim]affected:[/dim]")
                    for a in f.affected:
                        console.print(f"  {a.get('target_type', '?')}: {a.get('target_id', '?')}")
                if f.evidence_refs:
                    console.print("\n[dim]evidence:[/dim]")
                    for e in f.evidence_refs:
                        console.print(f"  {e.get('type', '?')}: {e.get('id', '?')}")
                if f.notes:
                    console.print(f"\n[dim]notes:[/dim] {f.notes}")
        finally:
            await engine.dispose()

    asyncio.run(_go())


@findings_app.command(name="ack")
def findings_ack(
    fingerprint: str = typer.Argument(..., help="Fingerprint (full or unique prefix)."),
    by: str | None = typer.Option(None, "--by", help="Acknowledger identity."),
    notes: str | None = typer.Option(None, "--notes"),
) -> None:
    """Mark a finding as acknowledged (status → ACKNOWLEDGED)."""

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                f = await _resolve_fingerprint(session, fingerprint)
                if f.status == FindingStatus.RESOLVED:
                    console.print(
                        "[yellow]warning:[/yellow] finding is already RESOLVED — "
                        "acknowledging will move it back to ACKNOWLEDGED."
                    )
                f.status = FindingStatus.ACKNOWLEDGED
                f.acknowledged_at = datetime.now(UTC)
                f.acknowledged_by = by
                if notes:
                    f.notes = notes
                console.print(
                    f"[green]ack[/green] {f.fingerprint}  {f.title}"
                    + (f"  [dim]by {by}[/dim]" if by else "")
                )
        finally:
            await engine.dispose()

    asyncio.run(_go())


@findings_app.command(name="resolve")
def findings_resolve(
    fingerprint: str = typer.Argument(..., help="Fingerprint (full or unique prefix)."),
    notes: str | None = typer.Option(None, "--notes"),
) -> None:
    """Manually mark a finding as resolved.

    On the next reconciler run, if the underlying condition is still observed,
    the auto-resolve gate will leave this row RESOLVED but emit a new finding
    with the same fingerprint (reopen-on-recurrence). Manual resolve is for
    cases the operator handled out-of-band.
    """

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                f = await _resolve_fingerprint(session, fingerprint)
                f.status = FindingStatus.RESOLVED
                f.resolved_at = datetime.now(UTC)
                if notes:
                    f.notes = notes
                console.print(f"[green]resolved[/green] {f.fingerprint}  {f.title}")
        finally:
            await engine.dispose()

    asyncio.run(_go())


@findings_app.command(name="suppress")
def findings_suppress(
    fingerprint: str = typer.Argument(..., help="Fingerprint (full or unique prefix)."),
    until: str | None = typer.Option(
        None,
        "--until",
        help="ISO date (YYYY-MM-DD) when suppression auto-expires; default: forever.",
    ),
    notes: str | None = typer.Option(None, "--notes"),
) -> None:
    """Suppress this finding from default listings.

    Honors the existing schema field ``suppressed_until``. Without ``--until``,
    suppression is open-ended; with one, the finding will surface again after
    that date. Suppression survives reopen — re-emitted fingerprints stay
    suppressed if their date is in the future.
    """

    suppressed_until: datetime | None = None
    if until is not None:
        try:
            suppressed_until = datetime.fromisoformat(until).replace(tzinfo=UTC)
        except ValueError as exc:
            raise typer.BadParameter(f"--until must be ISO date (got {until!r})") from exc

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                f = await _resolve_fingerprint(session, fingerprint)
                f.status = FindingStatus.SUPPRESSED
                f.suppressed_until = suppressed_until
                if notes:
                    f.notes = notes
                until_str = (
                    f"until {suppressed_until.date().isoformat()}"
                    if suppressed_until
                    else "indefinitely"
                )
                console.print(f"[dim]suppressed[/dim] {f.fingerprint}  {f.title}  ({until_str})")
        finally:
            await engine.dispose()

    asyncio.run(_go())


def _load_router() -> LLMRouter:
    """Factory (monkeypatched in tests)."""
    return router_from_env()


@findings_app.command(name="narrate")
def findings_narrate(
    fingerprints: list[str] = typer.Argument(
        None, help="Fingerprint prefixes to narrate; omit for all open findings."
    ),
    status: str = typer.Option(
        "active", "--status", help="Which findings to narrate when none are named."
    ),
) -> None:
    """Narrate findings as prose via the LLM router (Narrator agent)."""

    async def _go() -> int:
        engine = make_engine(_database_url())
        router = _load_router()
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                if fingerprints:
                    rows = [await _resolve_fingerprint(session, p) for p in fingerprints]
                else:
                    stmt = select(ReconciliationFinding).where(
                        ReconciliationFinding.status.in_(_parse_status_filter(status))
                    )
                    rows = list((await session.execute(stmt)).scalars().all())
                if not rows:
                    console.print("no findings to narrate")
                    return 0
                try:
                    result = await narrate_findings(router, rows)
                except RouterRefusal as refusal:
                    console.print(f"[red]refused:[/red] {refusal}")
                    return 2
                console.print(result.text)
                origin = "local" if result.local else "cloud"
                footer = (
                    f"[{result.backend}: {result.model} "
                    f"({result.tier.name.lower()}, {origin}) — "
                    f"{len(rows)} finding(s)]"
                )
                console.print(f"[dim]{escape(footer)}[/dim]")
                return 0
        finally:
            await router.aclose()
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))
