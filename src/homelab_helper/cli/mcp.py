"""`helper mcp ...` — run the harness's MCP server (Phase 4).

``serve`` blocks on stdio and is what an MCP client launches; ``tools`` prints
the tool roster for a quick smoke check without a client.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

mcp_app = typer.Typer(
    name="mcp",
    help="Model Context Protocol server for Claude Desktop / Claude Code / Cursor.",
    no_args_is_help=True,
)

console = Console()


@mcp_app.command(name="serve")
def mcp_serve() -> None:
    """Serve the harness's MCP tools over stdio (blocking; launched by a client)."""
    # Imported here: the server builds itself and loads .env at import, which
    # no other command should pay for.
    from homelab_helper.mcp_server import main  # noqa: PLC0415

    main()


@mcp_app.command(name="tools")
def mcp_tools() -> None:
    """List the MCP tools this server exposes (smoke check, no client needed)."""
    from homelab_helper.mcp_server import server  # noqa: PLC0415

    tools = asyncio.run(server.list_tools())
    table = Table(title=f"{len(tools)} MCP tool(s)")
    table.add_column("tool", no_wrap=True)
    table.add_column("description", overflow="fold")
    for t in sorted(tools, key=lambda t: t.name):
        table.add_row(t.name, (t.description or "").split("\n")[0])
    console.print(table)


__all__ = ["mcp_app"]
