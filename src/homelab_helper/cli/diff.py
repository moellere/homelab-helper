"""`helper diff ...` — cross-source divergence surfaces (Phase 3).

``diff git-vs-cluster`` reads Argo CD's view of git desired-state vs the live
cluster and reports the drift. Read-only by default (just the table); with
``--persist`` it records each drifted Application as a ``DRIFT_CANDIDATE``
finding via :func:`reconcile_argocd_drift`, so drift shows up in ``helper
findings`` / ``helper audit`` with the usual reopen-on-recurrence lifecycle.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.table import Table

from homelab_helper.adapters.argocd import ArgoCDAdapter, application_is_drifted
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.argocd_drift import reconcile_argocd_drift

diff_app = typer.Typer(
    name="diff",
    help="Cross-source divergence (git desired-state vs live cluster).",
    no_args_is_help=True,
)

console = Console()


def _database_url() -> str:
    return os.environ.get("HOMELAB_HELPER_DATABASE_URL") or "sqlite+aiosqlite:///./homelab.db"


def _load_argocd_adapter() -> ArgoCDAdapter:
    """Factory (monkeypatched in tests) — builds an Argo CD adapter from env."""
    return ArgoCDAdapter.from_env()


@diff_app.command(name="git-vs-cluster")
def diff_git_vs_cluster(
    persist: bool = typer.Option(
        False, "--persist", help="Record drifted apps as DRIFT_CANDIDATE findings."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --persist, preview + roll back."),
) -> None:
    """Report Argo CD git-vs-cluster drift; optionally record it as findings."""

    async def _go() -> int:
        adapter = _load_argocd_adapter()
        try:
            ok, err = await adapter.health_check()
            if not ok:
                console.print(f"[red]argocd unreachable:[/red] {err}")
                return 1
            apps = await adapter.list_applications()
        finally:
            await adapter.aclose()

        drifted = [a for a in apps if application_is_drifted(a)]
        console.print(
            f"[cyan]git-vs-cluster[/cyan]: {len(apps)} application(s), "
            f"[yellow]{len(drifted)}[/yellow] drifted"
        )
        table = Table(title="drift")
        for col in ("name", "sync", "health", "out-of-sync"):
            table.add_column(col, overflow="fold")
        for a in sorted(drifted, key=lambda x: str(x.get("name"))):
            oos = a.get("out_of_sync_resources") or []
            names = ", ".join(f"{r.get('kind')}/{r.get('name')}" for r in oos) or "—"
            table.add_row(
                str(a.get("name")),
                str(a.get("sync_status") or "—"),
                str(a.get("health_status") or "—"),
                names,
            )
        if drifted:
            console.print(table)
        else:
            console.print("[green]no drift — every app is Synced + Healthy[/green]")

        if not persist:
            return 0

        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                result = await reconcile_argocd_drift(session, apps, when=datetime.now(UTC))
                if dry_run:
                    await session.rollback()
            console.print(
                f"[green]findings[/green] (drift-candidate): "
                f"{len(result.opened)} opened, {len(result.reopened)} reopened, "
                f"{len(result.updated)} re-seen, {len(result.resolved)} resolved"
                + (" [dim](dry-run, rolled back)[/dim]" if dry_run else "")
            )
        finally:
            await engine.dispose()
        return 0

    raise typer.Exit(code=asyncio.run(_go()))


__all__ = ["diff_app"]
