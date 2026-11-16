"""The Phase-7 triggers: execute_proposal and maintenance runs. Each calls the executor with override=None; authority stays with decide() and the operator."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.adapters import factories
from homelab_helper.adapters.kernel_ssh import KernelSSHAdapter
from homelab_helper.adapters.proxmox import ProxmoxConfigError
from homelab_helper.adapters.talos import TalosAdapter, TalosError
from homelab_helper.config import database_url
from homelab_helper.db.enums import (
    ProposalOutcome,
)
from homelab_helper.db.lookup import rows_by_id_prefix
from homelab_helper.db.models import (
    MaintenanceRun,
    ProposalLog,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.approval import (
    ApprovalConfigError,
    ApprovalError,
    approval_channel_from_env,
)
from homelab_helper.engine.executor import (
    ExecutionRefused,
    ManifestError,
    parse_manifest,
)
from homelab_helper.engine.executor import (
    execute_proposal as _run_proposal,
)
from homelab_helper.engine.maintenance import (
    MaintenanceError,
    execute_maintenance,
    plan_maintenance,
    plan_talos_maintenance,
    run_summary,
    talos_health_gate,
)
from homelab_helper.engine.notify import notifier_from_env

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from homelab_helper.mcp_server._app import (
    server,
)

_RUN_ASK_STEPS = 4
"""How many step names the one phone prompt for a maintenance run lists."""


async def _one_run(session: AsyncSession, ref: str) -> tuple[MaintenanceRun | None, str]:
    """A maintenance run by id or unique id prefix, or the reason there is none."""
    hits = await rows_by_id_prefix(session, MaintenanceRun, ref)
    if len(hits) == 1:
        return hits[0], ""
    if hits:
        return None, f"{ref!r} matches more than one maintenance run; give more of the id"
    return None, f"no maintenance run {ref!r}"


@server.tool()
async def plan_maintenance_run(
    node: str, target_node: str | None = None, resume_from: str | None = None
) -> dict[str, Any]:
    """Draft a maintenance run for one Proxmox node (Phase 10.4): one proposal per
    step — migrate (or shut down, for node-local disks) every running guest off it,
    apt dist-upgrade, reboot and wait for its return, then bring the guests back.
    `resume_from` names a stopped run on the same node whose drained guests this
    run brings back too. Harness-DB write only; nothing executes. Returns the run
    with its steps."""
    engine = make_engine(database_url())
    try:
        adapter = factories.proxmox()
    except ProxmoxConfigError as exc:
        return {"error": f"Proxmox adapter: {exc}"}
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            earlier = None
            if resume_from:
                earlier, why = await _one_run(session, resume_from)
                if earlier is None:
                    return {"error": why}
            try:
                run = await plan_maintenance(
                    session,
                    adapter,
                    node,
                    actor="agent:mcp",
                    target_node=target_node,
                    resume_from=earlier,
                )
            except MaintenanceError as exc:
                return {"error": str(exc)}
            return run_summary(run)
    finally:
        await adapter.aclose()
        await engine.dispose()


@server.tool()
async def plan_talos_maintenance_run(
    control_plane: str,
    version: str,
    kubernetes: str | None = None,
    talosconfig: str | None = None,
) -> dict[str, Any]:
    """Draft a Talos cluster upgrade as a maintenance run (Phase 11.3): one
    `talos-upgrade` per member read through `control_plane` — control planes
    first, then workers, members already on `version` (a tag like "v1.14.2")
    skipped — and, with `kubernetes` (like "1.37.2"), a closing `k8s-upgrade`.
    Between steps the run requires `talosctl health` to pass. Harness-DB write
    only; nothing executes."""
    engine = make_engine(database_url())
    talos = TalosAdapter(talosconfig=talosconfig)
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            try:
                run = await plan_talos_maintenance(
                    session, talos, control_plane, version, actor="agent:mcp", kubernetes=kubernetes
                )
            except (MaintenanceError, TalosError) as exc:
                return {"error": str(exc)}
            return run_summary(run)
    finally:
        await engine.dispose()


@server.tool()
async def list_maintenance_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Maintenance runs, newest first, each with its steps and their states."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            rows = (
                await session.execute(
                    select(MaintenanceRun).order_by(MaintenanceRun.created_at.desc()).limit(limit)
                )
            ).scalars()
            return [run_summary(r) for r in rows]
    finally:
        await engine.dispose()


@server.tool()
async def execute_maintenance_run(run_id: str) -> dict[str, Any]:
    """Run a planned maintenance run step by step through the executor (Phase 10.4).
    A trigger, not an authority: each step passes `decide()` on its own cell; the
    operator is asked ONCE on the phone for the whole run and that answer is
    relayed to every step at CONFIRM. The run stops at the first failed or refused
    step. node-reboot has no inverse, so a run never executes unattended."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            found, why = await _one_run(session, run_id)
            if found is None:
                return {"error": why}
            run: MaintenanceRun = found
            pending = [st for st in run.steps if st["state"] == "planned"]
            if not pending:
                return {"error": "nothing left to run", **run_summary(run)}
            try:
                channel = approval_channel_from_env()
            except ApprovalConfigError:
                channel = None
            answers: dict[str, Any] = {}

            async def _confirm(m: Any, decision: Any) -> Any:
                if channel is None:
                    raise ExecutionRefused(
                        "policy says CONFIRM and no approval channel is configured "
                        "(HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE) — an operator runs "
                        f"`helper maintenance run {str(run.id)[:8]}`",
                        decision,
                    )
                if "run" not in answers:
                    answers["run"] = await channel.request(
                        m,
                        decision,
                        proposal_id=str(run.id),
                        title=f"Maintenance {run.node}: {len(pending)} step(s)",
                        why=(
                            f"Run all {len(pending)} step(s) on {run.node} in order — "
                            + "; ".join(st["name"] for st in pending[:_RUN_ASK_STEPS])
                            + (" …" if len(pending) > _RUN_ASK_STEPS else "")
                            + ". Stops at the first failure"
                        ),
                    )
                return answers["run"]

            try:
                adapter = factories.proxmox()
            except ProxmoxConfigError as exc:
                return {"error": f"Proxmox adapter: {exc}"}
            k8s = factories.k8s() if shutil.which("kubectl") else None
            talos = TalosAdapter() if run.plane == "talos" else None
            try:
                run = await execute_maintenance(
                    session,
                    run,
                    adapter,
                    actor="agent:mcp",
                    confirm_cb=_confirm,
                    ssh_adapter=KernelSSHAdapter(),
                    k8s_adapter=k8s,
                    talos_adapter=talos,
                    health_cb=talos_health_gate(talos, run.node) if talos else None,
                    notifier=notifier_from_env(),
                )
            except MaintenanceError as exc:
                return {"error": str(exc)}
            except ApprovalError as exc:
                return {"refused": f"approval channel failed: {exc}"}
            finally:
                await adapter.aclose()
            return run_summary(run)
    finally:
        await engine.dispose()


@server.tool()
async def execute_proposal(proposal_id: str) -> dict[str, Any]:
    """Ask the executor to run one PENDING action proposal. This is a trigger,
    not an authority: the same deterministic `decide()` gate the CLI uses
    runs first. AUTONOMOUS executes; CONFIRM sends the operator an approval
    notification (Approve / Deny on their phone) and waits for the tap;
    PROPOSE and BLOCK execute nothing and return the policy reason plus the
    `helper exec run` command. No override is ever passed from here. Returns
    the receipt id and outcome, or `{"refused": ...}`."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            matches = await rows_by_id_prefix(
                session,
                ProposalLog,
                proposal_id,
                ProposalLog.outcome == ProposalOutcome.PENDING,
            )
            if len(matches) != 1:
                return {
                    "error": (
                        f"no pending proposal with id {proposal_id!r}"
                        if not matches
                        else f"id prefix {proposal_id!r} matches more than one pending proposal"
                    )
                }
            proposal = matches[0]
            try:
                manifest = parse_manifest(proposal)
            except ManifestError as exc:
                return {"error": f"invalid manifest: {exc}"}

            channel_note: str | None = None
            try:
                channel = approval_channel_from_env()
            except ApprovalConfigError as exc:
                channel = None
                channel_note = str(exc)

            async def _confirm(m: Any, decision: Any) -> Any:
                if channel is None:
                    raise ExecutionRefused(
                        "policy says CONFIRM and no approval channel is configured "
                        f"({channel_note}); an operator runs `helper exec run {proposal.id}`",
                        decision,
                    )
                return await channel.request(m, decision, proposal_id=str(proposal.id))

            adapters, problem = factories.for_action(manifest)
            if problem is not None:
                return {"error": problem}
            assert adapters is not None
            adapter, k8s, argocd, unifi, ssh, talos = adapters
            try:
                result = await _run_proposal(
                    session,
                    proposal,
                    adapter,
                    actor="agent:mcp",
                    confirm_cb=_confirm,
                    override=None,
                    k8s_adapter=k8s,
                    argocd_adapter=argocd,
                    unifi_adapter=unifi,
                    ssh_adapter=ssh,
                    talos_adapter=talos,
                    notifier=notifier_from_env(),
                )
            except ExecutionRefused as exc:
                return {
                    "refused": str(exc),
                    "decision": exc.decision.level.value if exc.decision else None,
                    "reasons": list(exc.decision.reasons) if exc.decision else [],
                    "cell": manifest.cell_key,
                    "next": f"an operator runs `helper exec run {proposal.id}`",
                }
            except ApprovalError as exc:
                return {"refused": f"approval channel failed: {exc}", "cell": manifest.cell_key}
            finally:
                await adapter.aclose()
                if argocd is not None:
                    await argocd.aclose()
                if unifi is not None:
                    await unifi.aclose()
            return {
                "proposal_id": str(proposal.id),
                "cell": manifest.cell_key,
                "target": manifest.target_label,
                "decision": result.decision.level.value,
                "reasons": list(result.decision.reasons),
                "outcome": result.outcome,
                "error": result.error,
                "receipt_id": str(result.receipt_id),
                "duration_ms": result.duration_ms,
                "notification": result.notification,
                "escalation": (
                    None
                    if result.escalation is None
                    else {
                        "event": result.escalation.event,
                        "level": result.escalation.level.value,
                        "clean_streak": result.escalation.clean_streak,
                    }
                ),
            }
    finally:
        await engine.dispose()
