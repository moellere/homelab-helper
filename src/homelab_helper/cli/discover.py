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
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from homelab_helper.adapters.argocd import ArgoCDAdapter, application_is_drifted
from homelab_helper.adapters.cloudflare import CloudflareAdapter
from homelab_helper.adapters.homeassistant import (
    HomeAssistantAdapter,
    integrations_of_interest,
    summarize_states,
)
from homelab_helper.adapters.kubernetes import K8sAdapter
from homelab_helper.adapters.netbox import NetBoxAdapter, NetBoxConfig
from homelab_helper.adapters.openmediavault import OpenMediaVaultAdapter
from homelab_helper.adapters.proxmox import ProxmoxAdapter
from homelab_helper.adapters.unifi import UniFiAdapter, UniFiConfig
from homelab_helper.cli._probe_sync import sync_probes_sync
from homelab_helper.config import database_url as _database_url
from homelab_helper.db.models import Host, Observation
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.dns_reconcile import (
    reconcile_external_endpoints,
    reconcile_internal_endpoints,
)
from homelab_helper.engine.hass_import import import_home_assistant
from homelab_helper.engine.host_probe import (
    HostProbeRequest,
    ProbeOutcome,
    UnknownProbeError,
    probe_host,
    select_host_probes,
)
from homelab_helper.engine.k8s_import import discover_k8s_nodes
from homelab_helper.engine.lab_replay import load_lab_fixture, parse_lab_fixture
from homelab_helper.engine.reconciler import Reconciler, ReconcileResult
from homelab_helper.engine.runner import ProbeRunner
from homelab_helper.engine.scan_import import (
    ProbeStrategy,
    ScanImporter,
    classify,
    parse_scan_csv,
)
from homelab_helper.engine.stray_config import reconcile_stray_config
from homelab_helper.engine.stray_export import (
    StrayExport,
    detect_stray_exports,
    reconcile_stray_exports,
)
from homelab_helper.engine.talos_probe import TalosProbeRequest, probe_talos, select_talos_probes
from homelab_helper.engine.virt_reconcile import reconcile_proxmox_cluster
from homelab_helper.probes.base import AdapterRegistry, ProbeTarget
from homelab_helper.probes.network.fingerprint import NetworkFingerprintProbe
from homelab_helper.probes.network.subnet_scan import NetworkSubnetScanProbe

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


def _resolve_probes(filter_names: list[str] | None) -> list[type[Probe]]:
    """Pick the probes to run — by name filter, or all host-kind probes."""
    try:
        return select_host_probes(filter_names)
    except UnknownProbeError as exc:
        console.print(f"[red]unknown probe:[/red] {exc.args[0]}")
        sys.exit(2)


def _load_proxmox_adapter() -> ProxmoxAdapter:
    """Factory (monkeypatched in tests) — builds a Proxmox adapter from env."""
    return ProxmoxAdapter.from_env()


def _load_netbox_adapter() -> NetBoxAdapter:
    """Factory (monkeypatched in tests) — builds a NetBox adapter from env."""
    return NetBoxAdapter(NetBoxConfig.from_env())


def _load_k8s_adapter() -> K8sAdapter:
    """Factory (monkeypatched in tests) — builds a Kubernetes adapter from env."""
    return K8sAdapter.from_env()


def _load_unifi_adapter() -> UniFiAdapter:
    """Factory (monkeypatched in tests) — builds a UniFi adapter from env."""
    return UniFiAdapter.from_env()


def _load_unifi_adapters() -> list[UniFiAdapter]:
    """Every configured controller.

    Without ``HOMELAB_HELPER_UNIFI_CONTROLLERS`` this defers to the single-
    controller factory above, so one gateway stays the simple path.
    """
    raw = os.environ.get("HOMELAB_HELPER_UNIFI_CONTROLLERS") or ""
    if not [n for n in raw.split(",") if n.strip()]:
        return [_load_unifi_adapter()]
    return [UniFiAdapter(cfg) for cfg in UniFiConfig.all_from_env()]


def _load_cloudflare_adapter() -> CloudflareAdapter:
    """Factory (monkeypatched in tests) — builds a Cloudflare adapter from env."""
    return CloudflareAdapter.from_env()


def _load_argocd_adapter() -> ArgoCDAdapter:
    """Factory (monkeypatched in tests) — builds an Argo CD adapter from env."""
    return ArgoCDAdapter.from_env()


def _load_omv_adapter() -> OpenMediaVaultAdapter:
    """Factory (monkeypatched in tests) — builds an OpenMediaVault adapter from env."""
    return OpenMediaVaultAdapter.from_env()


def _load_hass_adapter() -> HomeAssistantAdapter:
    """Factory (monkeypatched in tests) — builds a Home Assistant adapter from env."""
    return HomeAssistantAdapter.from_env()


def _warn_superseded(resolvers: list[str]) -> None:
    """A slice whose rows another resolver now duplicates is a renamed controller's orphan."""
    for other in resolvers:
        console.print(
            f"[yellow]superseded slice:[/yellow] resolver {other!r} holds the same endpoints "
            f"this sync just wrote — a renamed controller? "
            f"`helper service retire-resolver {other}` removes the orphan."
        )


async def _reconcile_and_report(session: AsyncSession, host_id: uuid.UUID) -> None:
    """Run the reconciler for a host and print the deltas."""
    _report_reconcile(await Reconciler().reconcile_host(session, host_id))


def _report_reconcile(result: ReconcileResult) -> None:
    """Print reconciler deltas. Split out so callers that already reconciled
    (the shared host-probe path) render identically."""
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

    def _report(outcome: ProbeOutcome) -> None:
        console.print(f"[cyan]→ {outcome.probe}[/cyan] v{outcome.version}")
        if outcome.success:
            console.print(f"  [green]ok[/green] - {outcome.observations} observation(s)")
        else:
            console.print(f"  [red]failed[/red] - {outcome.error}")

    async def _go() -> int:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                result = await probe_host(
                    session,
                    HostProbeRequest(
                        name=name,
                        ssh_user=ssh_user,
                        ssh_key_path=str(ssh_key) if ssh_key else None,
                        ssh_password=ssh_password,
                        primary_ip=primary_ip,
                        ssh_port=ssh_port,
                    ),
                    probe_classes=probe_classes,
                    on_probe=_report,
                )
                if result.session_error:
                    console.print(
                        f"[red]could not establish SSH session:[/red] {result.session_error}"
                    )
                if result.reconcile is not None:
                    _report_reconcile(result.reconcile)

            console.print(
                f"\nrun complete: [bold]{result.observations}[/bold] observation(s), "
                f"[bold]{result.failures}[/bold] failure(s)"
            )
            return 0 if result.failures == 0 else 1
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

    try:
        probe_classes = select_talos_probes(probe_names)
    except UnknownProbeError as exc:
        console.print(f"[red]unknown probe:[/red] {exc.args[0]}")
        raise typer.Exit(code=2) from exc
    if not probe_classes:
        console.print("[red]error:[/red] no talos probes matched the filter.")
        raise typer.Exit(code=2)

    def _on_probe(outcome: ProbeOutcome) -> None:
        console.print(f"[cyan]→ {outcome.probe}[/cyan] v{outcome.version}")
        if outcome.success:
            console.print(f"  [green]ok[/green] - {outcome.observations} observation(s)")
        else:
            console.print(f"  [red]failed[/red] - {outcome.error}")

    async def _go() -> int:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                result = await probe_talos(
                    session,
                    TalosProbeRequest(
                        name=name,
                        node=node,
                        talosconfig=str(talosconfig) if talosconfig else None,
                        probe_names=tuple(probe_names) if probe_names else None,
                    ),
                    probe_classes=probe_classes,
                    on_probe=_on_probe,
                )
                if result.session_error:
                    console.print(f"[red]{result.session_error}[/red]")
                if result.reconcile is not None:
                    _report_reconcile(result.reconcile)
            console.print(
                f"\nrun complete: [bold]{result.observations}[/bold] observation(s), "
                f"[bold]{result.failures}[/bold] failure(s)"
            )
            return 0 if result.failures == 0 else 1
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


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
    persist: bool = typer.Option(
        False, "--persist", help="Write the cluster + VMs into the harness DB."
    ),
    netbox_sync: bool = typer.Option(
        False, "--netbox-sync", help="Propose discovered VMs into NetBox (cluster must exist)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --netbox-sync, preview writes."),
) -> None:
    """Read a Proxmox cluster's nodes + VMs/LXCs (read-only); persist and/or push to NetBox."""

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

            if persist:
                engine = make_engine(_database_url())
                try:
                    sm = make_sessionmaker(engine)
                    async with session_scope(sm) as session:
                        vr = await reconcile_proxmox_cluster(
                            session, status, vms, when=datetime.now(UTC)
                        )
                    console.print(
                        f"[green]persisted[/green]: cluster {vr.cluster_name} "
                        f"({'new' if vr.cluster_created else 'updated'}), "
                        f"VMs {len(vr.vms_created)} created / {len(vr.vms_updated)} updated / "
                        f"{len(vr.vms_unchanged)} unchanged"
                    )
                finally:
                    await engine.dispose()

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


@discover_app.command(name="k8s")
def discover_k8s() -> None:
    """Discover Kubernetes nodes as Host observations (INFERRED; kernel facts win)."""

    async def _go() -> int:
        adapter = _load_k8s_adapter()
        ok, err = await adapter.health_check()
        if not ok:
            console.print(f"[red]kubernetes unreachable:[/red] {err}")
            return 1
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                result = await discover_k8s_nodes(session, adapter, when=datetime.now(UTC))
            console.print(
                f"[green]k8s[/green]: {result.nodes_seen} node(s) — "
                f"{len(result.hosts_matched)} matched, {len(result.hosts_created)} new host(s); "
                "facts recorded INFERRED (kernel-verified data unchanged)."
            )
            return 0
        finally:
            await engine.dispose()

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


@discover_app.command(name="unifi")
def discover_unifi(
    persist: bool = typer.Option(
        False,
        "--persist",
        help="Reconcile internal ServiceEndpoints from DNS + stray-config from networks/clients.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --persist, preview + roll back."),
) -> None:
    """Read a UniFi controller's DNS records, clients, and networks (read-only)."""

    async def _read_one(adapter: UniFiAdapter) -> dict[str, Any] | None:
        try:
            ok, err = await adapter.health_check()
            if not ok:
                console.print(
                    f"[red]unifi[/red] [bold]{adapter.config.name}[/bold] unreachable: {err}"
                )
                return None
            return {
                "dns": await adapter.list_dns_records(),
                "clients": await adapter.list_clients(),
                "networks": await adapter.list_networks(),
            }
        finally:
            await adapter.aclose()

    async def _go() -> int:
        adapters = _load_unifi_adapters()
        reachable: list[tuple[UniFiAdapter, dict[str, Any]]] = []
        for adapter in adapters:
            data = await _read_one(adapter)
            if data is not None:
                reachable.append((adapter, data))
        if not reachable:
            return 1

        for adapter, data in reachable:
            cfg = adapter.config
            console.print(
                f"[cyan]unifi[/cyan] [bold]{cfg.name}[/bold] ({cfg.url}): "
                f"{len(data['dns'])} DNS record(s), {len(data['clients'])} client(s), "
                f"{len(data['networks'])} network(s)"
            )
            table = Table(title=f"internal DNS - {cfg.name}")
            for col in ("hostname", "value", "type", "enabled"):
                table.add_column(col)
            for r in sorted(data["dns"], key=lambda d: str(d.get("hostname"))):
                table.add_row(
                    str(r.get("hostname")),
                    str(r.get("value")),
                    str(r.get("record_type")),
                    str(r.get("enabled")),
                )
            console.print(table)

        if not persist:
            return 0

        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                now = datetime.now(UTC)
                for adapter, data in reachable:
                    cfg = adapter.config
                    # Each controller owns its own (scope, resolver) slice, so
                    # one gateway's sync never reaps another's endpoints.
                    result = await reconcile_internal_endpoints(
                        session, data["dns"], resolver=cfg.resolver, when=now
                    )
                    stray = await reconcile_stray_config(
                        session, data["networks"], data["clients"], when=now
                    )
                    console.print(
                        f"[green]endpoints[/green] ({cfg.resolver}/internal): "
                        f"{len(result.created)} created, {len(result.updated)} updated, "
                        f"{len(result.unchanged)} unchanged, {len(result.removed)} removed"
                        + (f", {len(result.moved)} re-aliased" if result.moved else "")
                    )
                    _warn_superseded(result.superseded_resolvers)
                    console.print(
                        f"[green]stray-config[/green] ({cfg.name}): {len(stray.opened)} opened, "
                        f"{len(stray.reopened)} reopened, {len(stray.updated)} re-seen, "
                        f"{len(stray.resolved)} resolved"
                        + (
                            f" ({stray.skipped_no_subnet} network(s) skipped)"
                            if stray.skipped_no_subnet
                            else ""
                        )
                    )
                if dry_run:
                    await session.rollback()
                    console.print("[dim](dry-run, rolled back)[/dim]")
        finally:
            await engine.dispose()
        return 0

    raise typer.Exit(code=asyncio.run(_go()))


@discover_app.command(name="cloudflare")
def discover_cloudflare(
    persist: bool = typer.Option(
        False, "--persist", help="Reconcile external ServiceEndpoints from Cloudflare DNS."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --persist, preview + roll back."),
) -> None:
    """Read a Cloudflare zone's public DNS records (read-only) — the external half."""

    async def _go() -> int:
        adapter = _load_cloudflare_adapter()
        try:
            ok, err = await adapter.health_check()
            if not ok:
                console.print(f"[red]cloudflare unreachable:[/red] {err}")
                return 1
            dns = await adapter.list_dns_records()
        finally:
            await adapter.aclose()

        console.print(f"[cyan]cloudflare[/cyan]: {len(dns)} DNS record(s)")
        table = Table(title="external DNS")
        for col in ("hostname", "value", "type", "proxied"):
            table.add_column(col)
        for r in sorted(dns, key=lambda d: str(d.get("hostname"))):
            table.add_row(
                str(r.get("hostname")),
                str(r.get("value")),
                str(r.get("record_type")),
                str(r.get("proxied")),
            )
        console.print(table)

        if not persist:
            return 0

        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                result = await reconcile_external_endpoints(session, dns, when=datetime.now(UTC))
                if dry_run:
                    await session.rollback()
            console.print(
                f"[green]endpoints[/green] (cloudflare/external): "
                f"{len(result.created)} created, {len(result.updated)} updated, "
                f"{len(result.unchanged)} unchanged, {len(result.removed)} removed"
                + (" [dim](dry-run, rolled back)[/dim]" if dry_run else "")
            )
            _warn_superseded(result.superseded_resolvers)
        finally:
            await engine.dispose()
        return 0

    raise typer.Exit(code=asyncio.run(_go()))


@discover_app.command(name="argocd")
def discover_argocd() -> None:
    """Read Argo CD Applications (read-only): git desired-state + sync/health drift."""

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
            f"[cyan]argocd[/cyan]: {len(apps)} application(s), "
            f"[yellow]{len(drifted)}[/yellow] drifted"
        )
        table = Table(title="applications")
        for col in ("name", "target", "sync", "health"):
            table.add_column(col)
        for a in sorted(apps, key=lambda x: str(x.get("name"))):
            sync = str(a.get("sync_status") or "—")
            health = str(a.get("health_status") or "—")
            drift = application_is_drifted(a)
            target = f"{a.get('repo_url') or '?'}@{a.get('target_revision') or 'HEAD'}"
            table.add_row(
                str(a.get("name")),
                target,
                f"[yellow]{sync}[/yellow]" if drift and sync != "Synced" else sync,
                f"[yellow]{health}[/yellow]" if drift and health != "Healthy" else health,
            )
        console.print(table)

        for a in drifted:
            oos = a.get("out_of_sync_resources") or []
            if oos:
                names = ", ".join(
                    f"{r.get('kind')}/{r.get('name')}({r.get('status')})" for r in oos
                )
                console.print(f"  [dim]{a.get('name')}[/dim]: {names}")
        return 0

    raise typer.Exit(code=asyncio.run(_go()))


_BYTES_PER_UNIT = 1024


def _fmt_bytes(n: int | None) -> str:
    if not n:
        return "—"
    val = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if val < _BYTES_PER_UNIT or unit == "PiB":
            return f"{val:.1f} {unit}"
        val /= _BYTES_PER_UNIT
    return f"{n} B"


def _print_omv(
    filesystems: list[dict[str, Any]],
    disks: list[dict[str, Any]],
    shares: list[dict[str, Any]],
    services: list[dict[str, Any]],
    nfs: list[dict[str, Any]],
    smb: list[dict[str, Any]],
) -> None:
    console.print(
        f"[cyan]openmediavault[/cyan]: {len(filesystems)} filesystem(s), "
        f"{len(disks)} disk(s), {len(shares)} share(s), {len(services)} service(s), "
        f"{len(nfs)} NFS + {len(smb)} SMB export(s)"
    )

    fs_table = Table(title="filesystems")
    for col in ("device", "type", "mountpoint", "size", "used %"):
        fs_table.add_column(col, overflow="fold")
    for fs in sorted(filesystems, key=lambda f: str(f.get("device"))):
        fs_table.add_row(
            str(fs.get("device")),
            str(fs.get("type") or "—"),
            str(fs.get("mountpoint") or "—"),
            _fmt_bytes(fs.get("size_bytes")),
            f"{fs.get('used_percent')}%" if fs.get("used_percent") is not None else "—",
        )
    console.print(fs_table)

    share_table = Table(title="shared folders")
    for col in ("name", "path", "comment"):
        share_table.add_column(col, overflow="fold")
    for sh in sorted(shares, key=lambda s: str(s.get("name"))):
        share_table.add_row(
            str(sh.get("name")), str(sh.get("path") or "—"), str(sh.get("comment") or "—")
        )
    console.print(share_table)

    if nfs or smb:
        exp_table = Table(title="exports")
        for col in ("protocol", "share", "detail"):
            exp_table.add_column(col, overflow="fold")
        for e in nfs:
            exp_table.add_row(
                "nfs", str(e.get("shared_folder") or "—"), str(e.get("client") or "—")
            )
        for e in smb:
            detail = "ro" if e.get("readonly") else "rw"
            # OMV leaves `name` empty on SMB rows — the shared folder is the
            # identifying label, same as the NFS side.
            exp_table.add_row("smb", str(e.get("name") or e.get("shared_folder") or "—"), detail)
        console.print(exp_table)

    running = [s for s in services if s.get("running")]
    console.print(f"[bold]services[/bold]: {len(running)}/{len(services)} running")


def _print_stray_exports(strays: list[StrayExport], skipped: int) -> None:
    if strays:
        stray_table = Table(title="stray exports (nothing behind them)")
        for col in ("protocol", "export", "reason"):
            stray_table.add_column(col, overflow="fold")
        for hit in strays:
            stray_table.add_row(hit.protocol, hit.label, hit.detail)
        console.print(stray_table)
    else:
        console.print("[green]no stray exports[/green]")
    if skipped:
        console.print(
            f"[dim]{skipped} shared folder(s) report no backing device — not judged[/dim]"
        )


@discover_app.command(name="omv")
def discover_omv(
    persist: bool = typer.Option(
        False,
        "--persist",
        help="Record stray exports (nothing behind them) as STRAY_CONFIG findings.",
    ),
) -> None:
    """Read an OpenMediaVault NAS (read-only): filesystems, disks, shares, services, stray exports."""

    async def _go() -> int:
        adapter = _load_omv_adapter()
        try:
            ok, err = await adapter.health_check()
            if not ok:
                console.print(f"[red]openmediavault unreachable:[/red] {err}")
                return 1
            filesystems = await adapter.list_filesystems()
            disks = await adapter.list_smart_devices()
            shares = await adapter.list_shared_folders()
            services = await adapter.list_services()
            nfs = await adapter.list_nfs_exports()
            smb = await adapter.list_smb_shares()
        finally:
            await adapter.aclose()

        _print_omv(filesystems, disks, shares, services, nfs, smb)
        strays, skipped = detect_stray_exports(filesystems, shares, [*nfs, *smb])
        _print_stray_exports(strays, skipped)

        if not persist:
            return 0
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                result = await reconcile_stray_exports(
                    session, filesystems, shares, [*nfs, *smb], when=datetime.now(UTC)
                )
        finally:
            await engine.dispose()
        console.print(
            f"[green]stray-export[/green]: {len(result.opened)} opened, "
            f"{len(result.reopened)} reopened, {len(result.updated)} re-seen, "
            f"{len(result.resolved)} resolved"
        )
        return 0

    raise typer.Exit(code=asyncio.run(_go()))


@discover_app.command(name="hass")
def discover_hass(
    persist: bool = typer.Option(
        False,
        "--persist",
        help="Upsert the home-assistant Service (+ endpoint) into the harness DB.",
    ),
) -> None:
    """Read a Home Assistant instance (read-only): version, integrations, entities."""

    async def _go() -> int:
        adapter = _load_hass_adapter()
        try:
            ok, err = await adapter.health_check()
            if not ok:
                console.print(f"[red]home assistant unreachable:[/red] {err}")
                return 1
            config = await adapter.get_config()
            states = await adapter.list_states()
            service_domains = await adapter.list_service_domains()
        finally:
            await adapter.aclose()

        by_domain = summarize_states(states)
        integrations = integrations_of_interest(config["components"])
        console.print(
            f"[cyan]home assistant[/cyan] {config.get('version') or '?'} "
            f"at {config.get('location_name') or '?'}: {len(states)} entit(ies) across "
            f"{len(by_domain)} domain(s), {len(config['components'])} component(s) loaded"
        )
        if integrations:
            console.print(f"[bold]integrations of interest[/bold]: {', '.join(integrations)}")

        table = Table(title="entities by domain")
        for col in ("domain", "entities"):
            table.add_column(col, no_wrap=True)
        for domain, count in by_domain.items():
            table.add_row(domain, str(count))
        console.print(table)
        console.print(f"[dim]service domains: {len(service_domains)}[/dim]")

        if not persist:
            return 0
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                result = await import_home_assistant(
                    session,
                    config=config,
                    states=states,
                    service_domains=service_domains,
                    url=adapter.config.url,
                    when=datetime.now(UTC),
                )
        finally:
            await engine.dispose()
        verb = "created" if result.created else "updated"
        console.print(
            f"[green]persisted[/green]: service {result.service!r} {verb}"
            + (f", endpoint {result.endpoint_hostname}" if result.endpoint_hostname else "")
        )
        return 0

    raise typer.Exit(code=asyncio.run(_go()))
