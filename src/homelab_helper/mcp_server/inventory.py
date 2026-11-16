"""Inventory and findings tools: hosts, services, the audit roll-up, the diagram, finding transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from homelab_helper.config import config_status as _config_status
from homelab_helper.config import database_url
from homelab_helper.db.enums import (
    FindingKind,
    FindingSeverity,
    FindingStatus,
    ResolutionScope,
)
from homelab_helper.db.models import (
    Cluster,
    Host,
    ReconciliationFinding,
    Service,
    ServiceEndpoint,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine import diagram as diagram_engine
from homelab_helper.engine.retire import is_retired, retired_host_ids
from homelab_helper.engine.retire import retire_host as _retire_host

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from homelab_helper.mcp_server._app import (
    _endpoint_dict,
    _finding_dict,
    _iso,
    _open_findings,
    _split_brain,
    server,
)


@server.tool()
async def list_hosts() -> list[dict[str, Any]]:
    """List every host the harness knows: hostname, IP, arch, discovery source."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            hosts = (await session.execute(select(Host).order_by(Host.hostname))).scalars().all()
            retired = await retired_host_ids(session)
            return [
                {
                    "hostname": h.hostname,
                    "primary_ip": h.primary_ip,
                    "arch": h.arch.value,
                    "discovery_source": h.discovery_source.value,
                    "last_verified": _iso(h.last_verified),
                    "retired": h.id in retired,
                }
                for h in hosts
            ]
    finally:
        await engine.dispose()


@server.tool()
async def get_host(hostname: str) -> dict[str, Any]:
    """Full record for one host: identity, capabilities, guests it runs, service
    endpoints resolving to it, and its open findings."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            host = (
                await session.execute(select(Host).where(Host.hostname == hostname))
            ).scalar_one_or_none()
            if host is None:
                return {"error": f"no host named {hostname!r}"}
            vms = (
                (
                    await session.execute(
                        select(VirtualMachine).where(VirtualMachine.node_host_id == host.id)
                    )
                )
                .scalars()
                .all()
            )
            eps: list[ServiceEndpoint] = []
            if host.primary_ip:
                eps = list(
                    (
                        await session.execute(
                            select(ServiceEndpoint).where(ServiceEndpoint.ip == host.primary_ip)
                        )
                    )
                    .scalars()
                    .all()
                )
            host_id = str(host.id)
            retired = await is_retired(session, host.id)
            findings = [
                _finding_dict(f)
                for f in await _open_findings(session)
                if any(
                    a.get("target_type") == "host" and a.get("target_id") == host_id
                    for a in (f.affected or [])
                )
            ]
            return {
                "hostname": host.hostname,
                "retired": retired,
                "primary_ip": host.primary_ip,
                "arch": host.arch.value,
                "discovery_source": host.discovery_source.value,
                "last_verified": _iso(host.last_verified),
                "capabilities": host.capabilities or {},
                "guests": [
                    {"name": v.name, "kind": v.kind, "vmid": v.vmid, "status": v.status}
                    for v in vms
                ],
                "endpoints_resolving_here": [_endpoint_dict(e) for e in eps],
                "open_findings": findings,
            }
    finally:
        await engine.dispose()


@server.tool()
async def list_findings(
    status: str | None = None,
    kind: str | None = None,
    severity: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List findings, most recently seen first, optionally filtered by status
    (open/acknowledged/resolved/suppressed), kind (e.g. stray-config,
    drift-candidate), or severity. Returns at most `limit` (default 200)."""
    try:
        wanted = [
            column == enum(value)
            for column, enum, value in (
                (ReconciliationFinding.status, FindingStatus, status),
                (ReconciliationFinding.kind, FindingKind, kind),
                (ReconciliationFinding.severity, FindingSeverity, severity),
            )
            if value
        ]
    except ValueError:
        return []  # a value no finding can have
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            stmt = (
                select(ReconciliationFinding)
                .where(*wanted)
                .order_by(ReconciliationFinding.last_seen.desc())
                .limit(max(1, limit))
            )
            return [_finding_dict(f) for f in (await session.execute(stmt)).scalars()]
    finally:
        await engine.dispose()


@server.tool()
async def get_finding(fingerprint: str) -> dict[str, Any]:
    """Full detail for one finding by its stable fingerprint."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            f = (
                await session.execute(
                    select(ReconciliationFinding).where(
                        ReconciliationFinding.fingerprint == fingerprint
                    )
                )
            ).scalar_one_or_none()
            if f is None:
                return {"error": f"no finding with fingerprint {fingerprint!r}"}
            d = _finding_dict(f)
            d["evidence_refs"] = f.evidence_refs
            d["proposed_actions"] = f.proposed_actions
            d["notes"] = f.notes
            return d
    finally:
        await engine.dispose()


@server.tool()
async def list_services() -> list[dict[str, Any]]:
    """List services with endpoint counts per scope and a DNS split-brain flag."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            services = (
                (await session.execute(select(Service).order_by(Service.name))).scalars().all()
            )
            out = []
            for svc in services:
                eps = list(
                    (
                        await session.execute(
                            select(ServiceEndpoint).where(ServiceEndpoint.service_id == svc.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                out.append(
                    {
                        "name": svc.name,
                        "internal_endpoints": sum(
                            1 for e in eps if e.scope == ResolutionScope.INTERNAL
                        ),
                        "external_endpoints": sum(
                            1 for e in eps if e.scope == ResolutionScope.EXTERNAL
                        ),
                        "split_brain": _split_brain(eps) is not None,
                    }
                )
            return out
    finally:
        await engine.dispose()


@server.tool()
async def get_service(name: str) -> dict[str, Any]:
    """Synthesized record for one service (by name or endpoint hostname):
    internal/external endpoints, DNS split-brain, and the VM/host carrying it."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            svc = (
                await session.execute(select(Service).where(Service.name == name))
            ).scalar_one_or_none()
            if svc is None:
                ep = (
                    await session.execute(
                        select(ServiceEndpoint).where(ServiceEndpoint.hostname == name.lower())
                    )
                ).scalar_one_or_none()
                if ep is not None:
                    svc = await session.get(Service, ep.service_id)
            if svc is None:
                return {"error": f"no service or endpoint matches {name!r}"}
            eps = list(
                (
                    await session.execute(
                        select(ServiceEndpoint).where(ServiceEndpoint.service_id == svc.id)
                    )
                )
                .scalars()
                .all()
            )
            vm = (
                await session.execute(select(VirtualMachine).where(VirtualMachine.name == svc.name))
            ).scalar_one_or_none()
            hosted = None
            if vm is not None:
                cluster = await session.get(Cluster, vm.cluster_id)
                hosted = {
                    "vm": vm.name,
                    "cluster": cluster.name if cluster else None,
                    "node": vm.node_name,
                    "status": vm.status,
                }
            return {
                "name": svc.name,
                "endpoints": [_endpoint_dict(e) for e in eps],
                "split_brain": _split_brain(eps),
                "hosted": hosted,
            }
    finally:
        await engine.dispose()


@server.tool()
async def audit_summary() -> dict[str, Any]:
    """Rollup of the harness DB: host/cluster/VM/service counts and findings
    grouped by status, kind, and severity."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:

            async def _count(model: Any) -> int:
                return (await session.execute(select(func.count()).select_from(model))).scalar_one()

            findings = (await session.execute(select(ReconciliationFinding))).scalars().all()
            by_status: dict[str, int] = {}
            by_kind: dict[str, int] = {}
            by_severity: dict[str, int] = {}
            for f in findings:
                by_status[f.status.value] = by_status.get(f.status.value, 0) + 1
                by_kind[f.kind.value] = by_kind.get(f.kind.value, 0) + 1
                if f.status in {FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED}:
                    by_severity[f.severity.value] = by_severity.get(f.severity.value, 0) + 1
            return {
                "hosts": await _count(Host),
                "clusters": await _count(Cluster),
                "virtual_machines": await _count(VirtualMachine),
                "services": await _count(Service),
                "findings_by_status": by_status,
                "findings_by_kind": by_kind,
                "open_findings_by_severity": by_severity,
            }
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ discovery


@server.tool()
async def diagram(site: str | None = None, format: str = "mermaid") -> dict[str, Any]:
    """The inventory as a diagram: sites, hosts (with guests and open findings),
    clusters, and the services that resolve to each host. A read over the harness
    DB; nothing is contacted. `format` is "mermaid" (default, paste into docs or a
    page), "html" (a standalone page) or "json" (the graph). `site` keeps one site."""
    if format not in ("mermaid", "html", "json"):
        return {"error": "format must be mermaid, html or json"}
    engine = make_engine(database_url())
    try:
        async with make_sessionmaker(engine)() as session:
            model = await diagram_engine.build_model(session, site=site)
    finally:
        await engine.dispose()
    if format == "json":
        return {"format": format, "graph": diagram_engine.to_dict(model), "stats": model.stats}
    text = (
        diagram_engine.render_html(model)
        if format == "html"
        else diagram_engine.render_mermaid(model)
    )
    return {"format": format, "text": text, "stats": model.stats}


@server.tool()
async def config_status() -> dict[str, Any]:
    """Which discovery sources have credentials, which are missing what, and
    where the harness DB and .env live. Secret values are never returned — only
    whether each variable is set. Check this first when run_discovery reports a
    credentials error."""
    return _config_status()


async def _lookup_by_prefix(
    session: AsyncSession, prefix: str
) -> ReconciliationFinding | dict[str, Any]:
    """One finding whose fingerprint starts with ``prefix``, or an error dict.

    Mirrors the CLI's prefix matching so the same short fingerprint works in
    either surface; ambiguity is reported rather than guessed at.
    """
    if not prefix:
        return {"error": "fingerprint prefix cannot be empty"}
    matches = (
        (
            await session.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.fingerprint.like(f"{prefix}%")
                )
            )
        )
        .scalars()
        .all()
    )
    if not matches:
        return {"error": f"no finding matches fingerprint prefix {prefix!r}"}
    if len(matches) > 1:
        return {
            "error": f"fingerprint prefix {prefix!r} is ambiguous ({len(matches)} matches)",
            "matches": [{"fingerprint": f.fingerprint, "title": f.title} for f in matches[:5]],
        }
    return matches[0]


async def _transition_finding(
    fingerprint: str,
    apply: Any,
) -> dict[str, Any]:
    """Resolve a fingerprint prefix, mutate the finding, return its new state."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            found = await _lookup_by_prefix(session, fingerprint)
            if isinstance(found, dict):
                return found
            apply(found)
            return _finding_dict(found)
    finally:
        await engine.dispose()


@server.tool()
async def ack_finding(
    fingerprint: str, by: str | None = None, notes: str | None = None
) -> dict[str, Any]:
    """Acknowledge a finding (status -> acknowledged): seen, not yet fixed.
    Accepts a full fingerprint or a unique prefix. Harness-DB only — changes
    nothing in the infrastructure."""

    def _apply(f: ReconciliationFinding) -> None:
        f.status = FindingStatus.ACKNOWLEDGED
        f.acknowledged_at = datetime.now(UTC)
        f.acknowledged_by = by
        if notes:
            f.notes = notes

    return await _transition_finding(fingerprint, _apply)


@server.tool()
async def resolve_finding(fingerprint: str, notes: str | None = None) -> dict[str, Any]:
    """Manually mark a finding resolved (status -> resolved). Use when the
    underlying condition is genuinely fixed; the reconciler reopens the same
    fingerprint if it recurs. Accepts a full fingerprint or a unique prefix."""

    def _apply(f: ReconciliationFinding) -> None:
        f.status = FindingStatus.RESOLVED
        f.resolved_at = datetime.now(UTC)
        if notes:
            f.notes = notes

    return await _transition_finding(fingerprint, _apply)


@server.tool()
async def suppress_finding(
    fingerprint: str, until: str | None = None, notes: str | None = None
) -> dict[str, Any]:
    """Suppress a finding from default listings (status -> suppressed). For
    known-and-accepted conditions the reconciler will keep re-detecting, such as
    hardware that forges a WWN. ``until`` is an ISO date; omit to suppress
    indefinitely. Accepts a full fingerprint or a unique prefix."""
    suppressed_until: datetime | None = None
    if until:
        try:
            suppressed_until = datetime.fromisoformat(until).replace(tzinfo=UTC)
        except ValueError:
            return {"error": f"until must be an ISO date (got {until!r})"}

    def _apply(f: ReconciliationFinding) -> None:
        f.status = FindingStatus.SUPPRESSED
        f.suppressed_until = suppressed_until
        if notes:
            f.notes = notes

    return await _transition_finding(fingerprint, _apply)


@server.tool()
async def retire_host(hostname: str, rationale: str | None = None) -> dict[str, Any]:
    """Mark a host decommissioned: records a DECOMMISSIONING intent, closes its
    open part placements explicitly, and resolves its open findings. Harness-DB
    only — nothing touches the host. Idempotent. The planners skip retired
    hosts; `helper host retire` is the same operation from the CLI."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            host = (
                await session.execute(select(Host).where(Host.hostname == hostname))
            ).scalar_one_or_none()
            if host is None:
                return {"error": f"no host named {hostname!r}"}
            result = await _retire_host(session, host, declared_by="agent:mcp", rationale=rationale)
            return result.as_dict()
    finally:
        await engine.dispose()
