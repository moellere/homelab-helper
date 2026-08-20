"""Planner agent — narrates placement reports (Phase 5).

Per the locked architecture split: the *solver/recommender is deterministic
Python; the LLM is the explainer*. This module receives a finished
:class:`PlacementReport` — ranked candidates with reasons, rejections with
reasons — and asks the router to turn it into operator prose. It cannot
reorder, add, or drop candidates; the report is the record.

Planning is Mid/Frontier tier, so on a small-local-only setup the router's
refusal (with options) is the expected behavior — the deterministic table has
already been printed by then, so narration is enhancement, never a gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homelab_helper.llm.router import TaskClass

if TYPE_CHECKING:
    from homelab_helper.engine.placement import PlacementReport
    from homelab_helper.engine.rebalance import RebalanceReport
    from homelab_helper.engine.workloads import WorkloadProfile
    from homelab_helper.llm.router import LLMRouter, RouterResult

_SYSTEM = (
    "You are the planner narrator for a homelab framework. You receive a "
    "deterministic placement analysis: a workload's requirements and every "
    "host either ranked (with scoring reasons) or rejected (with the "
    "constraint it failed). Explain the recommendation in prose: where to "
    "run it, why, what the runner-ups trade off, and what the caveats mean "
    "practically. Do not invent hosts, change the ranking, or soften hard "
    "constraint failures. The framework only proposes — the operator "
    "deploys by hand."
)


def _render(profile: WorkloadProfile, report: PlacementReport) -> str:
    lines = [
        f"WORKLOAD: {profile.name} — {profile.description}",
        f"baseline: {profile.cpu_cores} cores, {profile.ram_mb} MB RAM, "
        f"{profile.storage_gb} GB footprint; arch: {'/'.join(profile.arch)}; "
        f"gpu: {profile.gpu}" + (f" ({profile.gpu_purpose})" if profile.gpu_purpose else ""),
    ]
    if profile.depends_on:
        lines.append(f"depends on: {', '.join(profile.depends_on)}")
    if profile.data_gravity:
        lines.append(f"data gravity: {profile.data_gravity}")
    lines.append("\nRANKED CANDIDATES:")
    for i, c in enumerate(report.candidates, 1):
        lines.append(f"{i}. {c.hostname} (score {c.score:.1f})")
        lines.extend(f"   + {r}" for r in c.reasons)
        lines.extend(f"   ! {c}" for c in c.caveats)
    if not report.candidates:
        lines.append("(none — every host was rejected)")
    if report.rejected:
        lines.append("\nREJECTED:")
        lines.extend(f"- {host}: {reason}" for host, reason in report.rejected)
    return "\n".join(lines)


async def narrate_placement(
    router: LLMRouter, profile: WorkloadProfile, report: PlacementReport
) -> RouterResult:
    prompt = f"Narrate this placement analysis for the operator:\n\n{_render(profile, report)}"
    return await router.complete(TaskClass.PLANNING, _SYSTEM, [{"role": "user", "content": prompt}])


def _render_rebalance(report: RebalanceReport) -> str:
    lines = ["FLEET LOAD:"]
    for h in report.hosts:
        ratio = f"{h.ratio:.0%}" if h.ratio is not None else "?"
        lines.append(
            f"- {h.hostname}: {h.committed / 1024**3:.0f} GiB committed of "
            f"{(h.mem_total or 0) / 1024**3:.0f} GiB ({ratio}), {len(h.vms)} running VM(s)"
        )
    for name in report.unknown_hosts:
        lines.append(f"- {name}: RAM unknown (not deep-probed) — excluded from the math")
    lines.append(f"commitment spread: {report.spread:.0%}")
    for i, plan in enumerate(report.plans, 1):
        lines.append(f"\nPLAN {i} — {plan.name}: {plan.summary}")
        lines.extend(f"  step: {s.description}" for s in plan.steps)
        lines.extend(f"  tradeoff: {t}" for t in plan.tradeoffs)
        after = ", ".join(f"{host} {ratio:.0%}" for host, ratio in plan.resulting_ratios.items())
        lines.append(f"  resulting load: {after}")
    return "\n".join(lines)


async def narrate_rebalance(router: LLMRouter, report: RebalanceReport) -> RouterResult:
    prompt = (
        "Narrate these rebalancing options for the operator — compare the "
        f"plans' tradeoffs and recommend a starting point:\n\n{_render_rebalance(report)}"
    )
    return await router.complete(TaskClass.PLANNING, _SYSTEM, [{"role": "user", "content": prompt}])


__all__ = ["narrate_placement", "narrate_rebalance"]
