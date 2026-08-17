"""`helper chat` — talk to the lab (Phase 4, AC1).

One-shot (``helper chat "what hosts do I have?"``) or a REPL (no argument).
Each conversation is grounded in the reconciled lab context from
:func:`build_lab_context`; the model is told the L1 stance and answers from
those facts. Every reply prints which backend served it — the router never
silently picks for you without saying so.
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from homelab_helper.config import database_url
from homelab_helper.db.models import Host
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.host_probe import HostProbeRequest, probe_host
from homelab_helper.llm import LLMRouter, PrivacyPolicy, RouterRefusal, TaskClass, router_from_env
from homelab_helper.llm.context import build_lab_context
from homelab_helper.llm.discovery import (
    DISCOVERY_SYSTEM_TEMPLATE,
    apply_proposal,
    parse_agent_reply,
    render_hosts_for_prompt,
    validate_proposal,
)

chat_app = typer.Typer(name="chat", help="Chat with the harness about your lab.")

console = Console()

_SYSTEM_TEMPLATE = (
    "You are homelab-helper's assistant. You answer questions about the "
    "operator's homelab using the reconciled inventory below. It is the "
    "source of truth — answer from it, say so when it cannot answer, and do "
    "not invent hosts, services, or findings. The tool observes and proposes "
    "but never changes anything (L1); phrase suggestions accordingly. "
    "Reference findings by fingerprint when discussing problems.\n\n"
    "=== LAB INVENTORY ===\n{context}\n=== END INVENTORY ==="
)

_EXIT_WORDS = {"exit", "quit", "q"}


def _load_router() -> LLMRouter:
    """Factory (monkeypatched in tests)."""
    return router_from_env()


async def _lab_system_prompt() -> str:
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            context = await build_lab_context(session)
    finally:
        await engine.dispose()
    return _SYSTEM_TEMPLATE.format(context=context)


def _footer(backend: str, model: str, tier_name: str, local: bool) -> str:
    origin = "local" if local else "cloud"
    return f"[{backend}: {model} ({tier_name.lower()}, {origin})]"


@chat_app.callback(invoke_without_command=True)
def chat(
    question: str | None = typer.Argument(None, help="One-shot question; omit for a REPL."),
    privacy: str | None = typer.Option(
        None, "--privacy", help="Override the privacy policy (strict-local/prefer-local/open)."
    ),
) -> None:
    """Ask about the lab; grounded in the reconciled inventory."""

    async def _go() -> int:
        router = _load_router()
        if privacy is not None:
            router.policy = PrivacyPolicy(privacy)
        try:
            system = await _lab_system_prompt()
            history: list[dict[str, str]] = []

            async def _ask(text: str) -> int:
                history.append({"role": "user", "content": text})
                try:
                    result = await router.complete(TaskClass.CHAT, system, history)
                except RouterRefusal as refusal:
                    history.pop()
                    console.print(f"[red]refused:[/red] {refusal}")
                    return 2
                history.append({"role": "assistant", "content": result.text})
                console.print(result.text)
                footer = _footer(result.backend, result.model, result.tier.name, result.local)
                console.print(f"[dim]{escape(footer)}[/dim]")
                return 0

            if question is not None:
                return await _ask(question)

            console.print("[dim]chatting with your lab — 'exit' to leave[/dim]")
            while True:
                try:
                    line = console.input("[bold cyan]you>[/bold cyan] ")
                except (EOFError, KeyboardInterrupt):
                    break
                line = line.strip()
                if not line:
                    continue
                if line.lower() in _EXIT_WORDS:
                    break
                await _ask(line)
            return 0
        finally:
            await router.aclose()

    raise typer.Exit(code=asyncio.run(_go()))


_MAX_TURNS = 30
_MAX_PARSE_RETRIES = 3


def _proposal_table(proposal: Any) -> Table:
    table = Table(title="proposed host")
    table.add_column("field", no_wrap=True)
    table.add_column("value", overflow="fold")
    for field_name in (
        "hostname",
        "primary_ip",
        "role",
        "arch",
        "ssh_user",
        "ssh_key_path",
        "notes",
    ):
        table.add_row(field_name, str(getattr(proposal, field_name) or "—"))
    return table


# `helper onboard` is its own top-level verb (not a chat subcommand): a click
# group whose callback also takes a positional argument would make
# `helper chat <question>` ambiguous with subcommand resolution.
onboard_app = typer.Typer(
    name="onboard",
    help="Onboard a new host conversationally: interview → confirm → register.",
    invoke_without_command=True,
)


async def _proposal_step(sm: Any, proposal: Any) -> tuple[Any | None, str]:
    """Validate → show → confirm → apply. Returns (registered?, model feedback)."""
    async with sm() as session:
        errors = await validate_proposal(session, proposal)
    if errors:
        for e in errors:
            console.print(f"[yellow]rejected:[/yellow] {e}")
        return None, (
            "The harness rejected the proposal: "
            + "; ".join(errors)
            + ". Ask the operator how to correct it."
        )
    console.print(_proposal_table(proposal))
    if not typer.confirm("Register this host?", default=True):
        return None, "The operator declined that proposal. Ask what to change."
    async with session_scope(sm) as session:
        host = await apply_proposal(session, proposal)
        hostname = host.hostname
    console.print(f"[green]registered[/green] {hostname}")
    return proposal, (
        f"The harness registered {hostname}. Tell the operator it's done and reply with done=true."
    )


async def _post_registration(sm: Any, registered: Any, probe: bool) -> None:
    """Print or run the warm-discovery follow-up for a registered host."""
    if not (registered.ssh_user and registered.ssh_key_path):
        console.print(
            f"[dim]next: helper discover host {registered.hostname} "
            "--ssh-user <user> --ssh-key <path>[/dim]"
        )
        return
    command = (
        f"helper discover host {registered.hostname} "
        f"--ssh-user {registered.ssh_user} --ssh-key {registered.ssh_key_path}"
    )
    if not probe:
        console.print(f"[dim]next: {command}[/dim]")
        return
    console.print("[cyan]running warm discovery...[/cyan]")
    async with session_scope(sm) as session:
        outcome = await probe_host(
            session,
            HostProbeRequest(
                name=registered.hostname,
                ssh_user=registered.ssh_user,
                ssh_key_path=registered.ssh_key_path,
                primary_ip=registered.primary_ip,
            ),
        )
    if outcome.session_error:
        console.print(f"[yellow]probe failed:[/yellow] {outcome.session_error}")
        console.print(f"[dim]retry later with: {command}[/dim]")
    else:
        console.print(
            f"[green]warm discovery[/green]: {outcome.observations} "
            f"observation(s), {outcome.failures} failure(s)"
        )


@onboard_app.callback(invoke_without_command=True)
def onboard(
    description: str | None = typer.Argument(
        None, help='Opening description, e.g. "a new mini-PC at 10.0.6.27".'
    ),
    probe: bool = typer.Option(
        False,
        "--probe",
        help="After registration, run warm SSH discovery if credentials were collected.",
    ),
) -> None:
    """Interview → validate → confirm → register (writes only after your yes)."""

    async def _go() -> int:
        router = _load_router()
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                hosts = list((await session.execute(select(Host))).scalars().all())
            system = DISCOVERY_SYSTEM_TEMPLATE.replace("{hosts}", render_hosts_for_prompt(hosts))
            opening = description or "I want to add a new host to my inventory."
            history: list[dict[str, str]] = [{"role": "user", "content": opening}]
            parse_failures = 0
            registered: Any = None

            for _ in range(_MAX_TURNS):
                try:
                    result = await router.complete(TaskClass.DISCOVERY, system, history)
                except RouterRefusal as refusal:
                    console.print(f"[red]refused:[/red] {refusal}")
                    return 2
                history.append({"role": "assistant", "content": result.text})
                turn = parse_agent_reply(result.text)

                if turn.parse_error:
                    parse_failures += 1
                    if parse_failures >= _MAX_PARSE_RETRIES:
                        console.print(
                            "[red]the model can't hold the interview protocol; "
                            "try a more capable backend[/red]"
                        )
                        return 2
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                f"Protocol error: {turn.parse_error}. Reply again with "
                                "exactly one JSON object and no other text."
                            ),
                        }
                    )
                    continue
                parse_failures = 0
                console.print(turn.say)

                if turn.proposal is not None:
                    outcome, feedback = await _proposal_step(sm, turn.proposal)
                    if outcome is not None:
                        registered = outcome
                    history.append({"role": "user", "content": feedback})
                    continue

                if turn.done:
                    break

                try:
                    line = console.input("[bold cyan]you>[/bold cyan] ")
                except (EOFError, KeyboardInterrupt):
                    break
                line = line.strip()
                if not line or line.lower() in _EXIT_WORDS:
                    break
                history.append({"role": "user", "content": line})

            if registered is not None:
                await _post_registration(sm, registered, probe)
            return 0
        finally:
            await router.aclose()
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


__all__ = ["chat_app", "onboard_app"]
