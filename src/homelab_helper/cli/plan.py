"""`helper plan ...` — the recommender surface (Phase 5).

``plan workloads`` browses the profile library; ``plan add-workload <name>``
answers AC2's question — "if I add X, where should it run?" — with the
deterministic ranked table always printed, and ``--narrate`` layering the
Planner agent's prose on top (planning tier; a strict-local small-model setup
gets the router's refusal message, never a blocked table).
"""

from __future__ import annotations

import asyncio
from difflib import get_close_matches

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from homelab_helper.config import database_url
from homelab_helper.db.session import make_engine, make_sessionmaker
from homelab_helper.engine.network_path import (
    TOPOLOGY_ENV_VAR,
    TopologyError,
    load_topology,
)
from homelab_helper.engine.placement import (
    PlacementReport,
    network_verdict,
    recommend_placement,
)
from homelab_helper.engine.workloads import (
    WorkloadLibraryError,
    WorkloadProfile,
    load_workload_library,
)
from homelab_helper.llm import LLMRouter, RouterRefusal, router_from_env
from homelab_helper.llm.planner import narrate_placement

plan_app = typer.Typer(
    name="plan",
    help="Placement recommendations from the workload library.",
    no_args_is_help=True,
)

console = Console()


def _load_router() -> LLMRouter:
    """Factory (monkeypatched in tests)."""
    return router_from_env()


def _library() -> dict[str, WorkloadProfile]:
    try:
        return load_workload_library()
    except (WorkloadLibraryError, OSError) as exc:
        console.print(f"[red]workload library error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


@plan_app.command(name="workloads")
def plan_workloads(
    category: str | None = typer.Option(None, "--category", help="Filter by category."),
) -> None:
    """List the workload profile library."""
    library = _library()
    rows = [
        p
        for p in sorted(library.values(), key=lambda p: (p.category, p.name))
        if category is None or p.category == category
    ]
    table = Table(title=f"workload library ({len(rows)} of {len(library)})")
    for col in ("name", "category", "cores", "ram", "gpu", "gravity"):
        table.add_column(col, no_wrap=True)
    for p in rows:
        table.add_row(
            p.name,
            p.category,
            str(p.cpu_cores),
            f"{p.ram_mb} MB",
            p.gpu if p.gpu != "none" else "—",
            p.data_gravity or "—",
        )
    console.print(table)


def _print_report(profile: WorkloadProfile, report: PlacementReport) -> None:
    console.print(
        f"[bold]{profile.name}[/bold] — {profile.description}  "
        f"[dim](baseline {profile.cpu_cores} cores / {profile.ram_mb} MB, "
        f"arch {'/'.join(profile.arch)}, gpu {profile.gpu})[/dim]"
    )
    if profile.depends_on:
        console.print(f"[dim]depends on: {', '.join(profile.depends_on)}[/dim]")
    if not report.candidates:
        console.print("[red]no eligible host[/red]")
    else:
        table = Table(title="placement candidates")
        for col in ("#", "host", "score", "why"):
            table.add_column(col, overflow="fold")
        for i, c in enumerate(report.candidates, 1):
            why = "; ".join(c.reasons)
            if c.caveats:
                why += "  [yellow]⚠ " + "; ".join(c.caveats) + "[/yellow]"
            table.add_row(str(i), c.hostname, f"{c.score:.1f}", why)
        console.print(table)
    for host, reason in report.rejected:
        console.print(f"[dim]rejected {host}: {reason}[/dim]")


@plan_app.command(name="add-workload")
def plan_add_workload(
    name: str = typer.Argument(..., help="Workload name from the library."),
    narrate: bool = typer.Option(False, "--narrate", help="Narrate via the Planner agent."),
) -> None:
    """Recommend where a new workload should run (AC2)."""
    library = _library()
    profile = library.get(name)
    if profile is None:
        matches = get_close_matches(name.lower(), list(library), n=5, cutoff=0.6)
        hint = f" — did you mean: {', '.join(matches)}?" if matches else ""
        console.print(f"[red]no workload named {name!r} in the library[/red]{hint}")
        raise typer.Exit(code=2)

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                report = await recommend_placement(session, profile)
        finally:
            await engine.dispose()

        _print_report(profile, report)
        if not narrate:
            return 0

        router = _load_router()
        try:
            result = await narrate_placement(router, profile, report)
        except RouterRefusal as refusal:
            console.print(f"[yellow]narration unavailable:[/yellow] {refusal}")
            return 0  # the deterministic table above is the deliverable
        finally:
            await router.aclose()
        console.print()
        console.print(result.text)
        origin = "local" if result.local else "cloud"
        footer = f"[{result.backend}: {result.model} ({result.tier.name.lower()}, {origin})]"
        console.print(f"[dim]{escape(footer)}[/dim]")
        return 0

    raise typer.Exit(code=asyncio.run(_go()))


@plan_app.command(name="path")
def plan_path(
    host_a: str = typer.Argument(..., help="First hostname."),
    host_b: str = typer.Argument(..., help="Second hostname."),
    workload: str | None = typer.Option(
        None, "--workload", help="Judge the path for this workload's network class."
    ),
) -> None:
    """Show the network path between two hosts and what it inherits (AC6)."""
    try:
        topology = load_topology()
    except (TopologyError, OSError) as exc:
        console.print(f"[red]topology error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    if topology is None:
        console.print(
            "[dim]no topology declared — all hosts assumed on one LAN. "
            f"Set {TOPOLOGY_ENV_VAR} to a topology file "
            "(see fixtures/network-topology.example.yaml).[/dim]"
        )
        raise typer.Exit(code=0)

    path = topology.path(host_a, host_b)
    if path is None:
        console.print(f"[red]no route between {host_a} and {host_b} in the topology[/red]")
        raise typer.Exit(code=1)

    console.print(f"[bold]{host_a} ↔ {host_b}[/bold]: {path.describe()}")
    for link in path.links:
        console.print(
            f"  [dim]{link.a} ↔ {link.b}: {link.kind}, {link.bandwidth_mbps:.0f} Mbps, "
            f"{link.latency_ms:.1f} ms, {link.reliability}[/dim]"
        )
    console.print(f"LAN-grade: {'yes' if path.lan_grade else 'no'}")

    if workload is not None:
        profile = _library().get(workload)
        if profile is None:
            console.print(f"[red]no workload named {workload!r} in the library[/red]")
            raise typer.Exit(code=2)
        verdict, message = network_verdict(profile, path)
        if verdict == "refuse":
            console.print(f"[red]refused:[/red] {message}")
            raise typer.Exit(code=1)
        if verdict == "warn":
            console.print(f"[yellow]degraded:[/yellow] {message}")
        else:
            console.print(f"[green]ok[/green]: path suits {profile.name}")


__all__ = ["plan_app"]
