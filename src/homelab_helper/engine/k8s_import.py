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
from homelab_helper.db.models import DiscoveryRun, Host, Observation, Probe
from homelab_helper.engine.reconciler import Reconciler

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.adapters.kubernetes import K8sAdapter

_K8S_PROBE = "k8s.node"


@dataclass
class K8sDiscoverResult:
    nodes_seen: int = 0
    hosts_created: list[str] = field(default_factory=list)
    hosts_matched: list[str] = field(default_factory=list)


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

    for node in await adapter.list_nodes():
        name = node.get("name")
        if not isinstance(name, str) or not name:
            continue
        result.nodes_seen += 1
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

    return result


__all__ = ["K8sDiscoverResult", "discover_k8s_nodes"]
