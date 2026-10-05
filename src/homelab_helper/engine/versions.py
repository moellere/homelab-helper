"""Version currency (Phase 8.1) — version facts → ``VERSION_DRIFT`` findings.

Deterministic checks, each a *category* the pass either observed this run
or did not:

- ``pve-updates`` — a Proxmox node with packages pending in its own apt cache.
  MEDIUM when 50+ are pending or ``pve-manager`` itself is among them, LOW
  otherwise. (Proxmox reports package origin only as ``Debian`` or
  ``Proxmox``, so a security count is not derivable here.)
- ``pve-mixed`` — nodes of one Proxmox cluster reporting different
  ``pve-manager`` versions. MEDIUM: migrations and HA between mismatched nodes
  are where version skew bites.
- ``os-eol`` — a host whose OS release is past (HIGH) or within 180 days of
  (MEDIUM) the end-of-support date in ``data/os-eol.yaml``. The table is the
  only authority; no date is guessed.
- ``k8s-skew`` / ``talos-skew`` — Kubernetes nodes on different kubelet
  versions, or Talos nodes on different Talos releases. LOW.
- ``hass-updates`` — Home Assistant ``update.*`` entities that are ``on``,
  summarised per instance. MEDIUM when core, OS or supervisor is behind, LOW
  for add-ons and device firmware.

Findings key by ``(version-drift, target_type, target_id, category)`` and carry
their category in ``evidence_refs`` so resolution can honour invariant 1: a
finding resolves only when its category was observed this run and the issue is
no longer present. A source that failed or was skipped resolves nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.engine.fingerprint import make_fingerprint

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

EOL_TABLE_PATH = Path(__file__).resolve().parent.parent / "data" / "os-eol.yaml"
EOL_WARNING_WINDOW = timedelta(days=180)
PENDING_MEDIUM_THRESHOLD = 50
_LISTED_UPDATES = 8
_CORE_UPDATE_MARKERS = ("home_assistant_core", "home_assistant_operating_system", "supervisor")

CATEGORIES = ("pve-updates", "pve-mixed", "os-eol", "k8s-skew", "talos-skew", "hass-updates")


@dataclass(frozen=True)
class VersionIssue:
    """One version problem, ready to become (or refresh) a finding."""

    category: str
    target_type: str
    target_id: str
    severity: FindingSeverity
    title: str
    description: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return make_fingerprint(
            FindingKind.VERSION_DRIFT.value, self.target_type, self.target_id, self.category
        )


@dataclass
class VersionResult:
    observed: list[str] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- proxmox


def proxmox_issues(cluster: str, nodes: list[dict[str, Any]]) -> list[VersionIssue]:
    """``nodes``: ``{"node", "version", "pending": [apt entries]}`` per reachable node."""
    issues: list[VersionIssue] = []
    for n in nodes:
        pending = n.get("pending") or []
        if not pending:
            continue
        origins: dict[str, int] = {}
        for p in pending:
            origin = str(p.get("Origin") or "other")
            origins[origin] = origins.get(origin, 0) + 1
        manager = next((p for p in pending if p.get("Package") == "pve-manager"), None)
        severity = (
            FindingSeverity.MEDIUM
            if manager or len(pending) >= PENDING_MEDIUM_THRESHOLD
            else FindingSeverity.LOW
        )
        split = ", ".join(f"{v} {k}" for k, v in sorted(origins.items(), key=lambda kv: -kv[1]))
        upgrade = (
            f" pve-manager {manager.get('OldVersion')} → {manager.get('Version')} is among them."
            if manager
            else ""
        )
        issues.append(
            VersionIssue(
                category="pve-updates",
                target_type="host",
                target_id=n["node"],
                severity=severity,
                title=f"{n['node']}: {len(pending)} package update(s) pending",
                description=f"{n['node']} has {len(pending)} package update(s) pending ({split}).{upgrade}",
                evidence={
                    "pending": len(pending),
                    "origins": origins,
                    "pve_manager": manager.get("Version") if manager else None,
                },
            )
        )
    versions = {n["node"]: n.get("version") for n in nodes if n.get("version")}
    if len(set(versions.values())) > 1:
        listing = ", ".join(f"{k} {v}" for k, v in sorted(versions.items()))
        issues.append(
            VersionIssue(
                category="pve-mixed",
                target_type="cluster",
                target_id=cluster,
                severity=FindingSeverity.MEDIUM,
                title=f"Proxmox cluster {cluster}: nodes on {len(set(versions.values()))} versions",
                description=f"Nodes report different pve-manager versions: {listing}.",
                evidence={"versions": versions},
            )
        )
    return issues


# ------------------------------------------------------------------ os eol


def load_eol_table(path: Path = EOL_TABLE_PATH) -> dict[str, Any]:
    with path.open() as fh:
        return dict(yaml.safe_load(fh) or {})


def os_release(capabilities: dict[str, Any]) -> tuple[str, str] | None:
    """``(os_id, version)`` from a host's capabilities, or ``None`` if unknown."""
    os_id = str(capabilities.get("os_id") or "").lower()
    if not os_id:
        return None
    version = capabilities.get("os_version_id")
    if not version:
        pretty = str(capabilities.get("os_pretty_name") or "")
        if os_id == "ubuntu":
            m = re.search(r"(\d{2}\.\d{2})", pretty)
        else:
            m = re.search(r"\b(\d{1,2})\b", pretty)
        version = m.group(1) if m else None
    return (os_id, str(version)) if version else None


def os_eol_issues(
    hosts: Iterable[tuple[str, str, dict[str, Any]]],
    table: dict[str, Any],
    today: date,
) -> list[VersionIssue]:
    """``hosts``: ``(host_id, hostname, capabilities)``."""
    aliases = table.get("aliases") or {}
    issues: list[VersionIssue] = []
    for host_id, hostname, caps in hosts:
        release = os_release(caps)
        if release is None:
            continue
        os_id, version = release
        entry = (table.get(aliases.get(os_id, os_id)) or {}).get(version)
        if not entry:
            continue
        eol = entry["eol"] if isinstance(entry["eol"], date) else date.fromisoformat(entry["eol"])
        remaining = eol - today
        if remaining > EOL_WARNING_WINDOW:
            continue
        name = caps.get("os_pretty_name") or f"{os_id} {version}"
        past = remaining.days < 0
        issues.append(
            VersionIssue(
                category="os-eol",
                target_type="host",
                target_id=host_id,
                severity=FindingSeverity.HIGH if past else FindingSeverity.MEDIUM,
                title=(
                    f"{hostname}: {name} is past end of support"
                    if past
                    else f"{hostname}: {name} reaches end of support in {remaining.days} days"
                ),
                description=(
                    f"{name} ({entry.get('codename', '')}) {entry.get('basis', 'support')} "
                    f"is {eol.isoformat()}; {hostname} "
                    + (
                        "no longer receives free security updates."
                        if past
                        else "should be upgraded before then."
                    )
                ),
                evidence={"os": name, "eol": eol.isoformat(), "days_remaining": remaining.days},
            )
        )
    return issues


# ---------------------------------------------------------- kubernetes/talos


def _talos_release(caps: dict[str, Any]) -> str | None:
    if str(caps.get("os_id") or "").lower() != "talos":
        return None
    m = re.search(r"v\d+\.\d+\.\d+", str(caps.get("os_pretty_name") or ""))
    return m.group(0) if m else None


def k8s_issues(
    hosts: Iterable[tuple[str, str, dict[str, Any]]],
) -> tuple[list[VersionIssue], set[str]]:
    """Skew findings plus the categories that had any data to look at."""
    kubelet: dict[str, str] = {}
    talos: dict[str, str] = {}
    for _host_id, hostname, caps in hosts:
        if caps.get("k8s_kubelet_version"):
            kubelet[hostname] = str(caps["k8s_kubelet_version"])
        release = _talos_release(caps)
        if release:
            talos[hostname] = release
    observed: set[str] = set()
    issues: list[VersionIssue] = []
    for category, versions, what in (
        ("k8s-skew", kubelet, "kubelet"),
        ("talos-skew", talos, "Talos"),
    ):
        if not versions:
            continue
        observed.add(category)
        if len(set(versions.values())) > 1:
            listing = ", ".join(f"{k} {v}" for k, v in sorted(versions.items()))
            issues.append(
                VersionIssue(
                    category=category,
                    target_type="cluster",
                    target_id="kubernetes",
                    severity=FindingSeverity.LOW,
                    title=f"Kubernetes nodes on {len(set(versions.values()))} {what} versions",
                    description=f"{what} versions differ across nodes: {listing}.",
                    evidence={"versions": versions},
                )
            )
    return issues, observed


# ------------------------------------------------------------ home assistant


def hass_update_issues(instance: str, states: list[dict[str, Any]]) -> list[VersionIssue]:
    pending = [
        s for s in states if s.get("entity_id", "").startswith("update.") and s.get("state") == "on"
    ]
    if not pending:
        return []
    core = [s for s in pending if any(m in s["entity_id"] for m in _CORE_UPDATE_MARKERS)]

    def _label(s: dict[str, Any]) -> str:
        name = s.get("title") or s.get("name") or s["entity_id"]
        return f"{name} {s.get('installed_version') or '?'} → {s.get('latest_version') or '?'}"

    parts = [f"{len(pending)} update(s) available"]
    if core:
        parts.append("platform: " + "; ".join(_label(s) for s in core))
    others = [s for s in pending if s not in core]
    if others:
        shown = ", ".join(_label(s) for s in others[:_LISTED_UPDATES])
        parts.append(
            f"add-ons/devices: {shown}"
            + (
                f" and {len(others) - _LISTED_UPDATES} more"
                if len(others) > _LISTED_UPDATES
                else ""
            )
        )
    return [
        VersionIssue(
            category="hass-updates",
            target_type="service",
            target_id=instance,
            severity=FindingSeverity.MEDIUM if core else FindingSeverity.LOW,
            title=f"Home Assistant ({instance}): {len(pending)} update(s) available",
            description=". ".join(parts) + ".",
            evidence={
                "pending": len(pending),
                "platform": [s["entity_id"] for s in core],
                "entities": sorted(s["entity_id"] for s in pending),
            },
        )
    ]


# ---------------------------------------------------------------- reconcile


def _category_of(finding: ReconciliationFinding) -> str | None:
    for ref in finding.evidence_refs or []:
        if ref.get("type") == "version-category":
            return str(ref.get("category"))
    return None


async def reconcile_version_findings(
    session: AsyncSession,
    issues: list[VersionIssue],
    observed: set[str],
    *,
    when: datetime | None = None,
) -> VersionResult:
    """Upsert one finding per issue; resolve only within observed categories."""
    now = when or datetime.now(UTC)
    result = VersionResult(observed=sorted(observed))
    active = {i.fingerprint for i in issues}
    for issue in issues:
        refs = [
            {"type": "version-category", "category": issue.category},
            {"type": "version", **issue.evidence},
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
                    kind=FindingKind.VERSION_DRIFT,
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
                    ReconciliationFinding.kind == FindingKind.VERSION_DRIFT,
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
        if row.fingerprint in active or _category_of(row) not in observed:
            continue
        row.status = FindingStatus.RESOLVED
        row.resolved_at = now
        result.resolved.append(row.title)
    await session.flush()
    return result


__all__ = [
    "CATEGORIES",
    "EOL_TABLE_PATH",
    "VersionIssue",
    "VersionResult",
    "hass_update_issues",
    "k8s_issues",
    "load_eol_table",
    "os_eol_issues",
    "os_release",
    "proxmox_issues",
    "reconcile_version_findings",
]
