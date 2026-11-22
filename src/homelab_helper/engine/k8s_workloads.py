"""Kubernetes workload health → ``WORKLOAD_UNHEALTHY`` findings (Phase 7 slice 3).

The first finding kind that carries a workload identity, so the restart
playbook has something deterministic to act on. Same upsert discipline as the
Argo CD drift module: fingerprint-keyed, reopen-on-recurrence, auto-resolve
only for a workload observed healthy this run (a workload absent from the
listing is left untouched — invariant #1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.adapters.kubernetes import workload_is_unhealthy
from homelab_helper.db.enums import DiscoverySource, FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import Cluster, ReconciliationFinding
from homelab_helper.engine.fingerprint import make_fingerprint

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ROOT_CAUSE = "ready-below-desired"
KUBERNETES = "kubernetes"


@dataclass
class WorkloadHealthResult:
    seen: int = 0
    opened: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)

    @property
    def unhealthy(self) -> list[str]:
        return [*self.opened, *self.reopened, *self.updated]


def workload_id(w: dict[str, Any]) -> str:
    return f"{w['namespace']}/{w['kind']}/{w['name']}"


def _fingerprint(wid: str) -> str:
    return make_fingerprint(FindingKind.WORKLOAD_UNHEALTHY.value, "workload", wid, ROOT_CAUSE)


def _severity(w: dict[str, Any]) -> FindingSeverity:
    return FindingSeverity.HIGH if w["ready"] == 0 else FindingSeverity.MEDIUM


def _describe(w: dict[str, Any]) -> str:
    return (
        f"{w['kind']} {w['name']} in {w['namespace']} has {w['ready']} of {w['desired']} "
        f"replicas ready with its rollout settled (Available={w.get('available')})."
    )


def _workload_record(w: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": workload_id(w),
        "namespace": str(w["namespace"]),
        "kind": str(w["kind"]),
        "name": str(w["name"]),
        "desired": int(w.get("desired") or 0),
        "ready": int(w.get("ready") or 0),
        "settled": bool(w.get("settled", True)),
        "images": list(w.get("images") or []),
        "cpu_request": w.get("cpu_request"),
        "cpu_limit": w.get("cpu_limit"),
        "mem_request": w.get("mem_request"),
        "mem_limit": w.get("mem_limit"),
    }


async def _persist_cluster_workloads(
    session: AsyncSession,
    records: list[dict[str, Any]],
    *,
    cluster_name: str | None,
    when: datetime,
) -> None:
    """Attach the discovered workload inventory to the Kubernetes Cluster row.

    An empty listing never overwrites existing records (invariant #1); a
    non-empty listing replaces the list so removed workloads are reaped.
    """
    if not records:
        return
    cluster: Cluster | None = None
    if cluster_name:
        cluster = (
            await session.execute(
                select(Cluster).where(Cluster.name == cluster_name, Cluster.kind == KUBERNETES)
            )
        ).scalar_one_or_none()
    if cluster is None:
        cluster = (
            (
                await session.execute(
                    select(Cluster).where(Cluster.kind == KUBERNETES).order_by(Cluster.created_at)
                )
            )
            .scalars()
            .first()
        )
    if cluster is None and cluster_name:
        cluster = Cluster(
            name=cluster_name,
            kind=KUBERNETES,
            discovery_source=DiscoverySource.K8S,
            discovery_last_run=when,
        )
        session.add(cluster)
    if cluster is not None:
        attrs = dict(cluster.attributes or {})
        attrs["workloads"] = sorted(records, key=lambda r: r["id"])
        cluster.attributes = attrs


async def persisted_k8s_workloads(session: AsyncSession) -> list[dict[str, Any]]:
    """Every persisted Kubernetes workload across all ``Cluster(kind="kubernetes")`` rows."""
    clusters = (
        (
            await session.execute(
                select(Cluster).where(Cluster.kind == KUBERNETES).order_by(Cluster.name)
            )
        )
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    for cluster in clusters:
        for w in (cluster.attributes or {}).get("workloads") or []:
            if isinstance(w, dict) and w.get("name"):
                out.append({"cluster": cluster.name, **w})
    return out


async def reconcile_workload_health(
    session: AsyncSession,
    workloads: list[dict[str, Any]],
    *,
    when: datetime | None = None,
    cluster_name: str | None = None,
) -> WorkloadHealthResult:
    """Upsert one ``WORKLOAD_UNHEALTHY`` finding per unhealthy workload; resolve healed or removed ones."""
    now = when or datetime.now(UTC)
    result = WorkloadHealthResult(seen=len(workloads))
    seen_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    for w in workloads:
        if not (w.get("namespace") and w.get("name") and w.get("kind")):
            continue
        wid = workload_id(w)
        seen_ids.add(wid)
        records.append(_workload_record(w))
        fp = _fingerprint(wid)
        row = (
            await session.execute(
                select(ReconciliationFinding).where(ReconciliationFinding.fingerprint == fp)
            )
        ).scalar_one_or_none()
        affected = [
            {"target_type": "workload", "target_id": wid},
            {"target_type": "namespace", "target_id": str(w["namespace"])},
        ]
        if workload_is_unhealthy(w):
            if row is None:
                session.add(
                    ReconciliationFinding(
                        kind=FindingKind.WORKLOAD_UNHEALTHY,
                        severity=_severity(w),
                        fingerprint=fp,
                        title=f"Workload unhealthy: {wid}",
                        description=_describe(w),
                        evidence_refs=[{"type": "k8s_workload", "id": wid}],
                        affected=affected,
                        status=FindingStatus.OPEN,
                        first_seen=now,
                        last_seen=now,
                    )
                )
                result.opened.append(wid)
                continue
            if row.status is FindingStatus.RESOLVED:
                row.status = FindingStatus.OPEN
                row.resolved_at = None
                row.first_seen = now
                result.reopened.append(wid)
            else:
                result.updated.append(wid)
            row.last_seen = now
            row.severity = _severity(w)
            row.description = _describe(w)
            row.affected = affected
        elif row is not None and row.status in (FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED):
            row.status = FindingStatus.RESOLVED
            row.resolved_at = now
            result.resolved.append(wid)

    if seen_ids:
        open_rows = (
            (
                await session.execute(
                    select(ReconciliationFinding).where(
                        ReconciliationFinding.kind == FindingKind.WORKLOAD_UNHEALTHY,
                        ReconciliationFinding.status.in_(
                            (FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED)
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in open_rows:
            target = next(
                (
                    str(a["target_id"])
                    for a in (row.affected or [])
                    if a.get("target_type") == "workload" and a.get("target_id")
                ),
                None,
            )
            if target and target not in seen_ids:
                row.status = FindingStatus.RESOLVED
                row.resolved_at = now
                result.resolved.append(target)

    await _persist_cluster_workloads(session, records, cluster_name=cluster_name, when=now)
    await session.flush()
    return result


__all__ = [
    "KUBERNETES",
    "WorkloadHealthResult",
    "persisted_k8s_workloads",
    "reconcile_workload_health",
    "workload_id",
]
