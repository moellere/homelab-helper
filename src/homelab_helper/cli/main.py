"""``helper`` Typer app — entry point for the CLI.

Currently wired:

- ``helper version``
- ``helper db init|status|reset|migrate``
- ``helper discover host|show``
- ``helper findings list|show|ack|resolve|suppress``
- ``helper host show``
- ``helper audit``
- ``helper assert list|show|run``
- ``helper probes ...``

The remaining Phase 1 verbs (``netbox bootstrap``, ``discover network``,
``config``) land as those subsystems come online.
"""

from __future__ import annotations

import typer
from rich.console import Console

from homelab_helper import __version__
from homelab_helper.cli.assertion import assert_app
from homelab_helper.cli.audit import audit_app
from homelab_helper.cli.config import config_app
from homelab_helper.cli.db import db_app
from homelab_helper.cli.discover import discover_app
from homelab_helper.cli.findings import findings_app
from homelab_helper.cli.host import host_app
from homelab_helper.cli.netbox import netbox_app
from homelab_helper.cli.probes import probes_app
from homelab_helper.cli.view import view_app

app = typer.Typer(name="helper", no_args_is_help=True, add_completion=False)
app.add_typer(db_app)
app.add_typer(discover_app)
app.add_typer(findings_app)
app.add_typer(host_app)
app.add_typer(audit_app, name="audit")
app.add_typer(assert_app)
app.add_typer(netbox_app)
app.add_typer(probes_app)
app.add_typer(config_app, name="config")
app.add_typer(view_app)

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
