"""``helper status`` — the dashboard rollup, printed or served (Phase 9.7a).

``show`` prints ``engine.status.snapshot`` as a table (``--json`` for the raw
object); ``serve`` runs the HTTP endpoint for a Homepage widget or a Home
Assistant REST sensor. Both are reads.
"""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table

from homelab_helper.config import database_url
from homelab_helper.db.session import make_engine, make_sessionmaker
from homelab_helper.engine.status import snapshot

status_app = typer.Typer(
    name="status", help="One-screen status: findings, approvals, discovery, trust."
)
console = Console()

_HEALTH_STYLE = {"ok": "green", "attention": "yellow", "critical": "red"}


def _ago(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m ago"


@status_app.command(name="show")
def status_show(
    as_json: bool = typer.Option(False, "--json", help="Print the snapshot as JSON."),
) -> None:
    """Print the status snapshot."""

    async def _go() -> dict:
        engine = make_engine(database_url())
        try:
            async with make_sessionmaker(engine)() as session:
                return await snapshot(session)
        finally:
            await engine.dispose()

    snap = asyncio.run(_go())
    if as_json:
        console.print_json(json.dumps(snap))
        return
    style = _HEALTH_STYLE.get(snap["health"], "white")
    console.print(f"[{style}]{snap['health']}[/{style}] — {snap['headline']}")
    table = Table(show_header=False)
    table.add_column("item", no_wrap=True)
    table.add_column("value", overflow="fold")
    sev = snap["findings"]["by_severity"]
    table.add_row(
        "open findings",
        ", ".join(f"{k} {v}" for k, v in sev.items() if v) or "none",
    )
    table.add_row(
        "pending approvals",
        "; ".join(snap["proposals"]["titles"]) or "none",
    )
    table.add_row("last discovery", _ago(snap["discovery"]["age_seconds"]))
    table.add_row(
        "discovery runs (24h)",
        f"{snap['discovery']['runs_24h']} ({snap['discovery']['failed_24h']} failed)",
    )
    table.add_row("last assertion run", _ago(snap["assertions"]["age_seconds"]))
    table.add_row(
        "trust cells",
        ", ".join(f"{k} {v}" for k, v in snap["trust"]["cells_by_level"].items()) or "none",
    )
    table.add_row("open windows", str(snap["trust"]["open_windows"]))
    table.add_row(
        "receipts (24h)",
        f"{snap['receipts_24h']['succeeded']} succeeded, {snap['receipts_24h']['failed']} failed",
    )
    inv = snap["inventory"]
    table.add_row(
        "inventory",
        f"{inv['hosts']} host(s), {inv['clusters']} cluster(s), "
        f"{inv['virtual_machines']} VM(s), {inv['services']} service(s)",
    )
    console.print(table)


@status_app.command(name="serve")
def status_serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8710, "--port", help="TCP port."),
) -> None:
    """Serve GET /status and GET /healthz over HTTP (blocking)."""
    from homelab_helper.status_api import serve  # noqa: PLC0415 - FastAPI only when serving

    console.print(f"serving status on http://{host}:{port}/status")
    serve(host, port)


__all__ = ["status_app"]
