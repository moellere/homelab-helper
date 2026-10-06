"""Rightsizing (Phase 8.4) — usage history → ``RIGHTSIZING`` findings with a proposed value.

Every recommendation names the allocation, the observed p95 and peak, the
window, and the proposed value (acceptance criterion 5). A guest with fewer
than ``MIN_SAMPLES`` hourly buckets in the window gets no verdict and is not
counted as observed, so its earlier findings neither open nor resolve.

Rules (thresholds are module constants, deliberately conservative):

- ``cpu-grow`` — CPU p95 ≥ 60% of allocated cores: propose enough cores that
  the p95 would sit near 50%. MEDIUM when p95 ≥ 80% or the peak saturated.
- ``cpu-shrink`` — two or more cores and a *peak* under 30%: propose fewer.
  Peak, not p95, so bursty guests (builders) are left alone.
- ``mem-shrink`` — propose the peak plus 25%, rounded up to 512 MiB, when that
  frees at least 1 GiB and the allocation is at least 1.5x the proposal.
- ``mem-grow`` — **containers only**: p95 ≥ 90% of the allocation. A QEMU VM's
  reported memory includes the guest's page cache (Proxmox reads it from the
  balloon driver), so it reads near-full for any busy Linux guest; growing on
  it would be wrong, shrinking on it is merely conservative.
- ``idle`` — CPU peak under 5% and under 2 KiB/s of network for the whole
  window: a candidate to stop or retire.

Findings only. Changing a guest's cores or memory becomes an executable action
kind (behind the trust gate, with a captured prior config to roll back to) in a
later slice.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.db.models import Cluster, VirtualMachine
from homelab_helper.engine.category_findings import (
    CategoryIssue,
    CategoryResult,
    reconcile_category_findings,
)
from homelab_helper.engine.usage import HOUR, summarize

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

CATEGORIES = ("cpu-grow", "cpu-shrink", "mem-grow", "mem-shrink", "idle")
MIN_SAMPLES = 7 * 24
CPU_GROW_P95 = 0.60
CPU_TARGET = 0.50
CPU_HOT_P95 = 0.80
CPU_SHRINK_PEAK = 0.30
CPU_SHRINK_TARGET = 0.60
MEM_HEADROOM = 1.25
MEM_STEP = 512 * 1024**2
MEM_MIN_SAVING = 1024**3
MEM_SHRINK_RATIO = 1.5
MEM_GROW_P95 = 0.90
IDLE_CPU_PEAK = 0.05
IDLE_NET_BYTES = 2048.0
_GIB = 1024**3


def _issue(**kw: Any) -> CategoryIssue:
    return CategoryIssue(kind=FindingKind.RIGHTSIZING, target_type="guest", **kw)


def _gib(x: float) -> str:
    return f"{x / _GIB:.1f} GiB"


def _round_up(value: float, step: int) -> int:
    return max(step, math.ceil(value / step) * step)


def guest_issues(
    key: str, name: str, kind: str, s: dict[str, Any], window_days: int, node: str | None = None
) -> list[CategoryIssue]:
    """Rules for one guest's summary; ``s`` comes from ``engine.usage.summarize``.

    Every issue's evidence also carries the guest's identity (node, vmid, kind)
    so the ``rightsize`` playbook can draft an executable resize from it.
    """
    issues = _guest_rules(key, name, kind, s, window_days)
    vmid = int(key.rsplit("/", 1)[-1])
    return [
        CategoryIssue(
            kind=i.kind,
            category=i.category,
            target_type=i.target_type,
            target_id=i.target_id,
            severity=i.severity,
            title=i.title,
            description=i.description,
            evidence={**i.evidence, "node": node, "vmid": vmid, "vm_kind": kind, "name": name},
        )
        for i in issues
    ]


def _guest_rules(
    key: str, name: str, kind: str, s: dict[str, Any], window_days: int
) -> list[CategoryIssue]:
    issues: list[CategoryIssue] = []
    label = f"{name} ({key.rsplit('/', 1)[-1]})"
    cpus, cpu_p95, cpu_peak = s.get("cpus"), s.get("cpu_p95"), s.get("cpu_peak")
    if cpus and cpu_p95 is not None and cpu_peak is not None:
        evidence = {
            "allocated_cores": cpus,
            "cpu_p95": round(cpu_p95, 3),
            "cpu_peak": round(cpu_peak, 3),
            "window_days": window_days,
        }
        if cpu_p95 >= CPU_GROW_P95:
            proposed = max(int(cpus) + 1, math.ceil(cpus * cpu_p95 / CPU_TARGET))
            hot = cpu_p95 >= CPU_HOT_P95 or cpu_peak >= 1.0
            issues.append(
                _issue(
                    category="cpu-grow",
                    target_id=key,
                    severity=FindingSeverity.MEDIUM if hot else FindingSeverity.LOW,
                    title=f"{label}: CPU-bound on {cpus:g} core(s) — propose {proposed}",
                    description=(
                        f"Over {window_days} days {label} used {cpu_p95:.0%} of its {cpus:g} core(s) "
                        f"at p95 and peaked at {cpu_peak:.0%}. {proposed} cores would put the p95 "
                        f"near {CPU_TARGET:.0%}."
                    ),
                    evidence={**evidence, "proposed_cores": proposed},
                )
            )
        elif cpus >= 2 and cpu_peak < CPU_SHRINK_PEAK:  # noqa: PLR2004 - a one-core guest has nothing to give
            proposed = max(1, math.ceil(cpus * cpu_peak / CPU_SHRINK_TARGET))
            if proposed < cpus:
                issues.append(
                    _issue(
                        category="cpu-shrink",
                        target_id=key,
                        severity=FindingSeverity.LOW,
                        title=f"{label}: {cpus:g} cores, never above {cpu_peak:.0%} — propose {proposed}",
                        description=(
                            f"Over {window_days} days {label} peaked at {cpu_peak:.0%} of {cpus:g} core(s) "
                            f"(p95 {cpu_p95:.0%}). {proposed} core(s) would cover that peak with headroom."
                        ),
                        evidence={**evidence, "proposed_cores": proposed},
                    )
                )

    total, mem_p95, mem_peak = s.get("mem_total"), s.get("mem_p95"), s.get("mem_peak")
    if total and mem_peak is not None and mem_p95 is not None:
        evidence = {
            "allocated_bytes": total,
            "mem_p95_bytes": round(mem_p95),
            "mem_peak_bytes": round(mem_peak),
            "window_days": window_days,
            "guest_kind": kind,
        }
        proposed_mem = _round_up(mem_peak * MEM_HEADROOM, MEM_STEP)
        caveat = (
            " For a VM this figure includes the guest's page cache, so real use is lower still."
            if kind == "qemu"
            else ""
        )
        if total - proposed_mem >= MEM_MIN_SAVING and total >= MEM_SHRINK_RATIO * proposed_mem:
            issues.append(
                _issue(
                    category="mem-shrink",
                    target_id=key,
                    severity=FindingSeverity.LOW,
                    title=f"{label}: {_gib(total)} allocated, peak {_gib(mem_peak)} — propose {_gib(proposed_mem)}",
                    description=(
                        f"Over {window_days} days {label} peaked at {_gib(mem_peak)} (p95 {_gib(mem_p95)}) "
                        f"of {_gib(total)}. {_gib(proposed_mem)} keeps {MEM_HEADROOM - 1:.0%} above the peak "
                        f"and frees {_gib(total - proposed_mem)}.{caveat}"
                    ),
                    evidence={**evidence, "proposed_bytes": proposed_mem},
                )
            )
        elif kind == "lxc" and mem_p95 >= MEM_GROW_P95 * total:
            grown = _round_up(max(mem_peak, mem_p95) * MEM_HEADROOM, MEM_STEP)
            issues.append(
                _issue(
                    category="mem-grow",
                    target_id=key,
                    severity=FindingSeverity.MEDIUM,
                    title=f"{label}: memory at {mem_p95 / total:.0%} p95 — propose {_gib(grown)}",
                    description=(
                        f"Over {window_days} days container {label} used {_gib(mem_p95)} at p95 "
                        f"(peak {_gib(mem_peak)}) of {_gib(total)}."
                    ),
                    evidence={**evidence, "proposed_bytes": grown},
                )
            )

    net = s.get("net_mean")
    if (
        cpu_peak is not None
        and cpu_peak < IDLE_CPU_PEAK
        and net is not None
        and net < IDLE_NET_BYTES
    ):
        issues.append(
            _issue(
                category="idle",
                target_id=key,
                severity=FindingSeverity.LOW,
                title=f"{label}: idle for {window_days} days",
                description=(
                    f"{label} never exceeded {cpu_peak:.1%} CPU and averaged {net:.0f} B/s of network "
                    f"over {window_days} days — a candidate to stop or retire."
                ),
                evidence={
                    "cpu_peak": round(cpu_peak, 4),
                    "net_mean": round(net),
                    "window_days": window_days,
                },
            )
        )
    return issues


async def evaluate(
    session: AsyncSession, *, window: timedelta = timedelta(days=30), now: datetime | None = None
) -> tuple[list[CategoryIssue], set[tuple[str, str]], list[str]]:
    """``(issues, evaluated targets, skipped guest labels)`` over running guests."""
    rows = (
        await session.execute(
            select(VirtualMachine, Cluster).join(Cluster, Cluster.id == VirtualMachine.cluster_id)
        )
    ).all()
    issues: list[CategoryIssue] = []
    evaluated: set[tuple[str, str]] = set()
    skipped: list[str] = []
    days = max(1, round(window.total_seconds() / 86400))
    for vm, cluster in rows:
        if vm.status != "running":
            continue
        key = f"{cluster.name}/{vm.vmid}"
        s = await summarize(
            session, subject_type="guest", subject_key=key, window=window, resolution=HOUR, now=now
        )
        if (s.get("samples") or 0) < MIN_SAMPLES:
            skipped.append(f"{vm.name} ({vm.vmid}): {s.get('samples', 0)} hourly bucket(s)")
            continue
        evaluated.add(("guest", key))
        issues += guest_issues(key, vm.name or key, vm.kind or "qemu", s, days, vm.node_name)
    return issues, evaluated, skipped


async def reconcile_rightsizing(
    session: AsyncSession,
    *,
    window: timedelta = timedelta(days=30),
    now: datetime | None = None,
) -> tuple[CategoryResult, list[CategoryIssue], list[str]]:
    when = now or datetime.now(UTC)
    issues, evaluated, skipped = await evaluate(session, window=window, now=when)
    result = await reconcile_category_findings(
        session,
        FindingKind.RIGHTSIZING,
        issues,
        set(CATEGORIES),
        when=when,
        observed_targets=evaluated,
    )
    return result, issues, skipped


__all__ = ["CATEGORIES", "MIN_SAMPLES", "evaluate", "guest_issues", "reconcile_rightsizing"]
