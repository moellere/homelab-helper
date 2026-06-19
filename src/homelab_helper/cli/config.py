"""`helper config` — show the effective harness configuration and its sources.

Read-only inspection: where the DB lives, whether NetBox is wired, and which
environment variables are in play. Secrets (tokens) are reported as set/unset,
never printed. This is the "what is the harness actually going to do" view —
handy before a discover/sync run.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

_DEFAULT_DB_URL = "sqlite+aiosqlite:///./homelab.db"

config_app = typer.Typer(
    name="config",
    help="Show effective configuration and its sources.",
    no_args_is_help=False,
    invoke_without_command=True,
)

console = Console()


def _from_env(var: str, default: str | None = None) -> tuple[str, str]:
    """Return ``(value, source)`` where source is 'env', 'default', or 'unset'."""
    raw = os.environ.get(var)
    if raw is not None and raw != "":
        return raw, "env"
    if default is not None:
        return default, "default"
    return "(unset)", "unset"


def _db_note(url: str) -> str:
    """For a local sqlite URL, note whether the file exists yet."""
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return ""
    path = Path(url[len(prefix) :])
    return "file exists" if path.exists() else "not initialized (run `helper db init`)"


@config_app.callback(invoke_without_command=True)
def config() -> None:
    """Print the effective configuration."""
    db_url, db_src = _from_env("HOMELAB_HELPER_DATABASE_URL", _DEFAULT_DB_URL)
    netbox_url, netbox_src = _from_env("HOMELAB_HELPER_NETBOX_URL")
    _token, token_src = _from_env("HOMELAB_HELPER_NETBOX_TOKEN")
    verify_ssl, verify_src = _from_env("HOMELAB_HELPER_NETBOX_VERIFY_SSL", "true")
    ssh_key, ssh_src = _from_env("HOMELAB_HELPER_SSH_KEY")

    table = Table(title="homelab-helper configuration")
    table.add_column("setting")
    table.add_column("value")
    table.add_column("source", justify="right")

    table.add_row("database url", db_url, db_src)
    note = _db_note(db_url)
    if note:
        table.add_row("database state", note, "")
    table.add_row("netbox url", netbox_url, netbox_src)
    # Token never printed — only its presence.
    table.add_row("netbox token", "set" if token_src == "env" else "(unset)", token_src)
    table.add_row("netbox verify ssl", verify_ssl, verify_src)
    table.add_row("ssh key", ssh_key, ssh_src)

    console.print(table)
    if netbox_src == "unset":
        console.print("[dim]NetBox sync is disabled until HOMELAB_HELPER_NETBOX_URL is set.[/dim]")


__all__ = ["config_app"]
