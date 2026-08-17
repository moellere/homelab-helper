"""`helper chat` — talk to the lab (Phase 4, AC1).

One-shot (``helper chat "what hosts do I have?"``) or a REPL (no argument).
Each conversation is grounded in the reconciled lab context from
:func:`build_lab_context`; the model is told the L1 stance and answers from
those facts. Every reply prints which backend served it — the router never
silently picks for you without saying so.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.markup import escape

from homelab_helper.config import database_url
from homelab_helper.db.session import make_engine, make_sessionmaker
from homelab_helper.llm import LLMRouter, PrivacyPolicy, RouterRefusal, TaskClass, router_from_env
from homelab_helper.llm.context import build_lab_context

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


__all__ = ["chat_app"]
