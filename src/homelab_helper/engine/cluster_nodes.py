"""What makes a host a placement target (Phase 9.2).

The planners that fill, drain or reweigh hosts — ``plan rebalance``, ``plan
surplus``, the memory-pressure pattern in ``bottlenecks`` — may only reason
about **cluster nodes**: a host is a node of a cluster when discovery recorded
it on that cluster's node list (``Cluster.attributes["nodes"]``), or when it
runs one of the cluster's guests (the fallback for rows written before the node
list was persisted). Nothing else qualifies. Free RAM on a NAS, a Pi or a
Kubernetes worker is not capacity a guest can move into, and a NAS running
Docker is not "surplus" because the harness sees no guests on it.

This is the rule #51 fixed into ``plan rebalance`` after the live fleet planned
guests onto a NAS; the other planners had the same hole. One definition, read
by all of them, so the answer to "why wasn't my host considered?" is one
sentence: :data:`NOT_A_NODE`.

Deliberately not restricted by it: ``plan placement``. Placing a *new* workload
is a different question — a Docker host is a legitimate target for a container
— and its own hard rejections (arch, RAM, GPU, intent) do that filtering.

There is no role column. NetBox owns ``role`` (the operator's canonical
inventory, see the sync invariants), and a stored role that disagreed with what
a host actually runs would be the worse signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from homelab_helper.db.models import Cluster, Host, VirtualMachine

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

NOT_A_NODE = "runs no guest and is on no cluster's node list"
"""Why a host is not a placement target; the one reason there is."""

PLACEMENT_TARGET_RULE = (
    "a host is a placement target when it is a node of a cluster: on the "
    "cluster's node list, or running one of its guests"
)


@dataclass(frozen=True)
class ClusterNodes:
    """Cluster membership per host, for every host handed to :func:`cluster_nodes`."""

    members: dict[uuid.UUID, frozenset[uuid.UUID]] = field(default_factory=dict)
    """host id → the clusters it is a node of (empty: not a node)."""

    def of(self, host_id: uuid.UUID) -> frozenset[uuid.UUID]:
        return self.members.get(host_id, frozenset())

    def is_node(self, host_id: uuid.UUID) -> bool:
        return bool(self.members.get(host_id))

    def by_cluster(self) -> dict[uuid.UUID, frozenset[uuid.UUID]]:
        """The inverse view: cluster id → its member host ids."""
        out: dict[uuid.UUID, set[uuid.UUID]] = {}
        for host_id, clusters in self.members.items():
            for cluster_id in clusters:
                out.setdefault(cluster_id, set()).add(host_id)
        return {k: frozenset(v) for k, v in out.items()}


async def cluster_nodes(session: AsyncSession, hosts: Iterable[Host]) -> ClusterNodes:
    """Membership for ``hosts`` from the clusters' node lists, else from their guests."""
    by_id = {h.id: h for h in hosts}
    by_name = {h.hostname: h.id for h in by_id.values()}
    members: dict[uuid.UUID, set[uuid.UUID]] = {}

    for cluster in (await session.execute(select(Cluster))).scalars().all():
        for node in (cluster.attributes or {}).get("nodes") or []:
            host_id = by_name.get(str(node))
            if host_id is not None:
                members.setdefault(host_id, set()).add(cluster.id)

    rows = await session.execute(
        select(VirtualMachine.node_host_id, VirtualMachine.cluster_id).where(
            VirtualMachine.node_host_id.is_not(None)
        )
    )
    for host_id, cluster_id in rows.all():
        if host_id in by_id:
            members.setdefault(host_id, set()).add(cluster_id)

    return ClusterNodes({k: frozenset(v) for k, v in members.items()})


__all__ = ["NOT_A_NODE", "PLACEMENT_TARGET_RULE", "ClusterNodes", "cluster_nodes"]
