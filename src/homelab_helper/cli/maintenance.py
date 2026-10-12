"""``helper maintenance ...`` — plan and run one node's drain → update → reboot → return (Phase 10.4).

``plan`` drafts the steps as proposals and shows them (``plan-talos`` does the
same for a Talos cluster: one upgrade per node, control planes first, optionally
Kubernetes last); ``run`` executes them in order after one consent, relaying it to every step's CONFIRM; ``show``,
``list`` and ``abort`` do what they say. The run is a trigger: each step is
gated by ``decide()`` on its own cell, and ``node-reboot`` can never be
autonomous.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from homelab_helper.adapters.kernel_ssh import KernelSSHAdapter
from homelab_helper.adapters.proxmox import ProxmoxConfigError
from homelab_helper.adapters.talos import TalosAdapter, TalosError
from homelab_helper.cli.execute import (
    _build_adapter,
    _build_argocd_adapter,
    _build_k8s_adapter,
)
from homelab_helper.config import database_url
from homelab_helper.db.enums import MaintenanceStatus
from homelab_helper.db.models import MaintenanceRun
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.maintenance import (
    MaintenanceError,
    abort_maintenance,
    execute_maintenance,
    plan_maintenance,
    plan_talos_maintenance,
    talos_health_gate,
)
from homelab_helper.engine.notify import notifier_from_env
from homelab_helper.engine.trust import operator_identity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

maintenance_app = typer.Typer(
    name="maintenance",
    help="One node's drain → update → reboot → return as a sequence of gated steps.",
    no_args_is_help=True,
)
console = Console()
_STATE_STYLE = {
    "planned": "white",
    "succeeded": "green",
    "failed": "red",
    "skipped": "yellow",
}


async def _resolve(session: AsyncSession, prefix: str) -> MaintenanceRun | None:
    rows = (await session.execute(select(MaintenanceRun))).scalars().all()
    hits = [r for r in rows if str(r.id).startswith(prefix)]
    if len(hits) == 1:
        return hits[0]
    console.print(
        f"[red]no run matches {escape(prefix)!s}[/red]"
        if not hits
        else f"[red]{len(hits)} runs match {escape(prefix)}; give more of the id[/red]"
    )
    return None


def _print_run(run: MaintenanceRun) -> None:
    style = {
        MaintenanceStatus.SUCCEEDED: "green",
        MaintenanceStatus.FAILED: "red",
        MaintenanceStatus.ABORTED: "yellow",
    }.get(run.status, "cyan")
    plane = " (talos)" if run.plane == "talos" else ""
    console.print(
        f"[bold]{escape(run.node)}[/bold]{plane} run {run.id} — "
        f"[{style}]{run.status.value}[/{style}]" + (f" — {escape(run.notes)}" if run.notes else "")
    )
    table = Table(show_header=True)
    table.add_column("#", no_wrap=True)
    table.add_column("step", overflow="fold")
    table.add_column("kind", no_wrap=True)
    table.add_column("state", no_wrap=True)
    table.add_column("proposal", no_wrap=True)
    for step in run.steps:
        st = _STATE_STYLE.get(step["state"], "white")
        table.add_row(
            str(step["index"]),
            step["name"] + (f" — {step['detail']}" if step.get("detail") else ""),
            step["kind"],
            f"[{st}]{step['state']}[/{st}]",
            str(step["proposal_id"])[:8],
        )
    console.print(table)


@maintenance_app.command(name="plan")
def maintenance_plan(
    node: str = typer.Argument(..., help="The Proxmox node to maintain (a known host)."),
    target: str | None = typer.Option(
        None, "--target", help="Send every migratable guest to this node instead of the emptiest."
    ),
    resume: str | None = typer.Option(
        None,
        "--resume",
        help="A stopped run (id or prefix) on this node whose drained guests this run brings back.",
    ),
) -> None:
    """Draft the run: one proposal per step, nothing executed."""

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            adapter = _build_adapter()
        except ProxmoxConfigError as exc:
            console.print(f"[red]cannot plan:[/red] {escape(str(exc))}")
            return 2
        try:
            async with session_scope(make_sessionmaker(engine)) as session:
                earlier = None
                if resume is not None:
                    earlier = await _resolve(session, resume)
                    if earlier is None:
                        return 2
                try:
                    run = await plan_maintenance(
                        session,
                        adapter,
                        node,
                        actor=operator_identity(),
                        target_node=target,
                        resume_from=earlier,
                    )
                except MaintenanceError as exc:
                    console.print(f"[red]cannot plan:[/red] {escape(str(exc))}")
                    return 2
                _print_run(run)
                console.print(f"[dim]run it: helper maintenance run {str(run.id)[:8]}[/dim]")
                return 0
        finally:
            await adapter.aclose()
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@maintenance_app.command(name="plan-talos")
def maintenance_plan_talos(
    control_plane: str = typer.Argument(
        ..., help="A control-plane node as `talosctl -n` knows it (address or name)."
    ),
    to: str = typer.Option(..., "--to", help="Talos release tag for every node, e.g. v1.14.2."),
    kubernetes: str | None = typer.Option(
        None, "--kubernetes", help="Also upgrade Kubernetes to this version last, e.g. 1.37.2."
    ),
    talosconfig: str | None = typer.Option(
        None, "--talosconfig", help="Path to a talosconfig (default: talosctl's own)."
    ),
) -> None:
    """Draft a Talos cluster upgrade: one step per node, control planes first."""

    async def _go() -> int:
        engine = make_engine(database_url())
        talos = TalosAdapter(talosconfig=talosconfig)
        try:
            async with session_scope(make_sessionmaker(engine)) as session:
                try:
                    run = await plan_talos_maintenance(
                        session,
                        talos,
                        control_plane,
                        to,
                        actor=operator_identity(),
                        kubernetes=kubernetes,
                    )
                except (MaintenanceError, TalosError) as exc:
                    console.print(f"[red]cannot plan:[/red] {escape(str(exc))}")
                    return 2
                _print_run(run)
                console.print(f"[dim]run it: helper maintenance run {str(run.id)[:8]}[/dim]")
                return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@maintenance_app.command(name="run")
def maintenance_run(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix) from `maintenance plan`."),
    yes: bool = typer.Option(False, "--yes", help="Consent to every step up front."),
) -> None:
    """Execute the run's steps in order; stops at the first failure."""

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            adapter = _build_adapter()
        except ProxmoxConfigError as exc:
            console.print(f"[red]cannot run:[/red] {escape(str(exc))}")
            return 2
        try:
            async with session_scope(make_sessionmaker(engine)) as session:
                run = await _resolve(session, run_id)
                if run is None:
                    return 1
                _print_run(run)
                pending = [s for s in run.steps if s["state"] == "planned"]
                if not pending:
                    console.print("[yellow]nothing left to run[/yellow]")
                    return 1
                if not yes and not typer.confirm(
                    f"run {len(pending)} step(s) on {run.node}, stopping at the first failure?",
                    default=False,
                ):
                    console.print("[yellow]not started[/yellow]")
                    return 1

                async def _confirm(manifest: Any, decision: Any) -> bool:
                    console.print(
                        f"  [dim]confirm relayed — {escape(manifest.cell_key)} "
                        f"{escape(manifest.target_label)}[/dim]"
                    )
                    return True

                talos = TalosAdapter() if run.plane == "talos" else None
                try:
                    run = await execute_maintenance(
                        session,
                        run,
                        adapter,
                        actor=operator_identity(),
                        confirm_cb=_confirm,
                        ssh_adapter=KernelSSHAdapter(),
                        k8s_adapter=_build_k8s_adapter(),
                        argocd_adapter=_build_argocd_adapter(),
                        talos_adapter=talos,
                        health_cb=talos_health_gate(talos, run.node) if talos else None,
                        notifier=notifier_from_env(),
                    )
                except MaintenanceError as exc:
                    console.print(f"[red]{escape(str(exc))}[/red]")
                    return 2
                _print_run(run)
                return 0 if run.status is MaintenanceStatus.SUCCEEDED else 3
        finally:
            await adapter.aclose()
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@maintenance_app.command(name="show")
def maintenance_show(run_id: str = typer.Argument(..., help="Run id (or unique prefix).")) -> None:
    """One run, step by step."""

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            async with make_sessionmaker(engine)() as session:
                run = await _resolve(session, run_id)
                if run is None:
                    return 1
                _print_run(run)
                return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@maintenance_app.command(name="list")
def maintenance_list() -> None:
    """Every run, newest first."""

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            async with make_sessionmaker(engine)() as session:
                rows = (
                    (
                        await session.execute(
                            select(MaintenanceRun).order_by(MaintenanceRun.created_at.desc())
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()
        table = Table(title=f"{len(rows)} maintenance run(s)")
        for col in ("id", "node", "status", "steps", "created", "finished"):
            table.add_column(col, no_wrap=True)
        for r in rows:
            done = sum(1 for s in r.steps if s["state"] == "succeeded")
            table.add_row(
                str(r.id)[:8],
                r.node,
                r.status.value,
                f"{done}/{len(r.steps)}",
                r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
                r.finished_at.strftime("%Y-%m-%d %H:%M") if r.finished_at else "",
            )
        console.print(table)
        return 0

    raise typer.Exit(code=asyncio.run(_go()))


@maintenance_app.command(name="abort")
def maintenance_abort(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix)."),
    reason: str = typer.Option("", "--reason", "-r", help="Why, for the record."),
) -> None:
    """Stop a planned or interrupted run; its remaining proposals are rejected."""

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            async with session_scope(make_sessionmaker(engine)) as session:
                run = await _resolve(session, run_id)
                if run is None:
                    return 1
                try:
                    run = await abort_maintenance(
                        session, run, actor=operator_identity(), reason=reason or "operator"
                    )
                except MaintenanceError as exc:
                    console.print(f"[red]{escape(str(exc))}[/red]")
                    return 2
                _print_run(run)
                return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


def _uuid_or_none(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


__all__ = ["maintenance_app"]
