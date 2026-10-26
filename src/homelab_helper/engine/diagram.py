"""Diagram of the inventory — what runs where, drawn from the harness DB (Phase 12.1).

A read over rows discovery already persisted; no source is contacted. The
picture is: hosts grouped by site (the operator's network-topology file when
``HOMELAB_HELPER_NETWORK_TOPOLOGY`` names one, else the /24 of each primary
IP), the clusters those hosts are nodes of, the guests each node runs, the
services whose endpoints resolve to a host (internal and external resolvers
drawn differently), and the open findings on each host.

``build_model`` turns the rows into a small graph; ``render_mermaid``,
``render_html`` and ``to_dict`` turn the graph into text. A lab that has not
been discovered draws an empty page rather than failing.
"""

from __future__ import annotations

import html
import ipaddress
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import FindingStatus, ResolutionScope
from homelab_helper.db.models import (
    Cluster,
    Host,
    ReconciliationFinding,
    Service,
    ServiceEndpoint,
    VirtualMachine,
)
from homelab_helper.engine.cluster_nodes import cluster_nodes
from homelab_helper.engine.network_path import Topology, load_topology
from homelab_helper.engine.retire import retired_host_ids

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

UNPLACED = "unplaced"
"""The site of a host with no topology entry and no usable primary IP."""

MAX_GUESTS_LISTED = 6
"""Guest names shown inside a node before the rest collapse to ``+N more``."""


@dataclass(frozen=True)
class DiagramNode:
    id: str
    kind: str  # "host" | "cluster" | "service"
    label: str
    lines: tuple[str, ...] = ()
    site: str | None = None


@dataclass(frozen=True)
class DiagramEdge:
    src: str
    dst: str
    label: str
    style: str = "solid"  # "solid" | "dashed"


@dataclass
class DiagramModel:
    sites: dict[str, list[str]] = field(default_factory=dict)
    """site → host node ids, in display order."""
    nodes: dict[str, DiagramNode] = field(default_factory=dict)
    edges: list[DiagramEdge] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def _slug(prefix: str, name: str) -> str:
    return prefix + "_" + re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()


def _site_from_ip(ip: str | None) -> str:
    if not ip:
        return UNPLACED
    try:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    except ValueError:
        return UNPLACED
    return str(net)


_LEASE_SUFFIX = re.compile(r"(?:-[0-9a-f]{2}){2}$")


def _is_noise(service_name: str, host: Host) -> bool:
    """A DNS record that only restates the host: its own name, or a DHCP lease name
    (UniFi appends two MAC octets, ``bmax1-b4-be``). Drawing those adds no information."""
    short = host.hostname.split(".")[0].lower()
    name = service_name.lower()
    return name in {host.hostname.lower(), short} or bool(_LEASE_SUFFIX.search(name))


def _affects(finding: ReconciliationFinding, host_id: str) -> bool:
    for a in finding.affected or []:
        if a.get("target_type") == "host" and a.get("target_id") == host_id:
            return True
        if a.get("type") == "host" and a.get("id") == host_id:
            return True
    return False


@dataclass
class _Rows:
    """What one build reads, before any node is made."""

    hosts: list[Host]
    host_site: dict[Any, str]
    node_id_of: dict[Any, str]
    findings_on: dict[Any, int]
    guests_on: dict[Any, list[VirtualMachine]]


async def _read_rows(session: AsyncSession, site: str | None, topology: Topology | None) -> _Rows:
    hosts = list((await session.execute(select(Host).order_by(Host.hostname))).scalars().all())
    retired = await retired_host_ids(session)
    hosts = [h for h in hosts if h.id not in retired]
    host_site = {
        h.id: (topology.site_of(h.hostname) if topology else None) or _site_from_ip(h.primary_ip)
        for h in hosts
    }
    if site is not None:
        hosts = [h for h in hosts if host_site[h.id] == site]
    host_ids = {h.id for h in hosts}
    open_findings = (
        (
            await session.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.status == FindingStatus.OPEN
                )
            )
        )
        .scalars()
        .all()
    )
    guests_on: dict[Any, list[VirtualMachine]] = {}
    for vm in (await session.execute(select(VirtualMachine))).scalars().all():
        if vm.node_host_id in host_ids and not vm.template:
            guests_on.setdefault(vm.node_host_id, []).append(vm)
    return _Rows(
        hosts=hosts,
        host_site=host_site,
        node_id_of={h.id: _slug("h", h.hostname) for h in hosts},
        findings_on={h.id: sum(1 for f in open_findings if _affects(f, str(h.id))) for h in hosts},
        guests_on=guests_on,
    )


def _host_lines(h: Host, rows: _Rows, include_guests: bool) -> tuple[str, ...]:
    lines: list[str] = []
    if h.primary_ip:
        lines.append(h.primary_ip)
    guests = sorted(rows.guests_on.get(h.id, []), key=lambda v: v.name)
    if include_guests and guests:
        names = [v.name for v in guests[:MAX_GUESTS_LISTED]]
        extra = len(guests) - len(names)
        lines.append("guests: " + ", ".join(names) + (f" +{extra} more" if extra else ""))
    if n := rows.findings_on[h.id]:
        lines.append(f"{n} open finding" + ("s" if n != 1 else ""))
    return tuple(lines)


async def _add_clusters(session: AsyncSession, model: DiagramModel, rows: _Rows) -> None:
    clusters = {c.id: c for c in (await session.execute(select(Cluster))).scalars().all()}
    membership = await cluster_nodes(session, rows.hosts)
    for cluster_id, members in sorted(
        membership.by_cluster().items(),
        key=lambda kv: clusters[kv[0]].name if kv[0] in clusters else "",
    ):
        cluster = clusters.get(cluster_id)
        if cluster is None:
            continue
        cid = _slug("c", cluster.name)
        count = cluster.node_count if cluster.node_count is not None else len(members)
        model.nodes[cid] = DiagramNode(
            id=cid, kind="cluster", label=cluster.name, lines=(f"{cluster.kind}, {count} nodes",)
        )
        for host_id in sorted(members, key=lambda i: rows.node_id_of.get(i, "")):
            if host_id in rows.node_id_of:
                model.edges.append(DiagramEdge(rows.node_id_of[host_id], cid, "node of"))


async def _add_services(session: AsyncSession, model: DiagramModel, rows: _Rows) -> int:
    by_ip = {h.primary_ip: h for h in rows.hosts if h.primary_ip}
    result = await session.execute(
        select(ServiceEndpoint, Service)
        .join(Service, ServiceEndpoint.service_id == Service.id)
        .order_by(Service.name, ServiceEndpoint.hostname)
    )
    drawn = 0
    seen: set[tuple[str, str, str]] = set()
    for ep, svc in result.all():
        host = by_ip.get(ep.ip or "")
        if host is None or _is_noise(svc.name, host):
            continue
        sid = _slug("s", svc.name)
        if sid not in model.nodes:
            model.nodes[sid] = DiagramNode(id=sid, kind="service", label=svc.name)
            drawn += 1
        key = (sid, rows.node_id_of[host.id], ep.resolver)
        if key in seen:
            continue
        seen.add(key)
        external = ep.scope == ResolutionScope.EXTERNAL
        model.edges.append(
            DiagramEdge(
                sid,
                rows.node_id_of[host.id],
                f"{'external' if external else 'internal'} via {ep.resolver}",
                style="dashed" if external else "solid",
            )
        )
    return drawn


async def build_model(
    session: AsyncSession,
    *,
    site: str | None = None,
    include_services: bool = True,
    include_guests: bool = True,
    topology: Topology | None = None,
    use_topology_file: bool = True,
) -> DiagramModel:
    """The inventory as a graph. ``site`` keeps one site's hosts (and the clusters and
    services that touch them); ``topology`` overrides the file for tests."""
    if topology is None and use_topology_file:
        topology = load_topology()
    model = DiagramModel()
    rows = await _read_rows(session, site, topology)
    for h in rows.hosts:
        nid = rows.node_id_of[h.id]
        model.nodes[nid] = DiagramNode(
            id=nid,
            kind="host",
            label=h.hostname,
            lines=_host_lines(h, rows, include_guests),
            site=rows.host_site[h.id],
        )
        model.sites.setdefault(rows.host_site[h.id], []).append(nid)
    await _add_clusters(session, model, rows)
    services_drawn = await _add_services(session, model, rows) if include_services else 0
    model.stats = {
        "sites": len(model.sites),
        "hosts": len(rows.hosts),
        "clusters": sum(1 for n in model.nodes.values() if n.kind == "cluster"),
        "guests": sum(len(v) for v in rows.guests_on.values()),
        "services": services_drawn,
        "open_findings": sum(rows.findings_on.values()),
    }
    return model


def _mermaid_text(label: str, lines: tuple[str, ...]) -> str:
    parts = [label, *lines]
    return "<br/>".join(html.escape(p, quote=True) for p in parts)


def render_mermaid(model: DiagramModel, *, direction: str = "LR") -> str:
    """A Mermaid flowchart: one subgraph per site, hosts inside, clusters and services
    outside with labelled edges. Renders in GitHub, mkdocs-material and the artifact viewer."""
    out = [f"flowchart {direction}"]
    for site, ids in model.sites.items():
        out.append(f'  subgraph {_slug("site", site)}["{html.escape(site, quote=True)}"]')
        out.append("    direction TB")
        for nid in ids:
            n = model.nodes[nid]
            out.append(f'    {nid}["{_mermaid_text(n.label, n.lines)}"]')
        out.append("  end")
    for n in model.nodes.values():
        if n.kind == "cluster":
            out.append(f'  {n.id}[("{_mermaid_text(n.label, n.lines)}")]')
        elif n.kind == "service":
            out.append(f'  {n.id}(["{_mermaid_text(n.label, n.lines)}"])')
    for e in model.edges:
        arrow = "-.->" if e.style == "dashed" else "-->"
        out.append(f'  {e.src} {arrow}|"{html.escape(e.label, quote=True)}"| {e.dst}')
    if not model.nodes:
        out.append('  empty["nothing discovered yet — run helper discover"]')
    return "\n".join(out) + "\n"


MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"


def render_html(model: DiagramModel, *, title: str = "Homelab") -> str:
    """A standalone page that draws the Mermaid source with the pinned mermaid.js; the
    source is also in a ``<details>`` so it can be copied into docs."""
    src = render_mermaid(model)
    stats = " · ".join(f"{v} {k.replace('_', ' ')}" for k, v in model.stats.items())
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<style>body{font-family:system-ui,sans-serif;margin:24px;color:#1f2328;background:#fff}"
        "h1{font-size:1.4rem;margin:0 0 4px}p{color:#5d6670;margin:0 0 16px}"
        "pre.mermaid{overflow-x:auto}details{margin-top:24px}pre.src{font-size:12px;overflow-x:auto}"
        "@media (prefers-color-scheme: dark){body{color:#e8e6e1;background:#15181c}p{color:#9aa3ad}}</style>"
        "</head><body>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(stats)}</p>"
        f'<pre class="mermaid">{html.escape(src)}</pre>'
        f'<details><summary>Mermaid source</summary><pre class="src">{html.escape(src)}</pre></details>'
        f'<script src="{MERMAID_CDN}"></script>'
        "<script>mermaid.initialize({startOnLoad:true,theme:matchMedia('(prefers-color-scheme: dark)').matches?'dark':'default'});</script>"
        "</body></html>\n"
    )


def to_dict(model: DiagramModel) -> dict[str, Any]:
    return {
        "sites": {site: list(ids) for site, ids in model.sites.items()},
        "nodes": [
            {"id": n.id, "kind": n.kind, "label": n.label, "lines": list(n.lines), "site": n.site}
            for n in model.nodes.values()
        ],
        "edges": [
            {"from": e.src, "to": e.dst, "label": e.label, "style": e.style} for e in model.edges
        ],
        "stats": dict(model.stats),
    }


__all__ = [
    "MAX_GUESTS_LISTED",
    "MERMAID_CDN",
    "UNPLACED",
    "DiagramEdge",
    "DiagramModel",
    "DiagramNode",
    "build_model",
    "render_html",
    "render_mermaid",
    "to_dict",
]
