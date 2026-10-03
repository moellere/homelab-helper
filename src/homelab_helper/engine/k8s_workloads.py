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
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.engine.fingerprint import make_fingerprint

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ROOT_CAUSE = "ready-below-desired"


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


async def reconcile_workload_health(
    session: AsyncSession,
    workloads: list[dict[str, Any]],
    *,
    when: datetime | None = None,
) -> WorkloadHealthResult:
    """Upsert one ``WORKLOAD_UNHEALTHY`` finding per unhealthy workload; resolve the healed ones."""
    now = when or datetime.now(UTC)
    result = WorkloadHealthResult(seen=len(workloads))
    for w in workloads:
        if not (w.get("namespace") and w.get("name") and w.get("kind")):
            continue
        wid = workload_id(w)
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
    await session.flush()
    return result


__all__ = ["WorkloadHealthResult", "reconcile_workload_health", "workload_id"]
