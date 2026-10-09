"""Approval listener — asks the operator about agent- and playbook-drafted proposals.

Phase 7 slice 3. ``execute_proposal`` on the MCP surface asks for one proposal
while an agent waits. This is the operator-side counterpart for everything
drafted without an MCP session attached — a playbook pass, a cloud agent — so
those proposals still reach the phone. One pass:

1. pending action proposals whose ``proposed_by`` has one of the listened
   prefixes (``playbook:``, ``agent:`` by default);
2. skip any that were answered (a ``TrustHistory`` approval event with a
   responder — Approve or Deny is final). A prompt that *expired* is not an
   answer: it is asked again once ``REASK_AFTER`` has passed, up to
   ``MAX_ASKS`` times in all, because a missed notification is not a "no";
3. skip any the policy would refuse outright (PROPOSE / BLOCK, decided
   pessimistically) — nothing to ask;
4. ask about every remaining proposal **at once** — one prompt each, all on
   the phone together, all waiting the same window — then
5. run the answered ones through the executor, one at a time, with the
   prompt's answer as the confirmer. The executor's gate, receipts and
   escalation apply unchanged; it records the answer on the audit spine.

Never consulted for anything above CONFIRM's authority; never passes an
override. The listener is a trigger, like the MCP tool, not an authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import AutonomyLevel, ProposalOutcome
from homelab_helper.db.models import ProposalLog, TrustHistory
from homelab_helper.engine.executor import (
    ActionManifest,
    ExecutionRefused,
    ManifestError,
    execute_proposal,
    parse_manifest,
)
from homelab_helper.engine.trust import ActionRequest, Decision, decide, load_trust_context

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.engine.approval import ApprovalChannel, ApprovalResult
    from homelab_helper.engine.notify import Notifier

DEFAULT_SOURCES: tuple[str, ...] = ("playbook:", "agent:")
REASK_AFTER = timedelta(hours=2)
MAX_ASKS = 3

AdaptersFor = Callable[[ActionManifest], Awaitable["tuple[Any, Any, Any, Any, Any] | str"]]
"""Resolve ``(proxmox, k8s, argocd, unifi, ssh)`` for one manifest, or a message naming what is missing."""


@dataclass
class ListenerResult:
    asked: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    """Policy said PROPOSE / BLOCK — nothing to ask."""
    already_asked: int = 0
    unconfigured: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def _ask_state(session: AsyncSession, proposal: ProposalLog, now: datetime) -> str:
    """``ask`` | ``answered`` | ``waiting`` (expired recently, or out of asks)."""
    events = (
        (
            await session.execute(
                select(TrustHistory)
                .where(TrustHistory.proposal_id == proposal.id, TrustHistory.event == "approval")
                .order_by(TrustHistory.at)
            )
        )
        .scalars()
        .all()
    )
    if not events:
        return "ask"
    if any((e.detail or {}).get("responder") or (e.detail or {}).get("approved") for e in events):
        return "answered"
    if len(events) >= MAX_ASKS:
        return "waiting"
    last = events[-1].at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return "ask" if now - last >= REASK_AFTER else "waiting"


async def _pessimistic(session: AsyncSession, proposal: ProposalLog, m: ActionManifest) -> Decision:
    action = ActionRequest(
        domain=m.domain,
        action_kind=m.action_kind,
        blast_radius=m.blast_radius,
        hostnames=m.hostnames,
        rollback_verified=False,
        provenance=proposal.proposed_by,
    )
    return decide(action, await load_trust_context(session, action))


async def ask_pending(
    session: AsyncSession,
    *,
    channel: ApprovalChannel,
    adapters_for: AdaptersFor,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    actor: str = "listener",
    limit: int = 20,
    notifier: Notifier | None = None,
) -> ListenerResult:
    """One listener pass over pending proposals from the listened sources."""
    result = ListenerResult()
    rows = (
        (
            await session.execute(
                select(ProposalLog)
                .where(ProposalLog.outcome == ProposalOutcome.PENDING)
                .order_by(ProposalLog.proposed_at)
            )
        )
        .scalars()
        .all()
    )
    candidates = [
        p
        for p in rows
        if (p.artifact or {}).get("kind") == "action"
        and any(p.proposed_by.startswith(src) for src in sources)
    ][:limit]

    now = datetime.now(UTC)
    ready: list[tuple[ProposalLog, ActionManifest, Decision, tuple[Any, Any, Any, Any, Any]]] = []
    for proposal in candidates:
        pid = str(proposal.id)
        if await _ask_state(session, proposal, now) != "ask":
            result.already_asked += 1
            continue
        try:
            manifest = parse_manifest(proposal)
        except ManifestError as exc:
            result.errors.append(f"{pid[:8]}: {exc}")
            continue
        decision = await _pessimistic(session, proposal, manifest)
        if decision.level in (AutonomyLevel.PROPOSE, AutonomyLevel.BLOCK):
            result.refused.append(f"{pid[:8]} {manifest.cell_key} ({decision.level.value})")
            continue
        resolved = await adapters_for(manifest)
        if isinstance(resolved, str):
            result.unconfigured.append(f"{pid[:8]}: {resolved}")
            continue
        ready.append((proposal, manifest, decision, resolved))

    # Every prompt goes out together; each waits the channel's full window.
    async def _ask(item: tuple[ProposalLog, ActionManifest, Decision, Any]) -> ApprovalResult:
        proposal, manifest, decision, _ = item
        return await channel.request(
            manifest,
            decision,
            proposal_id=str(proposal.id),
            title=proposal.title,
            why=_why(proposal),
        )

    answers = await asyncio.gather(*(_ask(item) for item in ready), return_exceptions=True)

    try:
        for (proposal, _manifest, _decision, adapters), answer in zip(ready, answers, strict=True):
            pid = str(proposal.id)
            if isinstance(answer, BaseException):
                result.errors.append(f"{pid[:8]}: approval channel failed: {answer}")
                continue
            result.asked.append(pid[:8])
            proxmox, k8s, argocd, unifi, ssh = adapters

            async def _confirm(
                m: ActionManifest, d: Decision, _answer: ApprovalResult = answer
            ) -> ApprovalResult:
                return _answer

            try:
                outcome = await execute_proposal(
                    session,
                    proposal,
                    proxmox,
                    actor=actor,
                    confirm_cb=_confirm,
                    override=None,
                    k8s_adapter=k8s,
                    argocd_adapter=argocd,
                    unifi_adapter=unifi,
                    ssh_adapter=ssh,
                    notifier=notifier,
                )
            except ExecutionRefused as exc:
                result.declined.append(f"{pid[:8]}: {exc}")
                continue
            note = f" ({outcome.notification})" if outcome.notification else ""
            result.executed.append(f"{pid[:8]} {_manifest.cell_key} -> {outcome.outcome}{note}")
    finally:
        for _p, _m, _d, (proxmox, _k8s, argocd, unifi, _ssh) in ready:
            for a in (proxmox, argocd, unifi):
                close = getattr(a, "aclose", None)
                if close is not None:
                    await close()
    return result


def _why(proposal: ProposalLog) -> str | None:
    """The finding's first sentence, from a playbook draft's description."""
    text = (proposal.description or "").strip()
    if not text:
        return None
    last = [line for line in text.splitlines() if line.strip()][-1]
    return last.split(". ")[0]


__all__ = [
    "DEFAULT_SOURCES",
    "MAX_ASKS",
    "REASK_AFTER",
    "AdaptersFor",
    "ListenerResult",
    "ask_pending",
]
