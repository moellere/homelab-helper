"""``helper daemon run`` — the proactive loop (Phase 7 slice 3; the deferred Phase-2 scheduler).

Jobs on their own cadences, in one long-lived process:

- **discovery** — the same per-source discoverers the MCP ``run_discovery`` tool
  runs (read-only against the lab; findings land in the harness DB);
- **playbooks** — ``engine/playbooks.run_playbooks``: OPEN findings a playbook
  covers become PENDING proposals (deterministic; see that module);
- **listen** — ``engine/listener.ask_pending``: agent- and playbook-drafted
  proposals that policy would let run go to the approval channel (the phone),
  and execute on a tap through the same executor as everything else;
- **schedule** (Phase 9.6) — ``engine/schedule.run_due``: with a cadence file,
  each host/Talos probe and each assertion runs on its own interval, judged
  from its last recorded run, so ``--once`` from cron honours them too.

``--once`` runs each enabled job a single time and exits — the shape cron and
the tests want. Nothing here changes authority: cells, windows and overrides
remain CLI gestures, and a cell at PROPOSE still produces nothing but a row.
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.markup import escape

from homelab_helper.config import config_status, database_url
from homelab_helper.db.session import (
    advisory_lock,
    make_engine,
    make_sessionmaker,
    session_scope,
)
from homelab_helper.engine.approval import (
    ApprovalConfigError,
    HomeAssistantApprovalConfig,
    approval_channel_from_env,
)
from homelab_helper.engine.digest import build_digest, record_digest, render_notification
from homelab_helper.engine.listener import Adapters, ask_pending, listener_lock
from homelab_helper.engine.notify import notifier_from_env, send_digest
from homelab_helper.engine.playbooks import run_playbooks
from homelab_helper.engine.schedule import (
    SCHEDULE_ASSERTIONS_LOCK_KEY,
    SCHEDULE_ENV_VAR,
    SCHEDULE_PROBES_LOCK_KEY,
    ScheduleError,
    ScheduleRun,
    load_schedule,
    run_due,
)

if TYPE_CHECKING:
    from homelab_helper.engine.executor import ActionManifest

daemon_app = typer.Typer(
    name="daemon",
    help="Run discovery, playbooks and the approval listener on a cadence.",
    no_args_is_help=True,
)
console = Console(soft_wrap=True)

MIN_DIGEST_DAYS = 6.0
"""A digest covering less than this is not due yet — so a restart, or a
15-minute cron tick, cannot turn a weekly summary into a stream."""


def _stamp() -> str:
    return datetime.now(UTC).astimezone().strftime("%H:%M:%S")


async def _adapters_for(manifest: ActionManifest) -> Adapters | str:
    """The listener's adapter resolver — the same one the MCP trigger uses."""
    from homelab_helper.mcp_server import _execution_adapters  # noqa: PLC0415 - interface→interface

    adapters, problem = _execution_adapters(manifest)
    if problem is not None or adapters is None:
        return problem or "adapters unavailable"
    return (
        adapters.proxmox,
        adapters.k8s,
        adapters.argocd,
        adapters.unifi,
        adapters.ssh,
        adapters.talos,
    )


def configured_sources() -> list[str]:
    """The discovery sources ``helper config`` reports configured — the default
    when ``--sources`` is not given, so a lab without Argo CD is never asked
    about Argo CD every hour because the author's lab had it."""
    from homelab_helper.mcp_server import _DISCOVERERS  # noqa: PLC0415 - interface→interface

    return [s for s in config_status()["configured_sources"] if s in _DISCOVERERS]


async def run_discovery_pass(sources: list[str]) -> dict[str, Any]:
    from homelab_helper.mcp_server import run_discovery  # noqa: PLC0415 - interface→interface

    out: dict[str, Any] = {}
    for src in sources:
        out[src] = await run_discovery(src)
    return out


async def run_playbook_pass() -> dict[str, Any]:
    engine = make_engine(database_url())
    try:
        async with session_scope(make_sessionmaker(engine)) as session:
            r = await run_playbooks(session)
            return {
                "drafted": r.drafted,
                "skipped_pending": len(r.skipped_live),
                "skipped_cooldown": len(r.skipped_cooldown),
                "skipped_young": len(r.skipped_young),
                "withdrawn": r.withdrawn,
                "skipped_done": len(r.skipped_done),
                "no_playbook": r.no_playbook,
            }
    finally:
        await engine.dispose()


async def run_listen_pass() -> dict[str, Any]:
    try:
        channel = approval_channel_from_env()
    except ApprovalConfigError as exc:
        return {"error": str(exc)}
    engine = make_engine(database_url())
    try:
        async with listener_lock(engine) as held:
            if not held:
                return {"skipped": "another listener pass is still running"}
            async with session_scope(make_sessionmaker(engine)) as session:
                r = await ask_pending(
                    session,
                    channel=channel,
                    adapters_for=_adapters_for,
                    notifier=notifier_from_env(),
                )
            return {
                "asked": r.asked,
                "executed": r.executed,
                "declined": r.declined,
                "refused_by_policy": r.refused,
                "already_asked": r.already_asked,
                "unconfigured": r.unconfigured,
                "errors": r.errors,
            }
    finally:
        await engine.dispose()


async def run_schedule_pass(
    schedule_path: str | None, *, probes: bool, assertions: bool
) -> dict[str, Any]:
    """Phase 9.6: run the probes and assertions whose cadence says they are due."""
    try:
        schedule = load_schedule(schedule_path)
    except ScheduleError as exc:
        return {"error": str(exc)}
    # Each half runs only where the file asks for it, and under its own lock:
    # two daemons on one database (one probing, one asserting) never contend,
    # and two passes of the same half never overlap.
    halves = [
        ("probes", probes and bool(schedule.hosts or schedule.talos), SCHEDULE_PROBES_LOCK_KEY),
        ("assertions", assertions and bool(schedule.assertions), SCHEDULE_ASSERTIONS_LOCK_KEY),
    ]
    total = ScheduleRun()
    skipped: list[str] = []
    engine = make_engine(database_url())
    try:
        sessionmaker = make_sessionmaker(engine)
        for half, wanted, key in halves:
            if not wanted:
                continue
            async with advisory_lock(engine, key) as held:
                if not held:
                    skipped.append(f"{half}: another pass is still running")
                    continue
                async with session_scope(sessionmaker) as session:
                    r = await run_due(
                        session,
                        schedule,
                        probes=half == "probes",
                        assertions=half == "assertions",
                    )
            total.probes_run += r.probes_run
            total.probe_failures += r.probe_failures
            total.probe_findings |= r.probe_findings
            total.assertions_run += r.assertions_run
            total.not_due += r.not_due
            total.errors += r.errors
        return {
            "probes_run": total.probes_run,
            "probe_failures": total.probe_failures,
            "probe_findings": total.probe_findings,
            "assertions_run": total.assertions_run,
            "not_due": total.not_due,
            "errors": total.errors,
            "skipped": skipped,
        }
    finally:
        await engine.dispose()


async def run_digest_pass(days: int | None, quiet_ok: bool) -> dict[str, Any]:
    """Deliver a digest if one is due. The window is the digest's own business:
    a daemon restart cannot re-send, because the last run moved the window. A
    send that failed did not move it, so the next pass tries again."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            digest = await build_digest(session, days=days)
            if digest.window.days < MIN_DIGEST_DAYS:
                return {"skipped": f"last digest {digest.window.days:.1f}d ago"}
            if digest.quiet and not quiet_ok:
                await record_digest(session, digest, delivery="page", detail="quiet window")
                return {"quiet": True}
            title, message = render_notification(digest)
            try:
                config = HomeAssistantApprovalConfig.from_env()
            except ApprovalConfigError as exc:
                await record_digest(session, digest, delivery="unconfigured", detail=str(exc))
                return {"unconfigured": str(exc)}
            try:
                await send_digest(config, title, message)
            except Exception as exc:
                await record_digest(session, digest, delivery="failed", detail=str(exc))
                return {"failed": str(exc)}
            await record_digest(session, digest, delivery="sent")
            return {"sent": title, **digest.counts()}
    finally:
        await engine.dispose()


def _job_errored(result: dict[str, Any]) -> bool:
    """The job as a whole could not run, or one of a discovery pass's sources could not."""
    return "error" in result or any(isinstance(v, dict) and "error" in v for v in result.values())


def _report(job: str, result: dict[str, Any]) -> None:
    summary = ", ".join(
        f"{k}={escape(str(v))}" for k, v in result.items() if v not in ([], 0, None, {}, "")
    )
    console.print(f"[dim]{_stamp()}[/dim] [cyan]{job}[/cyan] {summary or 'nothing to do'}")


@daemon_app.command(name="run")
def daemon_run(
    sources: str | None = typer.Option(
        None,
        "--sources",
        help="Comma-separated discovery sources; default: every source `helper config` "
        "reports configured; '' to disable.",
    ),
    discovery_every: int = typer.Option(
        60, "--discovery-every", help="Minutes between discovery passes."
    ),
    playbooks_every: int = typer.Option(
        10, "--playbooks-every", help="Minutes between playbook passes."
    ),
    listen_every: int = typer.Option(1, "--listen-every", help="Minutes between listener passes."),
    ask: bool = typer.Option(True, "--ask/--no-ask", help="Run the approval listener."),
    playbooks: bool = typer.Option(True, "--playbooks/--no-playbooks", help="Run the playbooks."),
    digest: bool = typer.Option(
        False, "--digest/--no-digest", help="Deliver the weekly digest when one is due."
    ),
    digest_every: int = typer.Option(
        720, "--digest-every", help="Minutes between digest checks (the window gates delivery)."
    ),
    schedule: str | None = typer.Option(
        None,
        "--schedule",
        help=f"Probe/assertion cadence file (default: ${SCHEDULE_ENV_VAR}); '' to disable.",
    ),
    schedule_every: int = typer.Option(
        5,
        "--schedule-every",
        help="Minutes between checks for due probes and assertions (their cadences are in the file).",
    ),
    once: bool = typer.Option(False, "--once", help="One pass of each enabled job, then exit."),
) -> None:
    """Discovery → playbooks → listener, on cadences (or once with --once).

    With a schedule file (``--schedule`` or ``HOMELAB_HELPER_SCHEDULE``) each
    probe and assertion runs on its own cadence from the same process.
    """
    schedule_path = schedule if schedule is not None else os.environ.get(SCHEDULE_ENV_VAR)
    run_schedule = bool(schedule_path)
    source_list = (
        configured_sources()
        if sources is None
        else [s.strip() for s in sources.split(",") if s.strip()]
    )

    errored: list[str] = []

    async def _pass(job: str) -> None:
        try:
            if job == "discovery":
                result = await run_discovery_pass(source_list)
            elif job == "playbooks":
                result = await run_playbook_pass()
            elif job == "listen":
                result = await run_listen_pass()
            elif job == "digest":
                result = await run_digest_pass(None, quiet_ok=False)
            else:
                result = await run_schedule_pass(schedule_path, probes=True, assertions=True)
        except Exception as exc:
            errored.append(job)
            console.print(f"[dim]{_stamp()}[/dim] [red]{job} failed:[/red] {escape(str(exc))}")
            return
        if _job_errored(result):
            errored.append(job)
        _report(job, result)

    jobs: list[tuple[str, int]] = []
    if source_list:
        jobs.append(("discovery", discovery_every))
    if run_schedule:
        jobs.append(("schedule", schedule_every))
    if playbooks:
        jobs.append(("playbooks", playbooks_every))
    if ask:
        jobs.append(("listen", listen_every))
    if digest:
        jobs.append(("digest", digest_every))

    async def _once() -> None:
        for job, _ in jobs:
            await _pass(job)

    async def _forever() -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: PLC0415

        scheduler = AsyncIOScheduler()
        for job, minutes in jobs:
            scheduler.add_job(
                _pass, "interval", minutes=minutes, args=[job], id=job, max_instances=1
            )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        scheduler.start()
        console.print(
            f"[dim]{_stamp()}[/dim] daemon up — "
            + ", ".join(f"{job} every {m} min" for job, m in jobs)
            + " (Ctrl-C to stop)"
        )
        await _once()  # first pass immediately
        await stop.wait()
        scheduler.shutdown(wait=False)
        console.print(f"[dim]{_stamp()}[/dim] daemon stopped")

    if not jobs:
        console.print("[yellow]nothing enabled[/yellow]")
        raise typer.Exit(code=1)
    asyncio.run(_once() if once else _forever())
    if once and errored:
        # cron and a CronJob read the exit status; a pass that could not run
        # (a schedule file that does not parse, a source that is down) must
        # not look like a pass that had nothing to do.
        console.print(f"[red]errors in:[/red] {', '.join(errored)}")
        raise typer.Exit(code=1)


__all__ = [
    "configured_sources",
    "daemon_app",
    "run_discovery_pass",
    "run_listen_pass",
    "run_playbook_pass",
    "run_schedule_pass",
]
