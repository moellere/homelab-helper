"""``helper diagram`` — the inventory as a picture (Phase 12.1).

Mermaid by default (paste it into a README or a docs page), a standalone HTML
page with ``--format html``, or the graph itself with ``--format json``. A
read over the harness DB; nothing is contacted.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console

from homelab_helper.config import database_url
from homelab_helper.db.session import make_engine, make_sessionmaker
from homelab_helper.engine.diagram import build_model, render_html, render_mermaid, to_dict

diagram_app = typer.Typer(
    name="diagram", help="Draw what runs where: sites, hosts, clusters, guests, services."
)
console = Console()

_FORMATS = ("mermaid", "html", "json")


@diagram_app.callback(invoke_without_command=True)
def diagram(
    fmt: str = typer.Option("mermaid", "--format", "-f", help="mermaid | html | json"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Write here instead of stdout."),
    site: str | None = typer.Option(None, "--site", help="Only this site (topology name or /24)."),
    services: bool = typer.Option(True, "--services/--no-services", help="Draw service endpoints."),
    guests: bool = typer.Option(True, "--guests/--no-guests", help="List guests inside each node."),
    title: str = typer.Option("Homelab", "--title", help="Page title for --format html."),
) -> None:
    """Render the inventory as a diagram."""
    if fmt not in _FORMATS:
        console.print(f"[red]--format must be one of {', '.join(_FORMATS)}[/red]")
        raise typer.Exit(code=2)

    async def _go() -> str:
        engine = make_engine(database_url())
        try:
            async with make_sessionmaker(engine)() as session:
                model = await build_model(
                    session, site=site, include_services=services, include_guests=guests
                )
        finally:
            await engine.dispose()
        if fmt == "html":
            return render_html(model, title=title)
        if fmt == "json":
            return json.dumps(to_dict(model), indent=2) + "\n"
        return render_mermaid(model)

    text = asyncio.run(_go())
    if out is not None:
        out.write_text(text)
        console.print(f"wrote {out}")
    else:
        typer.echo(text, nl=False)


__all__ = ["diagram_app"]
