"""`helper view ...` — cross-source synthesized records (Phase 3 query surface).

Where ``helper host show`` reports one table's worth of a single host, ``view``
stitches a record together across every source the harness has ingested:
Services + their internal/external endpoints (UniFi, Cloudflare), the
VirtualMachine/Cluster projection (Proxmox, K8s), and Hosts. The point is to
answer "show me service X" or "show me host X" as one synthesized view rather
than making the operator join it by hand.

- ``view service <name>`` — the service's resolution paths on each side of the
  network, the DNS split-brain (internal vs external IP for one hostname) made
  explicit, plus any VM/Host that carries the same name.
- ``view host <name>`` — the host plus the guests it runs, its cluster
  membership, and the service endpoints that resolve to it.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import or_, select

from homelab_helper.db.enums import FindingStatus, ResolutionScope
from homelab_helper.db.models import (
    Cluster,
    Host,
    ReconciliationFinding,
    Service,
    ServiceEndpoint,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

view_app = typer.Typer(
    name="view",
    help="Cross-source synthesized records for a service or host.",
    no_args_is_help=True,
)

console = Console()


def _database_url() -> str:
    return os.environ.get("HOMELAB_HELPER_DATABASE_URL") or "sqlite+aiosqlite:///./homelab.db"


# ---------------------------------------------------------------------------
# view service
# ---------------------------------------------------------------------------


async def _resolve_service(session: AsyncSession, name: str) -> Service | None:
    """Find a Service by name, or by any endpoint hostname matching ``name``."""
    svc = (await session.execute(select(Service).where(Service.name == name))).scalar_one_or_none()
    if svc is not None:
        return svc
    ep = (
        await session.execute(
            select(ServiceEndpoint).where(ServiceEndpoint.hostname == name.lower())
        )
    ).scalar_one_or_none()
    if ep is not None:
        return await session.get(Service, ep.service_id)
    return None


def _print_endpoints(endpoints: list[ServiceEndpoint]) -> None:
    if not endpoints:
        console.print("\n[dim]no resolution endpoints recorded[/dim]")
        return
    console.print("\n[bold]endpoints[/bold]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("scope", no_wrap=True)
    table.add_column("resolver", no_wrap=True)
    table.add_column("hostname", overflow="fold")
    table.add_column("ip", no_wrap=True)
    table.add_column("tls")
    # internal first, then external.
    order = {ResolutionScope.INTERNAL: 0, ResolutionScope.EXTERNAL: 1}
    for ep in sorted(endpoints, key=lambda e: (order.get(e.scope, 9), e.resolver)):
        table.add_row(
            ep.scope.value,
            ep.resolver,
            ep.hostname,
            ep.ip or "—",
            ep.tls_provider or "—",
        )
    console.print(table)


def _print_split_brain(endpoints: list[ServiceEndpoint]) -> None:
    """Flag a service that resolves to different IPs internally vs externally.

    Compared at the service level, not per exact hostname — the internal and
    external resolvers name the same service with different suffixes
    (``ha.lan`` vs ``ha.example.com``), so pairing on the hostname string would
    miss it. Both sets of endpoints hang off one ``Service`` (see
    ``dns_reconcile.service_key``); differing internal-vs-external IPs are the
    split-brain.
    """
    internal = sorted({ep.ip for ep in endpoints if ep.scope == ResolutionScope.INTERNAL and ep.ip})
    external = sorted({ep.ip for ep in endpoints if ep.scope == ResolutionScope.EXTERNAL and ep.ip})
    if internal and external and internal != external:
        console.print(
            f"\n[yellow]DNS split-brain[/yellow]: "
            f"internal → {', '.join(internal)}, external → {', '.join(external)}"
        )


async def _print_service_crossrefs(
    session: AsyncSession, service: Service, endpoints: list[ServiceEndpoint]
) -> None:
    """Any VM or Host carrying the service's name (or an endpoint IP)."""
    vm = (
        await session.execute(select(VirtualMachine).where(VirtualMachine.name == service.name))
    ).scalar_one_or_none()
    if vm is not None:
        cluster = await session.get(Cluster, vm.cluster_id)
        cname = cluster.name if cluster is not None else "?"
        console.print(
            f"\n[bold]hosted[/bold]: VM [cyan]{vm.name}[/cyan] "
            f"(cluster {cname}, node {vm.node_name or '?'}, status {vm.status or '?'})"
        )

    endpoint_ips = [ep.ip for ep in endpoints if ep.ip]
    host = (
        await session.execute(
            select(Host).where(
                or_(Host.hostname == service.name, Host.primary_ip.in_(endpoint_ips or ["\0"]))
            )
        )
    ).scalar_one_or_none()
    if host is not None:
        console.print(f"[bold]host[/bold]: [cyan]{host.hostname}[/cyan] ({host.primary_ip or '—'})")


@view_app.command(name="service")
def view_service(
    name: str = typer.Argument(..., help="Service name or a resolvable hostname."),
) -> None:
    """Synthesize a service record across DNS endpoints, VMs, and hosts."""

    async def _go() -> int:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                service = await _resolve_service(session, name)
                if service is None:
                    console.print(f"[red]no service or endpoint matches {name!r}[/red]")
                    return 2
                endpoints = (
                    (
                        await session.execute(
                            select(ServiceEndpoint).where(ServiceEndpoint.service_id == service.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                nb = (
                    f"  [dim](netbox service #{service.netbox_service_id})[/dim]"
                    if service.netbox_service_id
                    else ""
                )
                console.print(f"[bold]{service.name}[/bold]{nb}")
                ep_list = list(endpoints)
                _print_endpoints(ep_list)
                _print_split_brain(ep_list)
                await _print_service_crossrefs(session, service, ep_list)
            return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


# ---------------------------------------------------------------------------
# view host
# ---------------------------------------------------------------------------


@view_app.command(name="host")
def view_host(
    hostname: str = typer.Argument(..., help="Hostname (must exist in the harness DB)."),
) -> None:
    """Synthesize a host record: identity + guests it runs + endpoints + findings."""

    async def _go() -> int:
        engine = make_engine(_database_url())
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                host = (
                    await session.execute(select(Host).where(Host.hostname == hostname))
                ).scalar_one_or_none()
                if host is None:
                    console.print(f"[red]no host named {hostname!r}[/red]")
                    return 2

                console.print(
                    f"[bold]{host.hostname}[/bold]  ({host.primary_ip or '—'}, "
                    f"{host.arch.value}, via {host.discovery_source.value})"
                )
                console.print("[dim]run `helper host show` for hardware detail[/dim]")
                await _print_hosted_vms(session, host)
                await _print_host_endpoints(session, host)
                await _print_host_findings(session, host)
            return 0
        finally:
            await engine.dispose()

    raise typer.Exit(code=asyncio.run(_go()))


async def _print_hosted_vms(session: AsyncSession, host: Host) -> None:
    vms = (
        (
            await session.execute(
                select(VirtualMachine)
                .where(VirtualMachine.node_host_id == host.id)
                .order_by(VirtualMachine.name)
            )
        )
        .scalars()
        .all()
    )
    if not vms:
        console.print("\n[dim]no guests recorded on this host[/dim]")
        return
    console.print(f"\n[bold]guests[/bold] ({len(vms)})")
    table = Table(show_header=True, header_style="bold")
    table.add_column("name", overflow="fold")
    table.add_column("kind", no_wrap=True)
    table.add_column("vmid", no_wrap=True)
    table.add_column("status", no_wrap=True)
    for vm in vms:
        table.add_row(vm.name, vm.kind, str(vm.vmid or "—"), vm.status or "—")
    console.print(table)


async def _print_host_endpoints(session: AsyncSession, host: Host) -> None:
    if not host.primary_ip:
        return
    eps = (
        (
            await session.execute(
                select(ServiceEndpoint).where(ServiceEndpoint.ip == host.primary_ip)
            )
        )
        .scalars()
        .all()
    )
    if not eps:
        return
    console.print(f"\n[bold]resolves to this host[/bold] ({host.primary_ip})")
    for ep in eps:
        console.print(f"  [cyan]{ep.hostname}[/cyan] [dim]({ep.scope.value}/{ep.resolver})[/dim]")


async def _print_host_findings(session: AsyncSession, host: Host) -> None:
    rows = (
        (
            await session.execute(
                select(ReconciliationFinding)
                .where(
                    ReconciliationFinding.status.in_(
                        [FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED]
                    )
                )
                .order_by(ReconciliationFinding.last_seen.desc())
            )
        )
        .scalars()
        .all()
    )
    host_id_str = str(host.id)
    affecting = [
        f
        for f in rows
        if any(
            a.get("target_type") == "host" and a.get("target_id") == host_id_str
            for a in (f.affected or [])
        )
    ]
    if not affecting:
        return
    console.print(f"\n[bold]open findings[/bold] ({len(affecting)})")
    for f in affecting:
        console.print(f"  [dim]{f.fingerprint}[/dim] {f.severity.value}: {f.title}")
