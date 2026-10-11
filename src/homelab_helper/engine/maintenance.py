"""Maintenance runs (Phase 10.4) — drain → update → reboot → return, as a sequence.

The rolling procedure an operator scripts by hand, inside the gradient. A run
is planned for one node: every running guest gets a step that moves it off
(a ``migrate`` to the emptiest cluster-mate that can hold it, honouring
``no-new-guests`` hosts and shared storage; a guest whose volumes are on
node-local storage gets a ``shutdown`` instead), then ``node-update``, then
``node-reboot``, then the mirror image — ``migrate`` back, ``start`` what was
shut down. Each step is an ordinary ``ProposalLog``; executing the run is
executing those proposals in order through ``execute_proposal``, so every
step passes ``decide()`` on its own cell and leaves its own receipt.

What the run adds is only the order and the stop: the first step that fails
or is refused ends the run, the steps after it are marked skipped and their
proposals rejected, and nothing else moves until the operator has looked.
The run itself is never an authority — the operator's one "yes" at the CLI
(or one tap on the phone) is relayed to each step's CONFIRM, and a step whose
cell sits at PROPOSE refuses exactly as it would alone. ``node-reboot`` has
no inverse and cannot be promoted, so a run can never execute unattended.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import (
    IntentState,
    IntentTargetType,
    MaintenanceStatus,
    ProposalOutcome,
)
from homelab_helper.db.models import (
    Cluster,
    Host,
    MaintenanceRun,
    OperationalIntent,
    ProposalLog,
    VirtualMachine,
)
from homelab_helper.engine.executor import ExecutionRefused, ManifestError, execute_proposal
from homelab_helper.engine.manifest import build_artifact, build_node_artifact

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.adapters.argocd import ArgoCDAdapter
    from homelab_helper.adapters.kernel_ssh import KernelSSHAdapter
    from homelab_helper.adapters.kubernetes import K8sAdapter
    from homelab_helper.adapters.proxmox import ProxmoxAdapter
    from homelab_helper.adapters.unifi import UniFiAdapter
    from homelab_helper.engine.executor import ConfirmCallback
    from homelab_helper.engine.notify import Notifier

STEP_KINDS = (
    "drain-migrate",
    "drain-shutdown",
    "node-update",
    "node-reboot",
    "return-migrate",
    "return-start",
)
_OS_RESERVE = 1024**3
_OPEN = (MaintenanceStatus.PLANNED, MaintenanceStatus.RUNNING)


class MaintenanceError(ValueError):
    """A run that cannot be planned or executed as asked."""


@dataclass
class _Guest:
    vmid: int
    name: str
    kind: str
    maxmem: int
    local_storage: bool


async def _closed_hosts(session: AsyncSession) -> set[str]:
    hosts = {str(h.id): h.hostname for h in (await session.execute(select(Host))).scalars().all()}
    rows = (
        await session.execute(
            select(OperationalIntent).where(
                OperationalIntent.target_type == IntentTargetType.HOST,
                OperationalIntent.intent == IntentState.NO_NEW_GUESTS,
            )
        )
    ).scalars()
    return {hosts[i.target_id] for i in rows if i.target_id in hosts}


async def _local_storage_vmids(session: AsyncSession, cluster_name: str | None) -> set[int]:
    rows = (await session.execute(select(VirtualMachine, Cluster).join(Cluster))).all()
    return {
        vm.vmid
        for vm, cluster in rows
        if vm.vmid is not None
        and (cluster_name is None or cluster.name == cluster_name)
        and (vm.attributes or {}).get("shared_storage") is False
    }


def _pick_target(
    guest: _Guest,
    free: dict[str, int],
    preferred: str | None,
) -> str:
    """The emptiest node with room, or the preferred one when it has room."""
    if preferred is not None:
        if preferred not in free:
            raise MaintenanceError(f"{preferred} is not an available target node")
        if free[preferred] < guest.maxmem:
            raise MaintenanceError(
                f"{preferred} cannot hold {guest.name} ({guest.maxmem / 1024**3:.0f} GiB)"
            )
        return preferred
    fits = [n for n, room in free.items() if room >= guest.maxmem]
    if not fits:
        raise MaintenanceError(
            f"no node can take {guest.name} ({guest.maxmem / 1024**3:.0f} GiB) — "
            "free memory elsewhere first, or shut it down by hand"
        )
    return max(fits, key=lambda n: free[n])


def _proposal(
    run_id: uuid.UUID,
    index: int,
    total: int,
    node: str,
    title: str,
    artifact: dict[str, Any],
    actor: str,
) -> ProposalLog:
    hosts = artifact["action"]["hostnames"]
    return ProposalLog(
        title=f"Maintenance {node} {index}/{total}: {title}"[:512],
        description=f"Step {index} of maintenance run {run_id} on {node}.",
        artifact=artifact,
        affected=[{"target_type": "host", "target_id": h} for h in hosts],
        blast_radius="single-host",
        proposed_by=f"maintenance:{actor}",
    )


async def plan_maintenance(
    session: AsyncSession,
    adapter: ProxmoxAdapter,
    node: str,
    *,
    actor: str,
    target_node: str | None = None,
) -> MaintenanceRun:
    """Draft one run for ``node``: its proposals in order, nothing executed."""
    open_runs = (
        (
            await session.execute(
                select(MaintenanceRun).where(
                    MaintenanceRun.node == node, MaintenanceRun.status.in_(list(_OPEN))
                )
            )
        )
        .scalars()
        .first()
    )
    if open_runs is not None:
        raise MaintenanceError(
            f"{node} already has an unfinished run {open_runs.id} — finish or abort it first"
        )
    host = (await session.execute(select(Host).where(Host.hostname == node))).scalar_one_or_none()
    if host is None:
        raise MaintenanceError(
            f"{node} is not a known host — the update and reboot steps need its SSH credentials; "
            "add it with `helper discover host` first"
        )
    cluster_name: str | None = (await adapter.cluster_status()).get("name")
    nodes = await adapter.cluster_resources("node")
    if not any(str(n.get("node")) == node for n in nodes):
        raise MaintenanceError(f"{node} is not a node of the Proxmox cluster")
    closed = await _closed_hosts(session)
    free: dict[str, int] = {}
    for n in nodes:
        name = str(n.get("node"))
        if name == node or n.get("status") != "online" or name in closed:
            continue
        free[name] = int(n.get("maxmem") or 0) - int(n.get("mem") or 0) - _OS_RESERVE

    local = await _local_storage_vmids(session, cluster_name)
    guests = [
        _Guest(
            vmid=int(v["vmid"]),
            name=str(v.get("name") or v["vmid"]),
            kind=str(v.get("type") or "qemu"),
            maxmem=int(v.get("maxmem") or 0),
            local_storage=int(v["vmid"]) in local,
        )
        for v in await adapter.cluster_resources("vm")
        if str(v.get("node")) == node and v.get("status") == "running" and not v.get("template")
    ]
    guests.sort(key=lambda g: -g.maxmem)

    drain: list[tuple[str, str, dict[str, Any], _Guest, str | None]] = []
    for g in guests:
        if g.local_storage:
            art = build_artifact(action_kind="shutdown", node=node, vmid=g.vmid, vm_kind=g.kind)
            drain.append(
                ("drain-shutdown", f"shut down {g.name} ({g.vmid}) — node-local disk", art, g, None)
            )
            continue
        target = _pick_target(g, free, target_node)
        free[target] -= g.maxmem
        art = build_artifact(
            action_kind="migrate", node=node, vmid=g.vmid, vm_kind=g.kind, target_node=target
        )
        drain.append(("drain-migrate", f"migrate {g.name} ({g.vmid}) → {target}", art, g, target))

    planned: list[tuple[str, str, dict[str, Any]]] = [(k, t, a) for k, t, a, _, _ in drain]
    planned.append(("node-update", f"apt dist-upgrade {node}", build_node_artifact(node=node)))
    planned.append(
        (
            "node-reboot",
            f"reboot {node} and wait for it",
            build_node_artifact(node=node, action_kind="node-reboot"),
        )
    )
    for kind, _, _, g, parked_on in reversed(drain):
        if kind == "drain-shutdown":
            art = build_artifact(action_kind="start", node=node, vmid=g.vmid, vm_kind=g.kind)
            planned.append(("return-start", f"start {g.name} ({g.vmid})", art))
        else:
            art = build_artifact(
                action_kind="migrate",
                node=str(parked_on),
                vmid=g.vmid,
                vm_kind=g.kind,
                target_node=node,
            )
            planned.append(
                ("return-migrate", f"migrate {g.name} ({g.vmid}) back from {parked_on}", art)
            )

    run = MaintenanceRun(node=node, created_by=actor, status=MaintenanceStatus.PLANNED)
    session.add(run)
    await session.flush()
    steps: list[dict[str, Any]] = []
    for index, (kind, title, artifact) in enumerate(planned, 1):
        proposal = _proposal(run.id, index, len(planned), node, title, artifact, actor)
        session.add(proposal)
        await session.flush()
        steps.append(
            {
                "index": index,
                "name": title,
                "kind": kind,
                "proposal_id": str(proposal.id),
                "receipt_id": None,
                "state": "planned",
                "detail": None,
            }
        )
    run.steps = steps
    await session.flush()
    return run


async def _reject_remaining(
    session: AsyncSession, run: MaintenanceRun, from_index: int, why: str
) -> None:
    steps = [dict(s) for s in run.steps]
    for step in steps:
        if step["index"] < from_index or step["state"] != "planned":
            continue
        step["state"] = "skipped"
        step["detail"] = why
        proposal = await session.get(ProposalLog, uuid.UUID(step["proposal_id"]))
        if proposal is not None and proposal.outcome is ProposalOutcome.PENDING:
            proposal.outcome = ProposalOutcome.USER_REJECTED
            proposal.outcome_at = datetime.now(UTC)
            proposal.outcome_by = "maintenance"
            proposal.outcome_notes = why
    run.steps = steps


async def execute_maintenance(
    session: AsyncSession,
    run: MaintenanceRun,
    adapter: ProxmoxAdapter,
    *,
    actor: str,
    confirm_cb: ConfirmCallback | None,
    ssh_adapter: KernelSSHAdapter | None,
    k8s_adapter: K8sAdapter | None = None,
    argocd_adapter: ArgoCDAdapter | None = None,
    unifi_adapter: UniFiAdapter | None = None,
    notifier: Notifier | None = None,
) -> MaintenanceRun:
    """Execute the run's steps in order through the executor; stop at the first failure.

    ``confirm_cb`` is the operator's answer, relayed to every step that lands
    at CONFIRM. A refused step (PROPOSE, BLOCK, a declined confirmation, a
    closed window) and a failed dispatch end the run the same way.
    """
    if run.status not in _OPEN:
        raise MaintenanceError(f"run {run.id} is {run.status.value}, not runnable")
    run.status = MaintenanceStatus.RUNNING
    run.started_at = run.started_at or datetime.now(UTC)
    await session.flush()
    for step in list(run.steps):
        if step["state"] == "succeeded":
            continue
        index = int(step["index"])
        proposal = await session.get(ProposalLog, uuid.UUID(step["proposal_id"]))
        failure: str | None = None
        receipt_id: str | None = None
        if proposal is None:
            failure = "proposal is gone"
        elif proposal.outcome is not ProposalOutcome.PENDING:
            failure = f"proposal is {proposal.outcome.value}, not pending"
        else:
            try:
                result = await execute_proposal(
                    session,
                    proposal,
                    adapter,
                    actor=actor,
                    confirm_cb=confirm_cb,
                    override=None,
                    k8s_adapter=k8s_adapter,
                    argocd_adapter=argocd_adapter,
                    unifi_adapter=unifi_adapter,
                    ssh_adapter=ssh_adapter,
                    notifier=notifier,
                )
                receipt_id = str(result.receipt_id)
                if result.outcome != "succeeded":
                    failure = str(result.error or "dispatch failed")
            except (ExecutionRefused, ManifestError) as exc:
                failure = str(exc)
        steps = [dict(s) for s in run.steps]
        current = steps[index - 1]
        current["receipt_id"] = receipt_id
        if failure is None:
            current["state"] = "succeeded"
            run.steps = steps
            await session.flush()
            continue
        current["state"] = "failed"
        current["detail"] = failure
        run.steps = steps
        await _reject_remaining(
            session, run, index + 1, f"maintenance run {run.id} stopped at step {index}: {failure}"
        )
        run.status = MaintenanceStatus.FAILED
        run.finished_at = datetime.now(UTC)
        run.notes = f"step {index} ({current['name']}): {failure}"
        await session.flush()
        return run
    run.status = MaintenanceStatus.SUCCEEDED
    run.finished_at = datetime.now(UTC)
    await session.flush()
    return run


async def abort_maintenance(
    session: AsyncSession, run: MaintenanceRun, *, actor: str, reason: str
) -> MaintenanceRun:
    """Stop a planned or interrupted run: its remaining proposals are rejected."""
    if run.status not in _OPEN:
        raise MaintenanceError(f"run {run.id} is {run.status.value}, nothing to abort")
    await _reject_remaining(session, run, 1, f"aborted by {actor}: {reason}")
    run.status = MaintenanceStatus.ABORTED
    run.finished_at = datetime.now(UTC)
    run.notes = f"aborted by {actor}: {reason}"
    await session.flush()
    return run


def run_summary(run: MaintenanceRun) -> dict[str, Any]:
    """JSON-safe view for the CLI and the MCP surface."""
    return {
        "id": str(run.id),
        "node": run.node,
        "status": run.status.value,
        "created_by": run.created_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "notes": run.notes,
        "steps": [dict(s) for s in run.steps],
    }


__all__ = [
    "STEP_KINDS",
    "MaintenanceError",
    "abort_maintenance",
    "execute_maintenance",
    "plan_maintenance",
    "run_summary",
]
