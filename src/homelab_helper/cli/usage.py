"""``helper usage`` — what hosts and guests actually used (Phase 8.3).

Reads the rollups ``helper discover usage`` maintains: CPU p95 and peak as a
share of allocated CPUs, memory p95 and peak against the allocation, over a
window. Read-only.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from homelab_helper import mcp_server

usage_app = typer.Typer(
    name="usage", help="Usage history: p95 and peak CPU/memory per host and guest."
)
console = Console()
_GIB = 1024**3


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.0f}%"


def _gib(x: float | None) -> str:
    return "—" if x is None else f"{x / _GIB:.1f}"


@usage_app.callback(invoke_without_command=True)
def usage_show(
    subject: str | None = typer.Argument(None, help="Node name, guest name or vmid; omit for all."),
    days: int = typer.Option(30, "--days", help="Window in days."),
) -> None:
    rows = asyncio.run(mcp_server.usage_summary(subject=subject, days=days))
    if not rows:
        console.print("no usage history yet — run `helper discover usage`")
        raise typer.Exit(code=0)
    table = Table(title=f"usage, last {days} days (hourly buckets)")
    for col in (
        "type",
        "name",
        "buckets",
        "cpus",
        "cpu p95",
        "cpu peak",
        "mem GiB",
        "mem p95",
        "mem peak",
    ):
        table.add_column(col, no_wrap=col in ("type", "buckets"))
    for r in rows:
        table.add_row(
            r["type"],
            escape(str(r.get("label") or r["subject"])),
            str(r["samples"]),
            "—" if r.get("cpus") is None else f"{r['cpus']:g}",
            _pct(r.get("cpu_p95")),
            _pct(r.get("cpu_peak")),
            _gib(r.get("mem_total")),
            _gib(r.get("mem_p95")),
            _gib(r.get("mem_peak")),
        )
    console.print(table)
    console.print(f"{len(rows)} subject(s)")
