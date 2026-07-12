"""Argo CD drift reconcile — Application sync/health → DRIFT_CANDIDATE findings.

Turns the drift the :class:`ArgoCDAdapter` already surfaces (git desired-state
vs live cluster) into tracked findings. Argo CD does the diff; this module owns
the finding lifecycle on top of it, mirroring :class:`AssertionEngine`:

- A drifted Application (``OutOfSync`` or non-``Healthy``) upserts one
  ``DRIFT_CANDIDATE`` finding, keyed by a deterministic fingerprint of the app
  name — so the same app drifting again bumps ``last_seen`` on the *same* row,
  and a RESOLVED finding whose app drifts anew flips back to OPEN with
  ``first_seen`` reset (reopen-on-recurrence, invariant #3).
- An Application observed this run as ``Synced`` + ``Healthy`` resolves its
  finding if one was open.

Scope discipline (invariant #1 — absence must never auto-resolve): resolution is
driven only by apps Argo CD *reports* as healthy this run. An app that vanished
from Argo CD entirely is not "resolved" — its finding is left untouched, exactly
as the assertion engine leaves deleted assertions' findings alone.

Severity ladder: ``Degraded`` / ``Missing`` health → HIGH (the workload isn't
running as declared); a plain ``OutOfSync`` while still healthy → MEDIUM (config
has drifted but the app is up).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.adapters.argocd import application_is_drifted
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.engine.fingerprint import make_fingerprint

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_TARGET_TYPE = "argocd-app"
_ROOT_CAUSE = "argocd-drift"
_UNHEALTHY = {"Degraded", "Missing", "Unknown"}


@dataclass
class ArgoCDDriftResult:
    opened: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)

    @property
    def drifted(self) -> int:
        return len(self.opened) + len(self.reopened) + len(self.updated)


def _drift_fingerprint(app_name: str) -> str:
    """Stable per-app fingerprint, so one app maps to one finding row forever."""
    return make_fingerprint(FindingKind.DRIFT_CANDIDATE.value, _TARGET_TYPE, app_name, _ROOT_CAUSE)


def _drift_severity(app: dict[str, Any]) -> FindingSeverity:
    health = app.get("health_status")
    if health in _UNHEALTHY:
        return FindingSeverity.HIGH
    return FindingSeverity.MEDIUM


def _drift_description(app: dict[str, Any]) -> str:
    sync = app.get("sync_status") or "?"
    health = app.get("health_status") or "?"
    oos = app.get("out_of_sync_resources") or []
    target = f"{app.get('repo_url') or '?'}@{app.get('target_revision') or 'HEAD'}"
    lead = f"Argo CD app {app.get('name')!r} is {sync}/{health} against {target}."
    if oos:
        detail = ", ".join(f"{r.get('kind')}/{r.get('name')} ({r.get('status')})" for r in oos)
        return f"{lead} Out-of-sync: {detail}."
    return lead


def _drift_affected(app: dict[str, Any]) -> list[dict[str, str]]:
    affected = [{"target_type": _TARGET_TYPE, "target_id": str(app.get("name"))}]
    namespace = app.get("namespace")
    if namespace:
        affected.append({"target_type": "namespace", "target_id": str(namespace)})
    return affected


async def _find_by_fingerprint(
    session: AsyncSession, fingerprint: str
) -> ReconciliationFinding | None:
    return (
        await session.execute(
            select(ReconciliationFinding).where(ReconciliationFinding.fingerprint == fingerprint)
        )
    ).scalar_one_or_none()


async def reconcile_argocd_drift(
    session: AsyncSession,
    applications: list[dict[str, Any]],
    *,
    when: datetime | None = None,
) -> ArgoCDDriftResult:
    """Upsert DRIFT_CANDIDATE findings from parsed Argo CD applications."""
    result = ArgoCDDriftResult()
    now_ts = when or datetime.now(UTC)

    for app in applications:
        name = str(app.get("name") or "")
        if not name:
            continue
        fp = _drift_fingerprint(name)
        existing = await _find_by_fingerprint(session, fp)

        if application_is_drifted(app):
            severity = _drift_severity(app)
            title = f"Argo CD drift: {name}"
            description = _drift_description(app)
            affected = _drift_affected(app)
            if existing is None:
                session.add(
                    ReconciliationFinding(
                        kind=FindingKind.DRIFT_CANDIDATE,
                        severity=severity,
                        fingerprint=fp,
                        title=title,
                        description=description,
                        affected=affected,
                        evidence_refs=[{"type": "argocd_application", "id": name}],
                        status=FindingStatus.OPEN,
                        first_seen=now_ts,
                        last_seen=now_ts,
                    )
                )
                result.opened.append(name)
            else:
                if existing.status == FindingStatus.RESOLVED:
                    existing.status = FindingStatus.OPEN
                    existing.resolved_at = None
                    existing.first_seen = now_ts
                    result.reopened.append(name)
                else:
                    result.updated.append(name)
                existing.last_seen = now_ts
                existing.severity = severity
                existing.title = title
                existing.description = description
                existing.affected = affected
                existing.evidence_refs = [{"type": "argocd_application", "id": name}]
        elif existing is not None and existing.status in {
            FindingStatus.OPEN,
            FindingStatus.ACKNOWLEDGED,
        }:
            existing.status = FindingStatus.RESOLVED
            existing.resolved_at = now_ts
            result.resolved.append(name)

    await session.flush()
    return result


__all__ = ["ArgoCDDriftResult", "reconcile_argocd_drift"]
