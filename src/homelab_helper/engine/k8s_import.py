"""K8s node discovery — Kubernetes nodes → Host observations (INFERRED).

Each cluster node is a Host the kernel probes may also cover. We record the
node's facts as ``INFERRED`` observations and reconcile: the reconciler's
precedence keeps any kernel-``VERIFIED`` fact (kernel, arch, cpu, memory) and
lets the K8s-only facts (kubelet/runtime versions, roles) fill in. This is
multi-source coordination on the same hosts — a management plane enriching, not
overriding, ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from homelab_helper.db.enums import Confidence, DiscoverySource, IntentTargetType, PrivilegeLevel
from homelab_helper.db.models import Cluster, DiscoveryRun, Host, Observation, Probe
from homelab_helper.engine.reconciler import Reconciler

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.adapters.kubernetes import K8sAdapter

_K8S_PROBE = "k8s.node"
KUBERNETES = "kubernetes"


@dataclass
class K8sDiscoverResult:
    nodes_seen: int = 0
    hosts_created: list[str] = field(default_factory=list)
    hosts_matched: list[str] = field(default_factory=list)
    cluster: str | None = None
    """The ``Cluster(kind="kubernetes")`` row the nodes were recorded on."""


async def _ensure_probe(session: AsyncSession) -> Probe:
    probe = (
        await session.execute(select(Probe).where(Probe.name == _K8S_PROBE))
    ).scalar_one_or_none()
    if probe is not None:
        return probe
    probe = Probe(
        name=_K8S_PROBE,
        version="0.1.0",
        module_path="k8s:node",
        required_privilege=PrivilegeLevel.API,
        produces_keys=[],
        description="Kubernetes node facts (management-plane, INFERRED).",
    )
    session.add(probe)
    await session.flush()
    return probe


async def _resolve_host(session: AsyncSession, name: str, ip: str | None) -> tuple[Host, bool]:
    conds = [Host.hostname == name]
    if ip:
        conds.append(Host.primary_ip == ip)
    existing = (
        (await session.execute(select(Host).where(or_(*conds)).order_by(Host.created_at)))
        .scalars()
        .first()
    )
    if existing is not None:
        if ip and not existing.primary_ip:
            existing.primary_ip = ip
        return existing, False
    host = Host(hostname=name, primary_ip=ip, discovery_source=DiscoverySource.K8S)
    session.add(host)
    await session.flush()
    return host, True


def _node_observations(node: dict[str, Any]) -> list[tuple[str, Any]]:
    return [
        ("host.identity.kernel", node.get("kernel")),
        ("host.identity.os_pretty_name", node.get("os_image")),
        ("host.identity.machine_id", node.get("machine_id")),
        ("host.cpu.architecture", node.get("arch")),
        ("host.cpu.cores", node.get("cpu_capacity")),
        ("host.memory.mem_total_bytes", node.get("memory_bytes")),
        ("host.k8s.node_name", node.get("name")),
        ("host.k8s.kubelet_version", node.get("kubelet_version")),
        ("host.k8s.container_runtime", node.get("container_runtime")),
        ("host.k8s.roles", node.get("roles") or None),
    ]


async def discover_k8s_nodes(
    session: AsyncSession,
    adapter: K8sAdapter,
    *,
    when: datetime,
) -> K8sDiscoverResult:
    """Record each K8s node's facts as INFERRED observations, then reconcile."""
    result = K8sDiscoverResult()
    probe = await _ensure_probe(session)
    roles: dict[str, list[str]] = {}

    for node in await adapter.list_nodes():
        name = node.get("name")
        if not isinstance(name, str) or not name:
            continue
        result.nodes_seen += 1
        roles[name] = list(node.get("roles") or [])
        host, created = await _resolve_host(session, name, node.get("internal_ip"))
        (result.hosts_created if created else result.hosts_matched).append(name)

        run = DiscoveryRun(
            host_id=host.id,
            probe_id=probe.id,
            probe_name=_K8S_PROBE,
            probe_version="0.1.0",
            privilege_level=PrivilegeLevel.API,
            triggered_by="k8s",
        )
        session.add(run)
        await session.flush()
        for key, value in _node_observations(node):
            if value is None:
                continue
            session.add(
                Observation(
                    run_id=run.id,
                    key=key,
                    value=value,
                    confidence=Confidence.INFERRED,  # management-plane: loses to kernel VERIFIED
                    target_type=IntentTargetType.HOST,
                    target_id=str(host.id),
                )
            )
        await session.flush()
        await Reconciler().reconcile_host(session, host.id)

    if roles:
        result.cluster = await _record_cluster(
            session,
            await adapter.cluster_name(),
            roles,
            result.hosts_created + result.hosts_matched,
            when,
        )
    return result


async def _record_cluster(
    session: AsyncSession,
    context: str,
    roles: dict[str, list[str]],
    hostnames: list[str],
    when: datetime,
) -> str:
    """Upsert this context's ``Cluster(kind="kubernetes")`` with the node list just seen.

    The list is replaced each run, so a node that left the cluster leaves the
    list. Membership is by the matched ``Host`` names (a node may match a host
    recorded under another name by its IP). A name already taken by another
    kind of cluster gets a ``(kubernetes)`` suffix rather than adopting it.
    """
    name = context
    taken = (
        await session.execute(select(Cluster).where(Cluster.name == name))
    ).scalar_one_or_none()
    if taken is not None and taken.kind != KUBERNETES:
        name = f"{context} (kubernetes)"
        taken = (
            await session.execute(select(Cluster).where(Cluster.name == name))
        ).scalar_one_or_none()
    cluster = taken
    if cluster is None:
        cluster = Cluster(name=name, kind=KUBERNETES, discovery_source=DiscoverySource.K8S)
        session.add(cluster)
    attrs = dict(cluster.attributes or {})
    attrs["nodes"] = sorted(set(hostnames))
    attrs["roles"] = {node: roles[node] for node in sorted(roles)}
    cluster.attributes = attrs
    cluster.node_count = len(roles)
    cluster.discovery_last_run = when
    await session.flush()
    return name


__all__ = ["KUBERNETES", "K8sDiscoverResult", "discover_k8s_nodes"]
