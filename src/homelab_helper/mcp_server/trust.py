"""The read-only trust surface: cell levels, receipts, what policy would allow. No tool here changes authority."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from homelab_helper.config import database_url
from homelab_helper.db.enums import (
    ProposalOutcome,
)
from homelab_helper.db.models import (
    CellTrust,
    Domain,
    ExecutionReceipt,
    Host,
    ProposalLog,
    TrustBoundary,
)
from homelab_helper.db.session import make_engine, make_sessionmaker
from homelab_helper.engine.escalation import PROMOTION_STREAK, is_promotable
from homelab_helper.engine.executor import (
    ManifestError,
    parse_manifest,
)
from homelab_helper.engine.trust import decide, load_trust_context, open_windows
from homelab_helper.mcp_server._app import (
    _iso,
    server,
)


@server.tool()
async def trust_status() -> dict[str, Any]:
    """Show the trust gradient: domains, granted cells, boundaries, windows.

    Read-only. Nothing here can change authority — use the CLI for that.
    """
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            domains = (await session.execute(select(Domain))).scalars().all()
            cells = (await session.execute(select(CellTrust))).scalars().all()
            boundaries = (
                (
                    await session.execute(
                        select(TrustBoundary, Host.hostname).join(
                            Host, Host.id == TrustBoundary.host_id, isouter=True
                        )
                    )
                )
                .tuples()
                .all()
            )
            live = await open_windows(session)
            return {
                "domains": [
                    {
                        "name": d.name.value,
                        "default_level": d.default_level.value,
                        "max_level": d.max_level.value,
                        "absolute": d.is_absolute,
                    }
                    for d in domains
                ],
                "cells": [
                    {
                        "cell": f"{c.domain.value}/{c.action_kind}/{c.blast_radius}",
                        "level": c.level.value,
                        "granted_by": c.granted_by,
                        "clean_streak": c.clean_streak,
                        "promotion_streak": PROMOTION_STREAK,
                        "on_probation": c.on_probation,
                        "auto_promotable": is_promotable(c.action_kind, c.blast_radius),
                    }
                    for c in cells
                ],
                "boundaries": [
                    {
                        "hostname": hostname,
                        "max_agent_authority": b.max_agent_authority.value,
                        "absolute": b.absolute,
                    }
                    for b, hostname in boundaries
                ],
                "open_windows": [
                    {
                        "id": str(w.id),
                        "reason": w.reason,
                        "opened_by": w.opened_by,
                        "expires_at": _iso(w.expires_at),
                        "scope": w.scope,
                    }
                    for w in live
                ],
                "note": (
                    "read-only view; grants, windows, overrides and execution "
                    "are operator gestures at the CLI"
                ),
            }
    finally:
        await engine.dispose()


@server.tool()
async def list_receipts(limit: int = 20) -> list[dict[str, Any]]:
    """Recent execution receipts — what actually ran, at what level, and how it ended."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            rows = (
                (
                    await session.execute(
                        select(ExecutionReceipt)
                        .order_by(ExecutionReceipt.executed_at.desc())
                        .limit(max(1, min(limit, 200)))
                    )
                )
                .scalars()
                .all()
            )
            return [
                {
                    "id": str(r.id),
                    "executed_at": _iso(r.executed_at),
                    "actor": r.actor,
                    "decision_level": r.decision_level.value,
                    "decision_reasons": r.decision_reasons,
                    "action": r.action,
                    "outcome": r.outcome,
                    "error": r.error,
                    "duration_ms": r.duration_ms,
                    "window_id": str(r.window_id) if r.window_id else None,
                    "rollback_state": r.rollback_state,
                    "approval": r.approval,
                    "rolled_back_at": _iso(r.rolled_back_at),
                    "rollback_receipt_id": (
                        str(r.rollback_receipt_id) if r.rollback_receipt_id else None
                    ),
                }
                for r in rows
            ]
    finally:
        await engine.dispose()


@server.tool()
async def pending_actions() -> list[dict[str, Any]]:
    """Pending action proposals and what policy would say about each.

    The decision shown is computed **pessimistically** — as if reversibility
    could not be verified — because verifying it means probing the target, and
    a read-only query tool has no business touching infrastructure. A real run
    may therefore land one level higher. Nothing here executes anything.
    """
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            proposals = (
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
            out: list[dict[str, Any]] = []
            for proposal in proposals:
                if (proposal.artifact or {}).get("kind") != "action":
                    continue
                entry: dict[str, Any] = {
                    "id": str(proposal.id),
                    "title": proposal.title,
                    "proposed_by": proposal.proposed_by,
                    "blast_radius": proposal.blast_radius,
                }
                try:
                    manifest = parse_manifest(proposal)
                except ManifestError as exc:
                    entry["error"] = str(exc)
                    out.append(entry)
                    continue
                action = manifest.request(proposal.proposed_by)
                decision = decide(action, await load_trust_context(session, action))
                entry.update(
                    {
                        "cell": manifest.cell_key,
                        "target": manifest.target_label,
                        "decision_if_run_now": decision.level.value,
                        "decision_reasons": list(decision.reasons),
                        "decision_basis": "pessimistic: rollback treated as unverified",
                    }
                )
                out.append(entry)
            return out
    finally:
        await engine.dispose()


# ------------------------------------------------------ proposals (agent-side)
#
# An agent may *draft* an action; it may never authorize one. propose_action
# writes a PENDING ProposalLog row and reports what the gradient would say
# about it — the operator then runs `helper exec run <id>` (or rejects it).
# Nothing here dispatches, grants, or lifts a floor.
