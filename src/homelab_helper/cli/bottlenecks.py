"""`helper bottlenecks` — known-pattern analysis with generated mitigations (P5-AC4).

Read-only analysis by default; ``--persist`` records hits as findings (with
the standard reopen/resolve lifecycle) so they surface in ``helper findings``,
``helper audit``, and chat. ``--narrate`` layers Planner prose over the
deterministic output.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.markup import escape

from homelab_helper.config import database_url
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.bottlenecks import (
    BottleneckHit,
    analyze_bottlenecks,
    persist_bottlenecks,
)
from homelab_helper.llm import LLMRouter, RouterRefusal, router_from_env
from homelab_helper.llm.planner import narrate_bottlenecks

bottlenecks_app = typer.Typer(
    name="bottlenecks",
    help="Detect known bottleneck patterns and generate candidate mitigations.",
    invoke_without_command=True,
)

console = Console()

_SEVERITY_STYLE = {"critical": "bold red", "high": "red", "medium": "yellow", "low": "cyan"}


def _load_router() -> LLMRouter:
    """Factory (monkeypatched in tests)."""
    return router_from_env()


def _print_hits(hits: list[BottleneckHit]) -> None:
    if not hits:
        console.print("[green]no known bottleneck patterns detected[/green]")
        return
    for hit in hits:
        style = _SEVERITY_STYLE.get(hit.severity.value, "white")
        console.print(f"[{style}]{hit.severity.value}[/{style}] [bold]{hit.title}[/bold]")
        console.print(f"  {hit.description}")
        console.print(f"  [dim]pattern: {hit.pattern}  fingerprint: {hit.fingerprint}[/dim]")
        for i, m in enumerate(hit.mitigations, 1):
            console.print(f"  {i}. {m}")
        console.print()


@bottlenecks_app.callback(invoke_without_command=True)
def bottlenecks(
    persist: bool = typer.Option(
        False, "--persist", help="Record hits as findings (reopen/resolve lifecycle)."
    ),
    narrate: bool = typer.Option(False, "--narrate", help="Narrate via the Planner agent."),
) -> None:
    """Analyze the fleet for known bottleneck patterns."""

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                hits = await analyze_bottlenecks(session)
            _print_hits(hits)
            if persist:
                async with session_scope(sm) as session:
                    result = await persist_bottlenecks(session, hits, when=datetime.now(UTC))
                console.print(
                    f"[green]findings[/green]: {len(result.opened)} opened, "
                    f"{len(result.reopened)} reopened, {len(result.updated)} re-seen, "
                    f"{len(result.resolved)} resolved"
                )
        finally:
            await engine.dispose()

        if not narrate or not hits:
            return 0
        router = _load_router()
        try:
            result_n = await narrate_bottlenecks(router, hits)
        except RouterRefusal as refusal:
            console.print(f"[yellow]narration unavailable:[/yellow] {refusal}")
            return 0
        finally:
            await router.aclose()
        console.print(result_n.text)
        origin = "local" if result_n.local else "cloud"
        footer = f"[{result_n.backend}: {result_n.model} ({result_n.tier.name.lower()}, {origin})]"
        console.print(f"[dim]{escape(footer)}[/dim]")
        return 0

    raise typer.Exit(code=asyncio.run(_go()))


__all__ = ["bottlenecks_app"]
