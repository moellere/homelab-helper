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
- ``ceph-eol`` — the Ceph release the cluster runs is past (HIGH) or within 180
  days of (MEDIUM) its upstream end of life, from the ``ceph`` section of the
  same table. ``ceph-mixed`` — daemons on different Ceph versions, which is
  the state between the first and last node of an upgrade. MEDIUM.
- ``k8s-skew`` / ``talos-skew`` — Kubernetes nodes on different kubelet
  versions, or Talos nodes on different Talos releases. LOW.
- ``hass-updates`` — Home Assistant ``update.*`` entities that are ``on``,
  summarised per instance. MEDIUM when core, OS or supervisor is behind, LOW
  for add-ons and device firmware.

Findings and their resolution follow ``engine/category_findings.py``: keyed by
``(version-drift, target_type, target_id, category)``, resolved only when the
category was observed this run (invariant 1).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.engine.category_findings import (
    CategoryIssue,
    CategoryResult,
    reconcile_category_findings,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

EOL_TABLE_PATH = Path(__file__).resolve().parent.parent / "data" / "os-eol.yaml"
EOL_WARNING_WINDOW = timedelta(days=180)
PENDING_MEDIUM_THRESHOLD = 50
_LISTED_UPDATES = 8
_CORE_UPDATE_MARKERS = ("home_assistant_core", "home_assistant_operating_system", "supervisor")

CATEGORIES = (
    "pve-updates",
    "pve-mixed",
    "ceph-eol",
    "ceph-mixed",
    "os-eol",
    "k8s-skew",
    "talos-skew",
    "hass-updates",
)


def VersionIssue(**kw: Any) -> CategoryIssue:
    return CategoryIssue(kind=FindingKind.VERSION_DRIFT, **kw)


# ----------------------------------------------------------------- proxmox


def proxmox_issues(cluster: str, nodes: list[dict[str, Any]]) -> list[CategoryIssue]:
    """``nodes``: ``{"node", "version", "pending": [apt entries]}`` per reachable node."""
    issues: list[CategoryIssue] = []
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


# -------------------------------------------------------------------- ceph

_CEPH_VERSION = re.compile(r"ceph version (\d+\.\d+\.\d+)")


def ceph_daemon_versions(metadata: dict[str, Any]) -> dict[str, str]:
    """``{"mon.bmax0": "19.2.6", "osd.2": "19.2.6", …}`` from ``/cluster/ceph/metadata``."""
    out: dict[str, str] = {}
    for kind in ("mon", "mgr", "mds"):
        entries = metadata.get(kind) or {}
        for name, meta in entries.items() if isinstance(entries, dict) else []:
            if isinstance(meta, dict):
                version = meta.get("ceph_version_short") or _short(meta.get("ceph_version"))
                if version:
                    out[f"{kind}.{name}"] = version
    for meta in metadata.get("osd") or []:
        if isinstance(meta, dict):
            version = meta.get("ceph_version_short") or _short(meta.get("ceph_version"))
            if version and meta.get("id") is not None:
                out[f"osd.{meta['id']}"] = version
    return out


def _short(text: Any) -> str | None:
    m = _CEPH_VERSION.search(str(text or ""))
    return m.group(1) if m else None


def ceph_issues(
    cluster: str, metadata: dict[str, Any], table: dict[str, Any], today: date
) -> tuple[list[CategoryIssue], set[str]]:
    """EOL and mixed-version findings for the Ceph cluster ``metadata`` describes."""
    versions = ceph_daemon_versions(metadata)
    if not versions:
        return [], set()
    issues: list[CategoryIssue] = []
    distinct = sorted(set(versions.values()))
    if len(distinct) > 1:
        by_version = {v: sorted(d for d, dv in versions.items() if dv == v) for v in distinct}
        listing = "; ".join(f"{v}: {', '.join(ds)}" for v, ds in by_version.items())
        issues.append(
            VersionIssue(
                category="ceph-mixed",
                target_type="cluster",
                target_id=f"ceph:{cluster}",
                severity=FindingSeverity.MEDIUM,
                title=f"Ceph on {cluster}: daemons on {len(distinct)} versions",
                description=f"Ceph daemons run different versions — {listing}. Finish the rolling upgrade.",
                evidence={"versions": by_version},
            )
        )
    newest = max(distinct, key=lambda v: tuple(int(x) for x in v.split(".")))
    major = newest.split(".")[0]
    entry = (table.get("ceph") or {}).get(major)
    if entry:
        eol = entry["eol"] if isinstance(entry["eol"], date) else date.fromisoformat(entry["eol"])
        remaining = eol - today
        if remaining <= EOL_WARNING_WINDOW:
            past = remaining.days < 0
            name = f"Ceph {newest} ({entry.get('codename', major)})"
            issues.append(
                VersionIssue(
                    category="ceph-eol",
                    target_type="cluster",
                    target_id=f"ceph:{cluster}",
                    severity=FindingSeverity.HIGH if past else FindingSeverity.MEDIUM,
                    title=(
                        f"{name} is past end of life"
                        if past
                        else f"{name} reaches end of life in {remaining.days} days"
                    ),
                    description=(
                        f"{name} {entry.get('basis', 'end of life')} is {eol.isoformat()}; "
                        + (
                            "it no longer receives upstream fixes."
                            if past
                            else "plan the upgrade to the next release before then."
                        )
                    ),
                    evidence={
                        "version": newest,
                        "eol": eol.isoformat(),
                        "days_remaining": remaining.days,
                    },
                )
            )
    return issues, {"ceph-eol", "ceph-mixed"}


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
) -> list[CategoryIssue]:
    """``hosts``: ``(host_id, hostname, capabilities)``."""
    aliases = table.get("aliases") or {}
    issues: list[CategoryIssue] = []
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
) -> tuple[list[CategoryIssue], set[str]]:
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
    issues: list[CategoryIssue] = []
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


def hass_update_issues(instance: str, states: list[dict[str, Any]]) -> list[CategoryIssue]:
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


async def reconcile_version_findings(
    session: AsyncSession,
    issues: list[CategoryIssue],
    observed: set[str],
    *,
    when: datetime | None = None,
) -> CategoryResult:
    """Upsert one ``version-drift`` finding per issue; resolve only within observed categories."""
    return await reconcile_category_findings(
        session, FindingKind.VERSION_DRIFT, issues, observed, when=when
    )


__all__ = [
    "CATEGORIES",
    "EOL_TABLE_PATH",
    "VersionIssue",
    "ceph_daemon_versions",
    "ceph_issues",
    "hass_update_issues",
    "k8s_issues",
    "load_eol_table",
    "os_eol_issues",
    "os_release",
    "proxmox_issues",
    "reconcile_version_findings",
]
