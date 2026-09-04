"""`helper service ...` — resolver slices and the alias map.

``resolvers`` lists every ``(scope, resolver)`` endpoint slice with counts —
where a renamed controller's orphaned rows show up. ``retire-resolver``
deletes one slice and the services it leaves empty. ``aliases`` shows the
operator's service alias map (``HOMELAB_HELPER_SERVICE_ALIASES``) as loaded.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from homelab_helper.config import database_url as _database_url
from homelab_helper.db.enums import ResolutionScope
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.retire import list_resolver_slices, retire_resolver_slice
from homelab_helper.engine.service_aliases import ALIASES_ENV_VAR, AliasError, load_service_aliases

service_app = typer.Typer(
    name="service",
    help="Service identity: resolver slices and the alias map.",
    no_args_is_help=True,
)

console = Console()


@service_app.command(name="resolvers")
def service_resolvers() -> None:
    """List every (scope, resolver) endpoint slice with its count."""

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                slices = await list_resolver_slices(session)
        finally:
            await engine.dispose()
        table = Table(title="resolver slices")
        for col in ("scope", "resolver", "endpoints"):
            table.add_column(col, no_wrap=True)
        for s in slices:
            table.add_row(s.scope, s.resolver, str(s.endpoints))
        console.print(table)
        console.print(f"[dim]{len(slices)} slice(s)[/dim]")

    asyncio.run(_go())


@service_app.command(name="retire-resolver")
def service_retire_resolver(
    resolver: str = typer.Argument(..., help="Resolver tag, e.g. 'unifi' or 'unifi:covington'."),
    scope: str | None = typer.Option(None, "--scope", help="internal | external (default: both)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete every endpoint a resolver produced, plus the services left empty."""
    wanted: ResolutionScope | None = None
    if scope is not None:
        try:
            wanted = ResolutionScope(scope)
        except ValueError as exc:
            console.print(f"[red]scope must be internal or external, not {scope!r}[/red]")
            raise typer.Exit(code=2) from exc
    if not yes and not typer.confirm(
        f"Delete every {scope or 'internal+external'} endpoint from resolver {resolver!r}?",
        default=False,
    ):
        console.print("[yellow]aborted[/yellow]")
        raise typer.Exit(code=1)

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                result = await retire_resolver_slice(session, resolver, scope=wanted)
        finally:
            await engine.dispose()
        console.print(
            f"[green]retired[/green] resolver {resolver!r}: {result.endpoints_removed} endpoint(s) removed, "
            f"{len(result.services_removed)} empty service(s) removed"
        )

    asyncio.run(_go())


@service_app.command(name="aliases")
def service_aliases() -> None:
    """Show the service alias map as loaded from HOMELAB_HELPER_SERVICE_ALIASES."""
    try:
        aliases = load_service_aliases()
    except (AliasError, OSError) as exc:
        console.print(f"[red]alias map error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    if aliases is None:
        console.print(
            f"[dim]no alias map — set {ALIASES_ENV_VAR} to a YAML file "
            "(see fixtures/service-aliases.example.yaml). Services are named by the leftmost DNS label.[/dim]"
        )
        return
    table = Table(title=f"service aliases ({aliases.source})")
    for col in ("hostname / pattern", "service"):
        table.add_column(col, overflow="fold")
    for host, name in sorted(aliases.exact.items()):
        table.add_row(host, name)
    for pattern, name in aliases.patterns:
        table.add_row(f"{pattern}  [dim](glob)[/dim]", name)
    console.print(table)


__all__ = ["service_app"]
