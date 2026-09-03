"""`helper assert ...` subcommands — run and inspect ConfigurationAssertions.

Mirrors the structure of ``helper findings``: a list, a show, and a run verb.
``run`` is the only mutating verb (it writes AssertionRun rows and may
open/resolve a finding). L1 stance untouched — assertions never touch
managed hosts at this layer; they only consult the harness DB. Network-
crossing verifier kinds (SSH_COMMAND etc.) skip with a clear reason until
their adapter slices land.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import desc, select

from homelab_helper.config import database_url as _database_url
from homelab_helper.db.enums import AssertionStatus
from homelab_helper.db.models import AssertionRun, ConfigurationAssertion
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.assertion_library import LibraryError, load_library
from homelab_helper.engine.assertions import AssertionEngine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

assert_app = typer.Typer(
    name="assert",
    help="Run and inspect configuration assertions.",
    no_args_is_help=True,
)

console = Console()


_STATUS_COLOR = {
    AssertionStatus.PASS: "green",
    AssertionStatus.FAIL: "red",
    AssertionStatus.ERROR: "magenta",
    AssertionStatus.SKIP: "dim",
}


def _status_chip(s: AssertionStatus) -> str:
    color = _STATUS_COLOR.get(s, "white")
    return f"[{color}]{s.value}[/{color}]"


async def _load_assertion(session: AsyncSession, name: str) -> ConfigurationAssertion:
    a = (
        await session.execute(
            select(ConfigurationAssertion).where(ConfigurationAssertion.name == name)
        )
    ).scalar_one_or_none()
    if a is None:
        console.print(f"[red]no assertion named {name!r}[/red]")
        raise typer.Exit(code=2)
    return a


async def _latest_run(session: AsyncSession, assertion_id: object) -> AssertionRun | None:
    return (
        await session.execute(
            select(AssertionRun)
            .where(AssertionRun.assertion_id == assertion_id)
            .order_by(desc(AssertionRun.ran_at))
            .limit(1)
        )
    ).scalar_one_or_none()


@assert_app.command(name="list")
def assert_list(
    enabled_only: bool = typer.Option(
        True,
        "--enabled/--all",
        help="Show only enabled assertions (default), or every row.",
    ),
) -> None:
    """List assertions with their latest run status."""

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                stmt = select(ConfigurationAssertion).order_by(ConfigurationAssertion.name)
                if enabled_only:
                    stmt = stmt.where(ConfigurationAssertion.enabled.is_(True))
                assertions = (await session.execute(stmt)).scalars().all()
                latest_by_id: dict[object, AssertionRun] = {}
                for a in assertions:
                    run = await _latest_run(session, a.id)
                    if run is not None:
                        latest_by_id[a.id] = run
        finally:
            await engine.dispose()

        if not assertions:
            console.print("[dim]no assertions defined yet[/dim]")
            return

        table = Table(show_header=True, header_style="bold")
        table.add_column("name", no_wrap=True)
        table.add_column("kind", no_wrap=True)
        table.add_column("scope", no_wrap=True)
        table.add_column("severity")
        table.add_column("latest")
        table.add_column("ran at", no_wrap=True)
        for a in assertions:
            run = latest_by_id.get(a.id)
            status_cell = _status_chip(run.status) if run else "[dim]never[/dim]"
            ran_at = run.ran_at.strftime("%Y-%m-%d %H:%M") if run else "—"
            name_cell = a.name if a.enabled else f"[dim]{a.name} (disabled)[/dim]"
            table.add_row(
                name_cell,
                a.kind.value,
                a.scope.value + (f":{a.scope_target}" if a.scope_target else ""),
                a.severity_on_fail.value,
                status_cell,
                ran_at,
            )
        console.print(table)
        console.print(f"[dim]{len(assertions)} assertion(s)[/dim]")

    asyncio.run(_go())


@assert_app.command(name="show")
def assert_show(
    name: str = typer.Argument(..., help="Assertion name."),
) -> None:
    """Show an assertion's definition and most-recent run evidence."""

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                a = await _load_assertion(session, name)
                run = await _latest_run(session, a.id)

                console.print(
                    f"[bold]{a.name}[/bold]  "
                    f"({a.kind.value} / {a.scope.value}"
                    + (f":{a.scope_target}" if a.scope_target else "")
                    + ")"
                )
                console.print(f"[dim]severity on fail:[/dim] {a.severity_on_fail.value}")
                console.print(f"[dim]enabled:[/dim]          {a.enabled}")
                console.print(f"\n{a.description}")
                if a.rationale:
                    console.print(f"\n[dim]rationale:[/dim] {a.rationale}")
                console.print("\n[dim]verifier_spec:[/dim]")
                for k, v in (a.verifier_spec or {}).items():
                    console.print(f"  {k} = {v!r}")
                if run is None:
                    console.print("\n[dim]no runs yet[/dim]")
                    return
                console.print(f"\n[bold]latest run[/bold] ({_status_chip(run.status)})")
                console.print(f"[dim]ran at:[/dim] {run.ran_at.isoformat()}")
                if run.error:
                    console.print(f"[dim]error:[/dim]  {run.error}")
                for k, v in (run.evidence or {}).items():
                    console.print(f"  [dim]{k}[/dim] = {v!r}")
        finally:
            await engine.dispose()

    asyncio.run(_go())


@assert_app.command(name="run")
def assert_run(
    name: str | None = typer.Option(
        None,
        "--name",
        help="Run a single assertion by name. Omit + use --all to run every enabled one.",
    ),
    run_all: bool = typer.Option(False, "--all", help="Run every enabled assertion."),
    include_disabled: bool = typer.Option(
        False,
        "--include-disabled",
        help="With --all, include assertions marked enabled=False.",
    ),
) -> None:
    """Execute assertion(s) and persist AssertionRun rows + finding lifecycle."""

    if not name and not run_all:
        console.print("[red]error:[/red] supply --name NAME or --all")
        raise typer.Exit(code=2)
    if name and run_all:
        console.print("[red]error:[/red] --name and --all are mutually exclusive")
        raise typer.Exit(code=2)

    async def _go() -> int:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                eng = AssertionEngine()
                if name is not None:
                    a = await _load_assertion(session, name)
                    results = [await eng.run_assertion(session, a)]
                else:
                    results = await eng.run_all(session, enabled_only=not include_disabled)

            if not results:
                console.print("[dim]no assertions ran[/dim]")
                return 0

            table = Table(show_header=True, header_style="bold")
            table.add_column("assertion", no_wrap=True)
            table.add_column("status")
            table.add_column("finding")
            table.add_column("evidence", overflow="fold")
            for r in results:
                if r.finding_opened:
                    finding_chip = "[red]opened[/red]"
                elif r.finding_resolved:
                    finding_chip = "[green]resolved[/green]"
                elif r.finding_updated:
                    finding_chip = "[dim]still failing[/dim]"
                else:
                    finding_chip = "—"
                evidence_summary = (
                    r.error
                    if r.status in (AssertionStatus.ERROR, AssertionStatus.SKIP)
                    else _format_evidence(r.evidence)
                )
                table.add_row(
                    r.assertion_name,
                    _status_chip(r.status),
                    finding_chip,
                    evidence_summary or "—",
                )
            console.print(table)

            # Exit non-zero if anything FAILed or ERRORed — useful for CI gating.
            bad = sum(
                1 for r in results if r.status in (AssertionStatus.FAIL, AssertionStatus.ERROR)
            )
            return 1 if bad else 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@assert_app.command(name="load")
def assert_load(
    path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="YAML library file to load."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Parse and report what would change, but write nothing.",
    ),
) -> None:
    """Upsert ConfigurationAssertion rows from a YAML library file.

    Idempotent: re-loading the same file twice produces no changes. Entries
    whose ``host:`` field references a hostname not in the harness DB are
    skipped with a reason (the loader is intentionally tolerant — land the
    library, then discover the hosts, then re-load).
    """

    async def _go() -> int:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                try:
                    result = await load_library(session, path, dry_run=dry_run)
                except LibraryError as exc:
                    console.print(f"[red]library error:[/red] {exc}")
                    return 2

            header = "[dim](dry run)[/dim] " if dry_run else ""
            console.print(
                f"{header}[green]{len(result.created)}[/green] created, "
                f"[yellow]{len(result.updated)}[/yellow] updated, "
                f"[dim]{len(result.unchanged)}[/dim] unchanged, "
                f"[red]{len(result.skipped)}[/red] skipped "
                f"[dim](of {result.total} entries)[/dim]"
            )
            for name in result.created:
                console.print(f"  [green]+[/green] {name}")
            for name in result.updated:
                console.print(f"  [yellow]~[/yellow] {name}")
            for name, reason in result.skipped:
                console.print(f"  [red]-[/red] {name}  [dim]{reason}[/dim]")
            return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


def _format_evidence(evidence: dict[str, object] | None) -> str:
    """One-line summary for the table cell."""
    if not evidence:
        return ""
    key = evidence.get("key")
    observed = evidence.get("observed")
    predicate = evidence.get("predicate")
    expected = evidence.get("expected")
    if key:
        return f"{key} {predicate} {expected!r} (observed: {observed!r})"
    return ", ".join(f"{k}={v!r}" for k, v in evidence.items())
