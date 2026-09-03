"""`helper audit` — the high-level summary across the lab.

A read-only roll-up. Counts hosts, parts, current placements, and findings;
breaks findings down by severity-by-status; surfaces the most recent open
findings as a teaser of what to drill into with ``helper findings list`` or
``helper findings show``.

This is the bones of the day-one audit acceptance criterion (AC3 in the
roadmap). Specific finding-kind counts will grow as more reconciler and
assertion-engine slices add new finding categories.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from homelab_helper.config import database_url as _database_url
from homelab_helper.db.enums import FindingSeverity, FindingStatus
from homelab_helper.db.models import (
    Cluster,
    Host,
    PhysicalPart,
    Placement,
    ReconciliationFinding,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker

audit_app = typer.Typer(
    name="audit",
    help="High-level lab audit roll-up.",
    no_args_is_help=False,
    invoke_without_command=True,
)

console = Console()


@audit_app.callback(invoke_without_command=True)
def audit(
    top: int = typer.Option(
        5, "--top", "-n", min=0, max=50, help="Show this many most-recent open findings."
    ),
) -> None:
    """Print a roll-up of inventory and audit findings."""

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                hosts = (await session.execute(select(func.count(Host.id)))).scalar_one()
                parts = (await session.execute(select(func.count(PhysicalPart.id)))).scalar_one()
                placements_now = (
                    await session.execute(
                        select(func.count(Placement.id)).where(Placement.to_date.is_(None))
                    )
                ).scalar_one()
                placements_total = (
                    await session.execute(select(func.count(Placement.id)))
                ).scalar_one()
                findings_total = (
                    await session.execute(select(func.count(ReconciliationFinding.id)))
                ).scalar_one()
                clusters = (await session.execute(select(func.count(Cluster.id)))).scalar_one()
                vms = (await session.execute(select(func.count(VirtualMachine.id)))).scalar_one()

                console.print("[bold]inventory[/bold]")
                console.print(f"  hosts:                  {hosts}")
                console.print(
                    f"  physical parts:         {parts}  "
                    f"[dim]({placements_now} placed now / {placements_total} placement rows)[/dim]"
                )
                if clusters or vms:
                    console.print(f"  clusters / VMs:         {clusters} / {vms}")
                console.print(f"  findings (all-time):    {findings_total}")

                # Findings crosstab: severity x status.
                crosstab_stmt = select(
                    ReconciliationFinding.severity,
                    ReconciliationFinding.status,
                    func.count(ReconciliationFinding.id),
                ).group_by(
                    ReconciliationFinding.severity,
                    ReconciliationFinding.status,
                )
                rows = (await session.execute(crosstab_stmt)).all()
                if rows:
                    matrix: dict[FindingSeverity, dict[FindingStatus, int]] = {
                        s: dict.fromkeys(FindingStatus, 0) for s in FindingSeverity
                    }
                    for sev, status, n in rows:
                        matrix[sev][status] = n

                    console.print("\n[bold]findings by severity / status[/bold]")
                    table = Table(show_header=True, header_style="bold")
                    table.add_column("severity", no_wrap=True)
                    for st in FindingStatus:
                        table.add_column(st.value, justify="right")
                    table.add_column("total", justify="right", style="bold")
                    severity_order = [
                        FindingSeverity.CRITICAL,
                        FindingSeverity.HIGH,
                        FindingSeverity.MEDIUM,
                        FindingSeverity.LOW,
                        FindingSeverity.INFO,
                    ]
                    for sev in severity_order:
                        row_counts = [matrix[sev][st] for st in FindingStatus]
                        total = sum(row_counts)
                        if total == 0:
                            continue
                        table.add_row(
                            sev.value,
                            *(str(c) for c in row_counts),
                            str(total),
                        )
                    console.print(table)

                # Top-N most recent open findings.
                if top > 0:
                    open_stmt = (
                        select(ReconciliationFinding)
                        .where(
                            ReconciliationFinding.status.in_(
                                [FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED]
                            )
                        )
                        .order_by(ReconciliationFinding.last_seen.desc())
                        .limit(top)
                    )
                    open_rows = (await session.execute(open_stmt)).scalars().all()
                    if open_rows:
                        console.print(f"\n[bold]most recent open ({len(open_rows)})[/bold]")
                        t2 = Table(show_header=True, header_style="bold")
                        t2.add_column("fingerprint", style="dim", no_wrap=True)
                        t2.add_column("sev")
                        t2.add_column("kind", no_wrap=True)
                        t2.add_column("title", overflow="fold")
                        for f in open_rows:
                            t2.add_row(
                                f.fingerprint,
                                f.severity.value,
                                f.kind.value,
                                f.title,
                            )
                        console.print(t2)
                    else:
                        console.print("\n[dim]no open findings[/dim]")
        finally:
            await engine.dispose()

    asyncio.run(_go())
