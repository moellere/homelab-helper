"""`helper discover ...` subcommands — run probes against real targets.

Phase-1 scope: discovery against a Linux host over SSH using the
``host.identity`` probe. As more host probes land (cpu, memory, storage, ...)
they're auto-included unless ``--probe`` filters them.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import or_, select

from homelab_helper.adapters.kernel_ssh import KernelSSHAdapter
from homelab_helper.adapters.netbox import NetBoxAdapter, NetBoxConfig
from homelab_helper.adapters.proxmox import ProxmoxAdapter
from homelab_helper.adapters.talos import TalosAdapter
from homelab_helper.cli._probe_sync import sync_probes_sync
from homelab_helper.db.enums import DiscoverySource
from homelab_helper.db.models import Host, Observation
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.lab_replay import load_lab_fixture, parse_lab_fixture
from homelab_helper.engine.reconciler import Reconciler
from homelab_helper.engine.runner import ProbeRunner
from homelab_helper.engine.scan_import import (
    ProbeStrategy,
    ScanImporter,
    classify,
    parse_scan_csv,
)
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


async def _resolve_host(session: AsyncSession, name: str, primary_ip: str | None) -> Host:
    """Find an existing Host by hostname *or* primary_ip; create one if missing.

    Matching by IP as well as name keeps a probe run (which may pass a short
    name) from duplicating a row another source created under a different label
    — e.g. the scan importer storing an FQDN. First-created row wins on ties.
    """
    conditions = [Host.hostname == name]
    if primary_ip:
        conditions.append(Host.primary_ip == primary_ip)
    existing: Host | None = (
        (await session.execute(select(Host).where(or_(*conditions)).order_by(Host.created_at)))
        .scalars()
        .first()
    )
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


def _mark_kernel_probed(host: Host, observations: int) -> None:
    """Flag a directly-probed host so the scan importer treats it as covered."""
    if observations > 0:
        host.discovery_source = DiscoverySource.KERNEL_PROBE


def _load_proxmox_adapter() -> ProxmoxAdapter:
    """Factory (monkeypatched in tests) — builds a Proxmox adapter from env."""
    return ProxmoxAdapter.from_env()


def _load_netbox_adapter() -> NetBoxAdapter:
    """Factory (monkeypatched in tests) — builds a NetBox adapter from env."""
    return NetBoxAdapter(NetBoxConfig.from_env())


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

                _mark_kernel_probed(host, total_observations)

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


@discover_app.command(name="talos")
def discover_talos(
    name: str = typer.Argument(..., help="Node name (NetBox/DB hostname) of the Talos node."),
    node: str | None = typer.Option(
        None, "--node", "-N", help="Talos API endpoint/IP. Defaults to NAME."
    ),
    talosconfig: Path | None = typer.Option(
        None, "--talosconfig", help="Path to a talosconfig (default: talosctl's own).", exists=False
    ),
    probe_names: list[str] | None = typer.Option(
        None, "--probe", help="Restrict to these probe names. Default: every talos probe."
    ),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip the probe-entry-point sync."),
) -> None:
    """Discover a Talos Linux node over the machine API (no SSH)."""
    if not no_sync:
        console.print("[dim]syncing probe entry points...[/dim]")
        sync_probes_sync(_database_url())

    available = discover_probes()
    if probe_names:
        probe_classes: list[type[Probe]] = []
        for n in probe_names:
            if n not in available:
                console.print(f"[red]unknown probe:[/red] {n}")
                raise typer.Exit(code=2)
            probe_classes.append(available[n])
    else:
        probe_classes = [cls for cls in available.values() if "talos" in cls.target_kinds]
    if not probe_classes:
        console.print("[red]error:[/red] no talos probes matched the filter.")
        raise typer.Exit(code=2)

    talos_adapter = TalosAdapter(talosconfig=str(talosconfig) if talosconfig else None)
    runner = ProbeRunner(AdapterRegistry({"talos": talos_adapter}))

    async def _go() -> int:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            total_observations = 0
            failures = 0
            async with session_scope(sm) as session:
                host = await _resolve_host(session, name, node)
                api_node = node or host.primary_ip or host.hostname or name

                ok, err = await talos_adapter.health_check(api_node)
                if not ok:
                    console.print(f"[red]talos node {api_node} unreachable:[/red] {err}")
                    failures += 1
                else:
                    target = ProbeTarget(
                        kind="talos",
                        host_id=str(host.id),
                        hostname=host.hostname,
                        primary_ip=host.primary_ip or api_node,
                    )
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

                    _mark_kernel_probed(host, total_observations)

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


@discover_app.command(name="import")
def discover_import(
    csv_path: Path = typer.Argument(..., help="Path to a host-scan CSV.", exists=True),
    source_label: str = typer.Option(
        "network-scan", "--source", help="Provenance label stored on imported hosts."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Parse + classify and print the plan without writing."
    ),
) -> None:
    """Ingest an external host-scan CSV: upsert hosts + agentless coverage findings."""
    rows = parse_scan_csv(csv_path.read_text())
    if not rows:
        console.print("[yellow]no rows parsed from CSV[/yellow]")
        raise typer.Exit(code=1)

    by_strategy: dict[ProbeStrategy, int] = {}
    for r in rows:
        s = classify(r)
        by_strategy[s] = by_strategy.get(s, 0) + 1

    table = Table(title=f"scan import: {len(rows)} host(s)")
    table.add_column("strategy")
    table.add_column("count", justify="right")
    for strat in ProbeStrategy:
        table.add_row(strat.value, str(by_strategy.get(strat, 0)))
    console.print(table)

    if dry_run:
        console.print("[dim]dry-run — no rows written.[/dim]")
        ssh_pending = [r for r in rows if classify(r) is ProbeStrategy.SSH]
        console.print(
            f"[cyan]{len(ssh_pending)}[/cyan] SSH-probeable host(s) ready for deep probe."
        )
        return

    async def _go() -> int:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                result = await ScanImporter().import_rows(
                    session, rows, when=datetime.now(UTC), source_label=source_label
                )
            console.print(
                f"\n[green]imported[/green]: {result.hosts_created} host(s) created, "
                f"{result.hosts_updated} updated"
            )
            console.print(
                f"[cyan]agentless findings[/cyan]: {len(result.findings_opened)} opened, "
                f"{len(result.findings_reseen)} re-seen, "
                f"{len(result.findings_resolved)} resolved (now deep-probed), "
                f"{result.already_probed_skipped} covered hosts skipped"
            )
            return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@discover_app.command(name="replay")
def discover_replay(
    fixture: Path = typer.Argument(..., help="Path to a lab-replay YAML fixture.", exists=True),
    no_assert: bool = typer.Option(
        False, "--no-assert", help="Skip the fixture's bundled assertion library."
    ),
) -> None:
    """Replay a synthetic lab fixture (hosts + observations) — no live access."""
    data = parse_lab_fixture(fixture.read_text())

    async def _go() -> int:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                result = await load_lab_fixture(session, data, run_assertions=not no_assert)
            console.print(
                f"[green]replayed[/green]: {result.hosts_loaded} host(s), "
                f"{result.observations_loaded} observation(s), "
                f"{result.assertions_loaded} assertion(s) loaded / {result.assertions_run} run"
            )
            console.print("Run [bold]helper audit[/bold] to see the resulting findings.")
            return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


@discover_app.command(name="proxmox")
def discover_proxmox(
    netbox_sync: bool = typer.Option(
        False, "--netbox-sync", help="Propose discovered VMs into NetBox (cluster must exist)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --netbox-sync, preview writes."),
) -> None:
    """Read a Proxmox cluster's nodes + VMs/LXCs (read-only); optionally push VMs to NetBox."""

    async def _go() -> int:
        adapter = _load_proxmox_adapter()
        try:
            ok, err = await adapter.health_check()
            if not ok:
                console.print(f"[red]proxmox unreachable:[/red] {err}")
                return 1
            status = await adapter.cluster_status()
            vms = await adapter.list_vms()

            cluster_label = status["name"] or "(standalone)"
            console.print(
                f"[cyan]cluster[/cyan] {cluster_label}: {status['node_count']} node(s), "
                f"quorate={status['quorate']}"
            )
            table = Table(title=f"{len(vms)} guest(s)")
            for col in ("vmid", "name", "type", "node", "status"):
                table.add_column(col)
            for vm in sorted(vms, key=lambda v: v.get("vmid") or 0):
                table.add_row(
                    str(vm.get("vmid")),
                    str(vm.get("name")),
                    str(vm.get("type")),
                    str(vm.get("node")),
                    str(vm.get("status")),
                )
            console.print(table)

            if not netbox_sync:
                return 0

            nb = _load_netbox_adapter()
            try:
                real_vms = [v for v in vms if not v.get("template")]
                res = await nb.sync_cluster_vms(cluster_label, real_vms, dry_run=dry_run)
                if not res.found:
                    console.print(f"[yellow]netbox:[/yellow] {res.reason}")
                    return 1
                console.print(
                    f"[green]netbox sync[/green]: {len(res.created)} created, "
                    f"{len(res.updated)} updated, {len(res.unchanged)} unchanged"
                    + (" [dim](dry-run)[/dim]" if dry_run else "")
                )
            finally:
                await nb.aclose()
            return 0
        finally:
            await adapter.aclose()

    raise typer.Exit(code=asyncio.run(_go()))


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
    cidr: str = typer.Argument(..., help="Network range to scan, e.g. 10.0.6.0/24."),
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
