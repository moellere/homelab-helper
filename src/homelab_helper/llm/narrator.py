"""Narrator agent — findings → prose. The lowest-stakes agent (Tiny/Small).

Takes finding rows (already reconciled — fingerprint, severity, kind, title,
description, proposed actions) and asks the router for a plain-language
narration: what's wrong, why it matters, what the operator could do about it.
The narration cites fingerprints so the reader can jump to ``helper findings``
for the canonical record — the LLM's prose is presentation, never the source
of truth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homelab_helper.llm.router import TaskClass

if TYPE_CHECKING:
    from homelab_helper.db.models import ReconciliationFinding
    from homelab_helper.llm.router import LLMRouter, RouterResult

_SYSTEM = (
    "You are the narrator for a homelab audit tool. You receive reconciled "
    "findings from the tool's database — these are facts, not suggestions. "
    "Explain them in clear prose for the operator: what is wrong, why it "
    "matters, and what they could do next. Reference each finding by its "
    "fingerprint so the operator can look it up. Group related findings. "
    "Do not invent findings, numbers, or hosts that are not in the input. "
    "The tool only proposes; it never applies changes — phrase next steps as "
    "things the operator may choose to do."
)


def _render_finding(f: ReconciliationFinding) -> str:
    lines = [
        f"fingerprint: {f.fingerprint}",
        f"severity: {f.severity.value}   kind: {f.kind.value}   status: {f.status.value}",
        f"title: {f.title}",
        f"description: {f.description}",
    ]
    if f.proposed_actions:
        actions = "; ".join(str(a.get("summary", a)) for a in f.proposed_actions)
        lines.append(f"proposed actions: {actions}")
    if f.affected:
        targets = ", ".join(f"{a.get('target_type')}:{a.get('target_id')}" for a in f.affected)
        lines.append(f"affected: {targets}")
    return "\n".join(lines)


async def narrate_findings(
    router: LLMRouter,
    findings: list[ReconciliationFinding],
    *,
    question: str | None = None,
) -> RouterResult:
    """Narrate findings; with ``question`` set, answer it from those findings."""
    body = "\n\n---\n\n".join(_render_finding(f) for f in findings)
    if question:
        prompt = f"The operator asks: {question}\n\nAnswer from these findings:\n\n{body}"
    else:
        prompt = f"Narrate these findings for the operator:\n\n{body}"
    return await router.complete(
        TaskClass.NARRATION, _SYSTEM, [{"role": "user", "content": prompt}]
    )


def narration_context(findings: list[ReconciliationFinding]) -> dict[str, Any]:
    """Metadata the caller can print alongside the prose (provenance footer)."""
    return {
        "findings": len(findings),
        "fingerprints": [f.fingerprint for f in findings],
    }


__all__ = ["narrate_findings", "narration_context"]
