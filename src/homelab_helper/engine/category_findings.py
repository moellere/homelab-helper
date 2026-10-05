"""Categorised findings — the reconcile shared by the Phase 8 checks.

A Phase 8 pass runs several independent checks, each a *category* (``pve-updates``,
``backup-stale``, …). An issue becomes one finding keyed by
``(kind, target_type, target_id, category)``; the category also rides in
``evidence_refs`` so a later run can tell which check owns a row. Resolution
honours invariant 1: an open finding resolves only when its category was
observed this run and produced no issue for that target. A check whose source
failed is simply absent from ``observed`` and its findings are left alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.engine.fingerprint import make_fingerprint

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_CATEGORY_REF = "category"


@dataclass(frozen=True)
class CategoryIssue:
    """One problem found by one check, ready to become (or refresh) a finding."""

    kind: FindingKind
    category: str
    target_type: str
    target_id: str
    severity: FindingSeverity
    title: str
    description: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return make_fingerprint(self.kind.value, self.target_type, self.target_id, self.category)


@dataclass
class CategoryResult:
    observed: list[str] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "opened": len(self.opened),
            "reopened": len(self.reopened),
            "updated": len(self.updated),
            "resolved": len(self.resolved),
        }


def category_of(finding: ReconciliationFinding) -> str | None:
    for ref in finding.evidence_refs or []:
        if ref.get("type") == _CATEGORY_REF:
            return str(ref.get("category"))
    return None


async def reconcile_category_findings(
    session: AsyncSession,
    kind: FindingKind,
    issues: list[CategoryIssue],
    observed: set[str],
    *,
    when: datetime | None = None,
) -> CategoryResult:
    """Upsert one finding per issue of ``kind``; resolve only within observed categories."""
    now = when or datetime.now(UTC)
    result = CategoryResult(observed=sorted(observed))
    active = {i.fingerprint for i in issues}
    for issue in issues:
        refs = [
            {"type": _CATEGORY_REF, "category": issue.category},
            {"type": "evidence", **issue.evidence},
        ]
        affected = [{"target_type": issue.target_type, "target_id": issue.target_id}]
        row = (
            await session.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.fingerprint == issue.fingerprint
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                ReconciliationFinding(
                    kind=kind,
                    severity=issue.severity,
                    fingerprint=issue.fingerprint,
                    title=issue.title[:512],
                    description=issue.description,
                    affected=affected,
                    evidence_refs=refs,
                    status=FindingStatus.OPEN,
                    first_seen=now,
                    last_seen=now,
                )
            )
            result.opened.append(issue.title)
            continue
        if row.status == FindingStatus.RESOLVED:
            row.status = FindingStatus.OPEN
            row.resolved_at = None
            row.first_seen = now
            result.reopened.append(issue.title)
        else:
            result.updated.append(issue.title)
        row.last_seen = now
        row.severity = issue.severity
        row.title = issue.title[:512]
        row.description = issue.description
        row.affected = affected
        row.evidence_refs = refs

    open_rows = (
        (
            await session.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.kind == kind,
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
        if row.fingerprint in active or category_of(row) not in observed:
            continue
        row.status = FindingStatus.RESOLVED
        row.resolved_at = now
        result.resolved.append(row.title)
    await session.flush()
    return result


__all__ = ["CategoryIssue", "CategoryResult", "category_of", "reconcile_category_findings"]
