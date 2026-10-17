"""print-warden status → ``SERVICE_UNHEALTHY`` findings.

Reads the warden's status contract (``adapters/printwarden.py``) and raises one finding per problem through the
shared categorised reconcile, so invariant 1 holds: a category resolves only when it was observed this run.

- ``printwarden-unreachable``: the status endpoint could not be read. Observed on every run, so a warden that comes
  back resolves it. When it is unreachable, the per-printer categories are not observed and their findings stand.
- ``printer-offline``: the warden has no Moonraker connection to a printer.
- ``printer-klippy``: Moonraker is connected but Klipper is not ready (shutdown, error, startup).
- ``printer-telemetry-stale``: connected, but no update for ``STALE_AFTER_S``.
- ``printer-failing``: at least ``FAILING_MIN_PRINTS`` prints in the warden's last 7 days and at least
  ``FAILING_RATE`` of them failed (a person's label wins over how the job ended).
- ``printer-maintenance``: maintenance items the warden reports as due.

A Klipper finding names the shutdown's classified cause and the warden's hint when the status carries
``last_shutdown`` (print-warden 0.4+).

Klipper state and telemetry age are only observed for a connected printer, so an offline printer's klippy or stale
finding stands until the printer is back and seen healthy. Failures and maintenance come from the warden's ledger,
so they are observed for every printer whose status carries them, connected or not; an older warden that does not
report them observes nothing in those categories.

The status lists every printer the warden has, so a printer that has left it is observed gone: its open findings in
every printer category resolve on the next successful read.

Targets are ``("service", "print-warden")`` and ``("service", "print-warden/<printer>")``. No playbook maps this kind:
these findings report, they never draft a remediation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.engine.category_findings import CategoryIssue, reconcile_category_findings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

KIND = FindingKind.SERVICE_UNHEALTHY
SERVICE = "print-warden"
UNREACHABLE = "printwarden-unreachable"
PRINTER_CATEGORIES = (
    "printer-offline",
    "printer-klippy",
    "printer-telemetry-stale",
    "printer-failing",
    "printer-maintenance",
)
LEDGER_CATEGORIES = ("printer-failing", "printer-maintenance")
STALE_AFTER_S = 600
FAILING_MIN_PRINTS = 3
FAILING_RATE = 0.5


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
        issues += _ledger_issues(name, target, p, evidence)
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
        shutdown = p.get("last_shutdown") or None
        if klippy not in ("ready", None):
            cause = str(shutdown["cause"]).replace("_", " ") if shutdown else None
            issues.append(
                CategoryIssue(
                    kind=KIND,
                    category="printer-klippy",
                    target_type="service",
                    target_id=target,
                    severity=FindingSeverity.HIGH
                    if klippy == "shutdown"
                    else FindingSeverity.MEDIUM,
                    title=f"Klipper on {name} is {klippy}" + (f": {cause}" if cause else ""),
                    description=(
                        f"Moonraker is reachable but Klipper reports {klippy!r}; the printer cannot print "
                        "until it is restarted. "
                        + (
                            f"print-warden classified the shutdown as {cause}: {shutdown['hint']}."
                            if shutdown
                            else "print-warden's events carry the classified shutdown reason."
                        )
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


def _ledger_issues(
    name: str, target: str, p: dict[str, Any], evidence: dict[str, Any]
) -> list[CategoryIssue]:
    out: list[CategoryIssue] = []
    seen = p.get("outcomes_7d") or {}
    prints, failed = int(seen.get("prints") or 0), int(seen.get("failed") or 0)
    if prints >= FAILING_MIN_PRINTS and failed / prints >= FAILING_RATE:
        out.append(
            CategoryIssue(
                kind=KIND,
                category="printer-failing",
                target_type="service",
                target_id=target,
                severity=FindingSeverity.MEDIUM,
                title=f"{failed} of {prints} prints on {name} failed this week",
                description=(
                    f"print-warden recorded {failed} failed prints out of {prints} on {name} in the last 7 days. "
                    "printer_stats there has the reasons."
                ),
                evidence=evidence,
            )
        )
    if due := [str(i) for i in p.get("maintenance_due") or []]:
        out.append(
            CategoryIssue(
                kind=KIND,
                category="printer-maintenance",
                target_type="service",
                target_id=target,
                severity=FindingSeverity.LOW,
                title=f"Maintenance due on {name}: {', '.join(due)}",
                description=(
                    f"print-warden counts {', '.join(due)} on {name} as due; marking it done there "
                    "(the phone's Done button or record_maintenance_done) resolves this."
                ),
                evidence=evidence,
            )
        )
    return out


async def _removed_printers(session: AsyncSession, names: set[str]) -> set[tuple[str, str]]:
    """Targets of open printer findings whose printer the warden no longer lists."""
    rows = (
        await session.execute(
            select(ReconciliationFinding.affected).where(
                ReconciliationFinding.kind == KIND,
                ReconciliationFinding.status.in_((FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED)),
            )
        )
    ).scalars()
    prefix = f"{SERVICE}/"
    return {
        ("service", tid)
        for affected in rows
        for a in affected or []
        if (tid := str(a.get("target_id") or "")).startswith(prefix)
        and tid.removeprefix(prefix) not in names
    }


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
    reporting = {
        ("service", _printer_target(name))
        for name, p in (status.get("printers") or {}).items()
        if "outcomes_7d" in p
    }
    gone = await _removed_printers(session, set(status.get("printers") or {}))
    connected |= gone
    reporting |= gone
    passes: tuple[tuple[set[str], set[tuple[str, str]] | None], ...] = (
        ({UNREACHABLE, "printer-offline"}, None),
        ({"printer-klippy", "printer-telemetry-stale"}, connected),
        (set(LEDGER_CATEGORIES), reporting),
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
