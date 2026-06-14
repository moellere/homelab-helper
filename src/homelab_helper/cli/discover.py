"""`helper discover ...` subcommands — run probes against real targets.

Phase-1 scope: discovery against a Linux host over SSH using the
``host.identity`` probe. As more host probes land (cpu, memory, storage, ...)
they're auto-included unless ``--probe`` filters them.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from homelab_helper.adapters.kernel_ssh import KernelSSHAdapter
from homelab_helper.cli._probe_sync import sync_probes_sync
from homelab_helper.db.models import Host, Observation
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.reconciler import Reconciler
from homelab_helper.engine.runner import ProbeRunner
from homelab_helper.probes.base import AdapterRegistry, ProbeTarget
from homelab_helper.probes.network.fingerprint import NetworkFingerprintProbe
from homelab_helper.probes.network.subnet_scan import NetworkSubnetScanProbe
from homelab_helper.probes.registry import discover_probes

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.probes.base import Probe

discover_app = typer.Typer(
    name="discover",
    help="Run discovery probes against targets.",
    no_args_is_help=True,
)

console = Console()


def _database_url() -> str:
    return os.environ.get("HOMELAB_HELPER_DATABASE_URL") or "sqlite+aiosqlite:///./homelab.db"


async def _resolve_host(session, name: str, primary_ip: str | None) -> Host:
    """Find an existing Host by hostname; create one if missing."""
    existing = (
        await session.execute(select(Host).where(Host.hostname == name))
    ).scalar_one_or_none()
    if existing is not None:
        if primary_ip and not existing.primary_ip:
            existing.primary_ip = primary_ip
        return existing
    host = Host(hostname=name, primary_ip=primary_ip)
    session.add(host)
    await session.flush()
    return host


def _resolve_probes(filter_names: list[str] | None) -> list[type[Probe]]:
    """Pick the probes to run — by name filter, or all host-kind probes."""
    available = discover_probes()
    if filter_names:
        chosen: list[type[Probe]] = []
        for n in filter_names:
            if n not in available:
                console.print(f"[red]unknown probe:[/red] {n}")
                sys.exit(2)
            chosen.append(available[n])
        return chosen
    return [cls for cls in available.values() if "host" in cls.target_kinds]


async def _reconcile_and_report(session: AsyncSession, host_id: uuid.UUID) -> None:
    """Run the reconciler for a host and print the deltas."""
    result = await Reconciler().reconcile_host(session, host_id)
    console.print(
        f"\n[cyan]reconciled[/cyan]: {result.observations_seen} "
        f"observation(s) applied, {len(result.changes)} change(s)"
    )
    for field_name, value in result.changes.items():
        console.print(f"  [dim]+[/dim] {field_name} = {value!r}")
    if result.touched_lineage:
        skip_bits: list[str] = []
        if result.parts_skipped_no_identity:
            skip_bits.append(f"{result.parts_skipped_no_identity} skipped (no identity)")
        if result.parts_skipped_filtered:
            skip_bits.append(f"{result.parts_skipped_filtered} skipped (virtual interface)")
        console.print(
            f"  [cyan]lineage[/cyan]: {result.parts_upserted} part(s) upserted, "
            f"{len(result.placements_opened)} placement(s) opened, "
            f"{len(result.placements_closed)} closed"
            + ("" if not skip_bits else ", " + ", ".join(skip_bits))
        )
        for serial, slot in result.placements_opened:
            console.print(f"  [dim]+ placement[/dim] {serial!r} @ {slot}")
        for serial, slot in result.placements_closed:
            console.print(f"  [dim]- placement[/dim] {serial!r} @ {slot}")
    if result.touched_findings:
        console.print(
            f"  [cyan]findings[/cyan]: {len(result.findings_opened)} opened, "
            f"{len(result.findings_resolved)} auto-resolved"
            + (f", {len(result.findings_updated)} re-seen" if result.findings_updated else "")
        )


@discover_app.command(name="host")
def discover_host(
    name: str = typer.Argument(..., help="Hostname (NetBox/DB) of the target."),
    ssh_user: str = typer.Option(..., "--ssh-user", "-u", help="SSH login user."),
    ssh_key: Path | None = typer.Option(
        None, "--ssh-key", "-k", help="Path to an SSH private key.", exists=False
    ),
    ssh_password_env: str | None = typer.Option(
        None,
        "--ssh-password-env",
        help="Name of an env var holding the SSH password. (Never accept the password directly.)",
    ),
    primary_ip: str | None = typer.Option(
        None, "--primary-ip", help="Override hostname for the SSH connection."
    ),
    ssh_port: int = typer.Option(22, "--ssh-port"),
    probe_names: list[str] | None = typer.Option(
        None, "--probe", help="Restrict to these probe names. Default: every host probe."
    ),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip the probe-entry-point sync."),
) -> None:
    """Discover a single host: run all registered host probes over SSH."""
    if ssh_key is None and ssh_password_env is None:
        console.print(
            "[red]error:[/red] supply --ssh-key or --ssh-password-env "
            "(or set HOMELAB_HELPER_SSH_KEY beforehand)."
        )
        raise typer.Exit(code=2)

    ssh_password = os.environ.get(ssh_password_env) if ssh_password_env else None
    if ssh_password_env and not ssh_password:
        console.print(f"[yellow]warning:[/yellow] env var {ssh_password_env} is empty")

    if not no_sync:
        console.print("[dim]syncing probe entry points...[/dim]")
        sync_probes_sync(_database_url())

    probe_classes = _resolve_probes(probe_names or None)
    if not probe_classes:
        console.print("[red]error:[/red] no probes matched the filter.")
        raise typer.Exit(code=2)

    ssh_adapter = KernelSSHAdapter()
    adapters = AdapterRegistry({"kernel-ssh": ssh_adapter})
    runner = ProbeRunner(adapters)

    async def _go() -> int:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            total_observations = 0
            failures = 0
            async with session_scope(sm) as session:
                host = await _resolve_host(session, name, primary_ip)
                connect_host = host.primary_ip or host.hostname or name
                target = ProbeTarget(
                    kind="host",
                    host_id=str(host.id),
                    hostname=host.hostname,
                    primary_ip=host.primary_ip,
                    ssh_user=ssh_user,
                    ssh_key_path=str(ssh_key) if ssh_key else None,
                    ssh_password=ssh_password,
                    ssh_port=ssh_port,
                )

                # One SSH connection shared across the whole probe batch.
                try:
                    async with ssh_adapter.shared_session(
                        connect_host,
                        user=ssh_user,
                        key_path=str(ssh_key) if ssh_key else None,
                        password=ssh_password,
                        port=ssh_port,
                    ):
                        for cls in probe_classes:
                            probe = cls()
                            console.print(f"[cyan]→ {probe.name}[/cyan] v{probe.version}")
                            _run_row, result = await runner.run(
                                probe, target, session, host_id=host.id, triggered_by="manual"
                            )
                            if result.success:
                                n = len(result.observations)
                                total_observations += n
                                console.print(f"  [green]ok[/green] - {n} observation(s)")
                            else:
                                failures += 1
                                console.print(f"  [red]failed[/red] - {result.error}")
                except Exception as exc:
                    console.print(f"[red]could not establish SSH session:[/red] {exc}")
                    failures += 1

                # Reconcile from whatever observations landed (partial runs are
                # worth applying). reconcile_host is idempotent.
                await _reconcile_and_report(session, host.id)

            console.print(
                f"\nrun complete: [bold]{total_observations}[/bold] observation(s), "
                f"[bold]{failures}[/bold] failure(s)"
            )
            return 0 if failures == 0 else 1
        finally:
            await engine.dispose()

    exit_code = asyncio.run(_go())
    raise typer.Exit(code=exit_code)


@discover_app.command(name="show")
def discover_show(
    hostname: str = typer.Argument(..., help="Hostname to show observations for."),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """Print the most recent observations for a host (debug helper)."""

    async def _go() -> None:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                host = (
                    await session.execute(select(Host).where(Host.hostname == hostname))
                ).scalar_one_or_none()
                if host is None:
                    console.print(f"[red]no host named {hostname!r} in DB[/red]")
                    return
                rows = (
                    (
                        await session.execute(
                            select(Observation)
                            .where(Observation.target_id == str(host.id))
                            .order_by(Observation.recorded_at.desc())
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    console.print("[yellow]no observations yet[/yellow]")
                    return
                for r in rows:
                    console.print(
                        f"[dim]{r.recorded_at.isoformat()}[/dim] [cyan]{r.key}[/cyan] = {r.value!r}"
                    )
        finally:
            await engine.dispose()

    asyncio.run(_go())


def _parse_ports(raw: str | None) -> tuple[int, ...] | None:
    if raw is None:
        return None
    out: list[int] = []
    for chunk in raw.split(","):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        try:
            out.append(int(cleaned))
        except ValueError as exc:
            raise typer.BadParameter(f"--ports: {cleaned!r} is not an integer") from exc
    return tuple(out)


@discover_app.command(name="network")
def discover_network(
    cidr: str = typer.Argument(..., help="Network range to scan, e.g. 10.250.6.0/24."),
    ports: str | None = typer.Option(
        None,
        "--ports",
        help="Comma-separated port list. Default: a small panel of common services.",
    ),
    timeout: float = typer.Option(0.8, "--timeout", help="Per-port connect timeout in seconds."),
    concurrency: int = typer.Option(
        200, "--concurrency", min=1, max=2000, help="Max concurrent TCP connects."
    ),
    no_fingerprint: bool = typer.Option(
        False,
        "--no-fingerprint",
        help="Skip the per-host service fingerprint step.",
    ),
) -> None:
    """Scan a CIDR for live hosts; optionally fingerprint discovered services.

    No SSH credentials needed — pure asyncio TCP connect-scan against a small
    panel of common ports. Observations are written to the harness DB; the
    reconciler treats the recorded ``network.*`` keys as evidence going
    forward. NetBox sync happens in a later slice (after NetBoxAdapter lands).
    """
    port_tuple = _parse_ports(ports)

    async def _go() -> int:
        scan_probe = NetworkSubnetScanProbe(
            ports=port_tuple,
            per_port_timeout_s=timeout,
            concurrency=concurrency,
        )
        runner = ProbeRunner(AdapterRegistry())

        db_engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(db_engine)
            async with session_scope(sm) as session:
                console.print(f"[cyan]→ scanning {cidr}[/cyan]")
                _scan_run, scan_result = await runner.run(
                    scan_probe,
                    ProbeTarget(kind="network", network_cidr=cidr),
                    session,
                    triggered_by="manual",
                )
                if not scan_result.success:
                    console.print(f"[red]scan failed:[/red] {scan_result.error}")
                    return 1

                live = (scan_result.raw_payload or {}).get("live_hosts", [])
                console.print(
                    f"  [green]{len(live)}[/green] live host(s) out of "
                    f"{(scan_result.raw_payload or {}).get('scanned_count', 0)} scanned"
                )
                if not live:
                    return 0

                table = Table(show_header=True, header_style="bold")
                table.add_column("ip", no_wrap=True)
                table.add_column("open ports", overflow="fold")
                table.add_column("identified services", overflow="fold")
                identified: list[tuple[str, list[dict[str, object]]]] = []

                if no_fingerprint:
                    for h in live:
                        table.add_row(h["ip"], ", ".join(str(p) for p in h["open_ports"]), "—")
                else:
                    for h in live:
                        console.print(f"[cyan]→ fingerprint {h['ip']}[/cyan]")
                        # Fingerprint exactly the ports the scan found open —
                        # avoids spending time on closed defaults.
                        fp_probe = NetworkFingerprintProbe(ports=h["open_ports"])
                        _fp_run, fp_result = await runner.run(
                            fp_probe,
                            ProbeTarget(
                                kind="network",
                                primary_ip=h["ip"],
                            ),
                            session,
                            triggered_by="manual",
                        )
                        services = (fp_result.raw_payload or {}).get("services", [])
                        identified.append((h["ip"], services))
                        svc_str = (
                            ", ".join(
                                (f"{s['port']}:{s.get('service') or s.get('hint', '?')}")
                                for s in services
                            )
                            or "—"
                        )
                        table.add_row(
                            h["ip"],
                            ", ".join(str(p) for p in h["open_ports"]),
                            svc_str,
                        )

                console.print(table)
            return 0
        finally:
            await db_engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))
