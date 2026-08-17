"""`helper config` — show the effective harness configuration and its sources.

Read-only inspection: where the DB lives, which ``.env`` is in play, and which
discovery sources have credentials. Secrets are reported as set/unset, never
printed. This is the "what is the harness actually going to do" view — handy
before a discover/sync run, and the first thing to check when a source errors
out with a credentials message.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from homelab_helper.config import SOURCES, config_status

config_app = typer.Typer(
    name="config",
    help="Show effective configuration and its sources.",
    no_args_is_help=False,
    invoke_without_command=True,
)

console = Console()


def _render_general(status: dict) -> Table:
    table = Table(title="homelab-helper configuration")
    table.add_column("setting")
    table.add_column("value", overflow="fold")

    table.add_row("database url", status["database_url"])
    if status["database_exists"] is not None:
        table.add_row(
            "database state",
            "file exists"
            if status["database_exists"]
            else "not initialized (run `helper db init`)",
        )
    table.add_row("env files", ", ".join(status["env_files"]) or "(none found)")
    table.add_row("ssh key", status["ssh_key"] or "(unset)")
    return table


_VAR_PREFIX = "HOMELAB_HELPER_"


def _short(var: str) -> str:
    return var.removeprefix(_VAR_PREFIX).lower()


def _detail(row: dict) -> str:
    """Configured -> what it points at; unconfigured -> what's missing.

    Secrets render as ``set`` — their values are never available here.
    """
    if not row["configured"]:
        return "missing: " + ", ".join(_short(v) for v in row["missing"])
    if row.get("controllers"):
        return "controllers: " + ", ".join(row["controllers"])
    bits = [
        f"{_short(v['variable'])}=" + ("set" if v["secret"] else str(v["value"]))
        for v in row["variables"]
        if v["set"]
    ]
    if row["note"]:
        bits.append(f"[dim]{row['note']}[/dim]")
    return ", ".join(bits)


def _render_sources(status: dict) -> Table:
    table = Table(title="discovery sources")
    table.add_column("source", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("detail", overflow="fold")

    by_name = {s["source"]: s for s in status["sources"]}
    for source in SOURCES:
        row = by_name[source.name]
        state = "[green]ready[/green]" if row["configured"] else "[yellow]not configured[/yellow]"
        table.add_row(source.name, state, _detail(row))
    return table


def _render_llm(llm: dict) -> Table:
    table = Table(title=f"llm backends (privacy: {llm.get('privacy', '?')})")
    table.add_column("backend", no_wrap=True)
    table.add_column("model", overflow="fold")
    table.add_column("tier", no_wrap=True)
    table.add_column("origin", no_wrap=True)
    for b in llm.get("backends", []):
        table.add_row(b["backend"], b["model"], b["tier"], "local" if b["local"] else "cloud")
    return table


@config_app.callback(invoke_without_command=True)
def config() -> None:
    """Print the effective configuration."""
    status = config_status()
    console.print(_render_general(status))
    console.print(_render_sources(status))
    llm = status["llm"]
    if "error" in llm:
        console.print(f"[red]llm config error:[/red] {llm['error']}")
    else:
        console.print(_render_llm(llm))

    ready = status["configured_sources"]
    blocked = [s for s in status["unconfigured_sources"] if s != "netbox"]
    console.print(f"[dim]{len(ready)} source(s) ready: {', '.join(ready) or 'none'}[/dim]")
    if blocked:
        console.print(
            "[dim]Set the missing variables in a .env file at the repo root "
            "(gitignored) or export them before running.[/dim]"
        )


__all__ = ["config_app"]
