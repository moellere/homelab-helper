"""`helper skills` — view and pin the operator skill profile (Phase 4, AC6).

The profile accumulates passively from chat; this verb is the inspection and
override surface. ``set`` pins a domain (``source=manual``) so inference can
no longer change its level — the operator always outranks the inferer.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from homelab_helper.config import database_url
from homelab_helper.db.enums import SkillLevel
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.skill_inferer import LEXICON, get_profile, set_skill

skills_app = typer.Typer(
    name="skills",
    help="Operator skill profile (inferred from chat; feeds future trust hints).",
    no_args_is_help=False,
    invoke_without_command=True,
)

console = Console()


@skills_app.callback(invoke_without_command=True)
def skills(ctx: typer.Context) -> None:
    """Show the current skill profile."""
    if ctx.invoked_subcommand is not None:
        return

    async def _go() -> None:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                rows = await get_profile(session)
        finally:
            await engine.dispose()
        if not rows:
            console.print(
                "[dim]no skill signal yet — the profile builds up as you use "
                "`helper chat`; pin one with `helper skills set <domain> <level>`[/dim]"
            )
            return
        table = Table(title="skill profile")
        for col in ("domain", "level", "source", "evidence"):
            table.add_column(col, no_wrap=True)
        for r in rows:
            table.add_row(r.domain, r.level.value, r.source.value, str(r.evidence_count))
        console.print(table)

    asyncio.run(_go())


@skills_app.command(name="set")
def skills_set(
    domain: str = typer.Argument(..., help=f"Domain (known: {', '.join(sorted(LEXICON))})."),
    level: str = typer.Argument(..., help="novice, basic, intermediate, or advanced."),
) -> None:
    """Pin a domain's level (manual overrides inference permanently)."""
    try:
        parsed = SkillLevel(level.lower())
    except ValueError as exc:
        allowed = ", ".join(s.value for s in SkillLevel)
        raise typer.BadParameter(f"level must be one of: {allowed}") from exc

    async def _go() -> None:
        engine = make_engine(database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                row = await set_skill(session, domain.lower(), parsed)
                console.print(f"[green]pinned[/green] {row.domain} = {row.level.value} (manual)")
        finally:
            await engine.dispose()

    asyncio.run(_go())


__all__ = ["skills_app"]
