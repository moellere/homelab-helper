"""``helper schedule`` — what runs when (Phase 9.6).

Reads the cadence file and the recorded runs and prints, per target and probe
and per assertion, the interval, the last run and when it is next due. Read-only;
``helper daemon run`` does the running.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from homelab_helper.config import database_url
from homelab_helper.db.session import make_engine, make_sessionmaker
from homelab_helper.engine.schedule import (
    ScheduleError,
    load_schedule,
    plan_assertions,
    plan_probes,
)

schedule_app = typer.Typer(
    name="schedule", help="Probe and assertion cadences: what is due, and when."
)
console = Console()


def _ago(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return "never"
    delta = now - ts
    return f"{int(delta.total_seconds() // 3600)}h {int(delta.total_seconds() % 3600 // 60)}m ago"


def _in(delta: timedelta) -> str:
    if delta <= timedelta(0):
        return "[yellow]due[/yellow]"
    return f"{int(delta.total_seconds() // 3600)}h {int(delta.total_seconds() % 3600 // 60)}m"


def _interval(delta: timedelta) -> str:
    s = int(delta.total_seconds())
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if s % size == 0 and s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


@schedule_app.callback(invoke_without_command=True)
def schedule_show(
    path: str | None = typer.Option(
        None, "--file", help="Schedule file (default: $HOMELAB_HELPER_SCHEDULE)."
    ),
    due_only: bool = typer.Option(False, "--due", help="Only what is due now."),
) -> None:
    try:
        schedule = load_schedule(path)
    except ScheduleError as exc:
        console.print(f"[red]schedule:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from None
    now = datetime.now(UTC)

    async def _go() -> int:
        engine = make_engine(database_url())
        try:
            async with make_sessionmaker(engine)() as session:
                probes = await plan_probes(session, schedule, now=now)
                assertions = await plan_assertions(session, schedule, now=now)
        finally:
            await engine.dispose()
        table = Table(title="probe cadences")
        for col in ("target", "probe", "every", "last run", "next"):
            table.add_column(col, no_wrap=True)
        shown = 0
        for d in probes:
            due_in = d.due_in(now)
            if due_only and due_in > timedelta(0):
                continue
            table.add_row(
                d.target, d.probe, _interval(d.interval), _ago(d.last_run, now), _in(due_in)
            )
            shown += 1
        console.print(table)
        atable = Table(title="assertion cadences")
        for col in ("assertion", "every", "last run", "next"):
            atable.add_column(col, no_wrap=True)
        for a in assertions:
            due_at = (a.last_run + a.interval) if a.last_run else now
            due_in = due_at - now
            if due_only and due_in > timedelta(0):
                continue
            atable.add_row(
                a.assertion.name, _interval(a.interval), _ago(a.last_run, now), _in(due_in)
            )
            shown += 1
        console.print(atable)
        console.print(
            f"{shown} row(s); {len(probes)} probe slot(s), {len(assertions)} assertion(s) scheduled"
        )
        return 0

    raise typer.Exit(code=asyncio.run(_go()))
