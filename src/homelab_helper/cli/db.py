"""`helper db ...` subcommands — database lifecycle wrapped around Alembic.

These are thin wrappers over ``alembic.command`` that surface the most common
operations as named verbs. Config defaults to the project-local
``alembic.ini``; override the DB URL via ``HOMELAB_HELPER_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import typer
from alembic import command
from alembic.config import Config
from rich.console import Console
from sqlalchemy.engine.url import make_url

from homelab_helper.cli._probe_sync import sync_probes_sync
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.trust import seed_domains

db_app = typer.Typer(
    name="db",
    help="Database management — init, status, migrations.",
    no_args_is_help=True,
)

console = Console()

_DEFAULT_URL = "sqlite+aiosqlite:///./homelab.db"


def _alembic_config() -> Config:
    """Locate alembic.ini relative to the project root and return a Config."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "alembic.ini"
        if candidate.exists():
            return Config(str(candidate))
    raise FileNotFoundError("alembic.ini not found — are you running from the project root?")


def _database_url() -> str:
    return os.environ.get("HOMELAB_HELPER_DATABASE_URL") or _DEFAULT_URL


def _sqlite_path(url: str) -> Path | None:
    """Return the on-disk path for a SQLite URL, or None if not SQLite or in-memory."""
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return None
    database = parsed.database
    if not database or database == ":memory:":
        return None
    return Path(database)


@db_app.command(name="init")
def db_init(
    skip_probe_sync: bool = typer.Option(
        False,
        "--skip-probe-sync",
        help="Skip probe entry-point sync after migration.",
    ),
) -> None:
    """Upgrade the database to the latest revision and register shipped probes."""
    cfg = _alembic_config()
    console.print("[cyan]running alembic upgrade head...[/cyan]")
    command.upgrade(cfg, "head")
    if not skip_probe_sync:
        console.print("[cyan]syncing probe entry points...[/cyan]")
        inserted, updated = sync_probes_sync(_database_url())
        console.print(f"  inserted: [green]{inserted}[/green], updated: [yellow]{updated}[/yellow]")
    console.print("[cyan]seeding trust domains...[/cyan]")
    seeded = _seed_trust_domains_sync(_database_url())
    console.print(f"  created: [green]{seeded}[/green] (idempotent)")
    console.print("[green]done[/green]")


def _seed_trust_domains_sync(url: str) -> int:
    """Seed the trust-domain taxonomy (idempotent; SECRETS ships absolute)."""

    async def _go() -> int:
        engine = make_engine(url)
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                return await seed_domains(session)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


@db_app.command(name="status")
def db_status() -> None:
    """Show the database's current Alembic revision and table count."""
    cfg = _alembic_config()
    url = _database_url()
    console.print(f"[dim]database URL:[/dim] {url}")

    console.print("[dim]current revision:[/dim]")
    command.current(cfg, verbose=False)

    sqlite_path = _sqlite_path(url)
    if sqlite_path is None:
        return
    if not sqlite_path.exists():
        console.print(f"[yellow]database file does not exist yet:[/yellow] {sqlite_path}")
        return
    try:
        con = sqlite3.connect(sqlite_path)
        tables = [
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' "
                "ORDER BY name"
            )
        ]
    finally:
        con.close()
    pretty = ", ".join(tables) if tables else "(none)"
    console.print(f"[dim]tables ({len(tables)}):[/dim] {pretty}")


@db_app.command(name="reset")
def db_reset(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm destructive reset without prompting."
    ),
) -> None:
    """DESTRUCTIVE: drop all tables, then re-upgrade to head. Dev use only."""
    if not yes:
        confirmed = typer.confirm(
            "This will DROP all tables and re-run every migration. Continue?",
            default=False,
        )
        if not confirmed:
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(code=1)
    cfg = _alembic_config()
    console.print("[cyan]downgrade base...[/cyan]")
    command.downgrade(cfg, "base")
    console.print("[cyan]upgrade head...[/cyan]")
    command.upgrade(cfg, "head")
    console.print("[green]done[/green]")


@db_app.command(name="migrate")
def db_migrate(
    message: str = typer.Argument(..., help="Short description of the schema change."),
    autogenerate: bool = typer.Option(
        True,
        "--autogenerate/--manual",
        help="Use Alembic autogenerate (default) or scaffold a blank migration.",
    ),
) -> None:
    """Generate a new Alembic migration. Equivalent to ``alembic revision``."""
    cfg = _alembic_config()
    console.print(f"[cyan]generating migration: {message!r}[/cyan]")
    command.revision(cfg, message=message, autogenerate=autogenerate)
    console.print("[green]done[/green] — review the new file under alembic/versions/")
