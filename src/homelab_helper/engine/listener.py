"""Approval listener — asks the operator about agent- and playbook-drafted proposals.

Phase 7 slice 3. ``execute_proposal`` on the MCP surface asks for one proposal
while an agent waits. This is the operator-side counterpart for everything
drafted without an MCP session attached — a playbook pass, a cloud agent — so
those proposals still reach the phone. One pass:

1. pending action proposals whose ``proposed_by`` has one of the listened
   prefixes (``playbook:``, ``agent:`` by default);
2. skip any that have already been asked (a ``TrustHistory`` approval event
   exists for the proposal — a denial or a timeout is an answer, and is not
   re-asked);
3. skip any the policy would refuse outright (PROPOSE / BLOCK, decided
   pessimistically) — nothing to ask;
4. run the rest through the executor with the approval channel as the
   confirmer. The executor's gate, receipts and escalation apply unchanged.

Never consulted for anything above CONFIRM's authority; never passes an
override. The listener is a trigger, like the MCP tool, not an authority.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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

DEFAULT_SOURCES: tuple[str, ...] = ("playbook:", "agent:")

AdaptersFor = Callable[[ActionManifest], Awaitable["tuple[Any, Any, Any, Any] | str"]]
"""Resolve ``(proxmox, k8s, argocd, unifi)`` for one manifest, or a message naming what is missing."""


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


async def _asked_before(session: AsyncSession, proposal: ProposalLog) -> bool:
    row = (
        await session.execute(
            select(TrustHistory.id)
            .where(TrustHistory.proposal_id == proposal.id, TrustHistory.event == "approval")
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


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

    for proposal in candidates:
        pid = str(proposal.id)
        if await _asked_before(session, proposal):
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
        proxmox, k8s, argocd, unifi = resolved

        async def _confirm(m: ActionManifest, d: Decision, _pid: str = pid) -> ApprovalResult:
            result.asked.append(_pid[:8])
            return await channel.request(m, d, proposal_id=_pid)

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
            )
        except ExecutionRefused as exc:
            result.declined.append(f"{pid[:8]}: {exc}")
            continue
        finally:
            for a in (proxmox, argocd, unifi):
                close = getattr(a, "aclose", None)
                if close is not None:
                    await close()
        result.executed.append(f"{pid[:8]} {manifest.cell_key} -> {outcome.outcome}")
    return result


__all__ = ["DEFAULT_SOURCES", "AdaptersFor", "ListenerResult", "ask_pending"]
