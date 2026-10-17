"""print-warden status → ``SERVICE_UNHEALTHY`` findings.

Reads the warden's status contract (``adapters/printwarden.py``) and raises one finding per problem through the
shared categorised reconcile, so invariant 1 holds: a category resolves only when it was observed this run.

- ``printwarden-unreachable``: the status endpoint could not be read. Observed on every run, so a warden that comes
  back resolves it. When it is unreachable, the per-printer categories are not observed and their findings stand.
- ``printer-offline``: the warden has no Moonraker connection to a printer.
- ``printer-klippy``: Moonraker is connected but Klipper is not ready (shutdown, error, startup).
- ``printer-telemetry-stale``: connected, but no update for ``STALE_AFTER_S``.

Klipper state and telemetry age are only observed for a connected printer, so an offline printer's klippy or stale
finding stands until the printer is back and seen healthy.

Targets are ``("service", "print-warden")`` and ``("service", "print-warden/<printer>")``. No playbook maps this kind:
these findings report, they never draft a remediation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.engine.category_findings import CategoryIssue, reconcile_category_findings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

KIND = FindingKind.SERVICE_UNHEALTHY
SERVICE = "print-warden"
UNREACHABLE = "printwarden-unreachable"
PRINTER_CATEGORIES = ("printer-offline", "printer-klippy", "printer-telemetry-stale")
STALE_AFTER_S = 600


def _printer_target(name: str) -> str:
    return f"{SERVICE}/{name}"


def unreachable_issue(url: str, error: str) -> CategoryIssue:
    return CategoryIssue(
        kind=KIND,
        category=UNREACHABLE,
        target_type="service",
        target_id=SERVICE,
        severity=FindingSeverity.HIGH,
        title="print-warden is unreachable",
        description=f"GET {url}/healthz failed: {error}. Printers are not being watched or alerted on.",
        evidence={"url": url, "error": error[:300]},
    )


def printer_issues(status: dict[str, Any]) -> list[CategoryIssue]:
    """One issue per unhealthy printer in an api-1 status body."""
    issues: list[CategoryIssue] = []
    for name, p in sorted((status.get("printers") or {}).items()):
        target = _printer_target(name)
        evidence = {"printer": name, "version": status.get("version"), **p}
        if not p.get("connected"):
            issues.append(
                CategoryIssue(
                    kind=KIND,
                    category="printer-offline",
                    target_type="service",
                    target_id=target,
                    severity=FindingSeverity.MEDIUM,
                    title=f"Printer {name} is offline to print-warden",
                    description=(
                        f"print-warden has no Moonraker connection to {name}: the printer, its host or the "
                        "network is down, and nothing on it is being watched."
                    ),
                    evidence=evidence,
                )
            )
            continue
        klippy = p.get("klippy")
        if klippy not in ("ready", None):
            issues.append(
                CategoryIssue(
                    kind=KIND,
                    category="printer-klippy",
                    target_type="service",
                    target_id=target,
                    severity=FindingSeverity.HIGH
                    if klippy == "shutdown"
                    else FindingSeverity.MEDIUM,
                    title=f"Klipper on {name} is {klippy}",
                    description=(
                        f"Moonraker is reachable but Klipper reports {klippy!r}; the printer cannot print "
                        "until it is restarted. print-warden's events carry the classified shutdown reason."
                    ),
                    evidence=evidence,
                )
            )
        age = p.get("last_update_s_ago")
        if isinstance(age, int | float) and age > STALE_AFTER_S:
            issues.append(
                CategoryIssue(
                    kind=KIND,
                    category="printer-telemetry-stale",
                    target_type="service",
                    target_id=target,
                    severity=FindingSeverity.LOW,
                    title=f"No telemetry from {name} for {age / 60:.0f} min",
                    description=(
                        f"print-warden is connected to {name} but has had no status update for "
                        f"{age:.0f} s; its rules and alerts are working from stale data."
                    ),
                    evidence=evidence,
                )
            )
    return issues


async def reconcile_printwarden(
    session: AsyncSession,
    url: str,
    status: dict[str, Any] | None,
    error: str | None = None,
    *,
    when: datetime | None = None,
) -> tuple[list[CategoryIssue], dict[str, int]]:
    """Reconcile one read of the warden: ``status`` on success, ``error`` when it could not be read."""
    now = when or datetime.now(UTC)
    if status is None:
        issues = [unreachable_issue(url, error or "unknown error")]
        result = await reconcile_category_findings(session, KIND, issues, {UNREACHABLE}, when=now)
        return issues, result.counts()
    issues = printer_issues(status)
    counts: dict[str, int] = {}
    connected = {
        ("service", _printer_target(name))
        for name, p in (status.get("printers") or {}).items()
        if p.get("connected")
    }
    passes = (
        ({UNREACHABLE, "printer-offline"}, None),
        ({"printer-klippy", "printer-telemetry-stale"}, connected),
    )
    for observed, targets in passes:
        result = await reconcile_category_findings(
            session,
            KIND,
            [i for i in issues if i.category in observed],
            observed,
            when=now,
            observed_targets=targets,
        )
        for k, v in result.counts().items():
            counts[k] = counts.get(k, 0) + v
    return issues, counts
