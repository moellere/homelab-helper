"""``helper`` Typer app — entry point for the CLI.

Currently wired:

- ``helper version``
- ``helper db init|status|reset|migrate``  (Task #6)

The full verb set (``discover``, ``audit``, ``findings``, ``inventory``,
``host``, ``assert``, ``agent``, ``netbox``, ``probes``, ``config``) lands
as Phase 1 progresses.
"""

from __future__ import annotations

import typer
from rich.console import Console

from homelab_helper import __version__
from homelab_helper.cli.db import db_app
from homelab_helper.cli.discover import discover_app
from homelab_helper.cli.probes import probes_app

app = typer.Typer(name="helper", no_args_is_help=True, add_completion=False)
app.add_typer(db_app)
app.add_typer(discover_app)
app.add_typer(probes_app)

console = Console()


@app.callback()
def _root() -> None:
    """homelab-helper - inventory, audit, and (eventually) recommendations."""


@app.command(name="version")
def version_cmd() -> None:
    """Print the installed homelab-helper version."""
    console.print(f"homelab-helper [bold cyan]{__version__}[/bold cyan]")


if __name__ == "__main__":  # pragma: no cover
    app()
