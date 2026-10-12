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

Phase 11.3 adds the Talos plane: :func:`plan_talos_maintenance` reads the
cluster's members through one control-plane node and drafts a
``talos-upgrade`` per member — control planes first, then workers, skipping
members already on the target — and optionally a closing ``k8s-upgrade``.
Between steps the run asks ``talosctl health`` (``health_cb``) and stops when
the cluster is not healthy, so a node that came back wrong halts the rest.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import (
    AutonomyLevel,
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
from homelab_helper.engine.executor import (
    ExecutionRefused,
    ManifestError,
    _ssh_credentials,
    execute_proposal,
    parse_manifest,
)
from homelab_helper.engine.manifest import (
    ManifestError as _ManifestError,
)
from homelab_helper.engine.manifest import (
    build_artifact,
    build_k8s_upgrade_artifact,
    build_node_artifact,
    build_talos_artifact,
)
from homelab_helper.engine.trust import ActionRequest, decide, load_trust_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.adapters.argocd import ArgoCDAdapter
    from homelab_helper.adapters.kernel_ssh import KernelSSHAdapter
    from homelab_helper.adapters.kubernetes import K8sAdapter
    from homelab_helper.adapters.proxmox import ProxmoxAdapter
    from homelab_helper.adapters.talos import TalosAdapter
    from homelab_helper.adapters.unifi import UniFiAdapter
    from homelab_helper.engine.executor import ConfirmCallback
    from homelab_helper.engine.notify import Notifier

    HealthCallback = Callable[[], Awaitable[tuple[bool, str]]]

STEP_KINDS = (
    "drain-migrate",
    "drain-shutdown",
    "node-update",
    "node-reboot",
    "return-migrate",
    "return-start",
    "talos-upgrade",
    "k8s-upgrade",
)
PLANES = ("proxmox", "talos")
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
    closed: dict[str, int],
) -> str:
    """The emptiest open node with room, or the preferred one when it has room.

    A ``no-new-guests`` host is never chosen, but may be *named*: the operator
    overriding their own standing intent for one maintenance window is a
    decision, and the plan says so in the step.
    """
    if preferred is not None:
        room = free.get(preferred, closed.get(preferred))
        if room is None:
            raise MaintenanceError(f"{preferred} is not an online node of this cluster")
        if room < guest.maxmem:
            raise MaintenanceError(
                f"{preferred} cannot hold {guest.name} ({guest.maxmem / 1024**3:.0f} GiB)"
            )
        return preferred
    fits = [n for n, room in free.items() if room >= guest.maxmem]
    if not fits:
        hint = (
            f"; {', '.join(sorted(closed))} would fit but is no-new-guests — name it with --target "
            "to override that for this run"
            if any(room >= guest.maxmem for room in closed.values())
            else "; free memory elsewhere first, or shut it down by hand"
        )
        raise MaintenanceError(
            f"no node can take {guest.name} ({guest.maxmem / 1024**3:.0f} GiB){hint}"
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
    blast_radius: str = "single-host",
) -> ProposalLog:
    hosts = artifact["action"]["hostnames"]
    return ProposalLog(
        title=f"Maintenance {node} {index}/{total}: {title}"[:512],
        description=f"Step {index} of maintenance run {run_id} on {node}.",
        artifact=artifact,
        affected=[{"target_type": "host", "target_id": h} for h in hosts],
        blast_radius=blast_radius,
        proposed_by=f"maintenance:{actor}",
    )


async def _refuse_open_run(session: AsyncSession, node: str) -> None:
    open_run = (
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
    if open_run is not None:
        raise MaintenanceError(
            f"{node} already has an unfinished run {open_run.id} — finish or abort it first"
        )


async def _would_refuse(session: AsyncSession, proposal: ProposalLog) -> str | None:
    """The gate's answer today, read-only and pessimistic, so a run is not started
    only to stop at a cell nobody granted. Informational: the run re-decides each
    step when it executes."""
    manifest = parse_manifest(proposal)
    request = ActionRequest(
        domain=manifest.domain,
        action_kind=manifest.action_kind,
        blast_radius=manifest.blast_radius,
        hostnames=manifest.hostnames,
        rollback_verified=False,
        provenance=proposal.proposed_by,
    )
    level = decide(request, await load_trust_context(session, request)).level
    if level in (AutonomyLevel.BLOCK, AutonomyLevel.PROPOSE):
        return f"would be refused today: {manifest.cell_key} is at {level.value} — grant it first"
    return None


async def _record_steps(
    session: AsyncSession,
    run: MaintenanceRun,
    planned: list[tuple[str, str, dict[str, Any]]],
    actor: str,
) -> MaintenanceRun:
    steps: list[dict[str, Any]] = []
    for index, (kind, title, artifact) in enumerate(planned, 1):
        radius = "cluster" if kind == "k8s-upgrade" else "single-host"
        proposal = _proposal(
            run.id, index, len(planned), run.node, title, artifact, actor, blast_radius=radius
        )
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
                "detail": await _would_refuse(session, proposal),
            }
        )
    run.steps = steps
    await session.flush()
    return run


_TITLE_RE = re.compile(r"^(?:migrate|shut down) (.+?) \((\d+)\)")


async def _displaced(
    session: AsyncSession, run: MaintenanceRun, node: str, resources: list[dict[str, Any]]
) -> list[_Drain]:
    """The guests a stopped run moved off ``node`` and never brought back.

    Read from the run's succeeded drain steps and checked against where each
    guest is now: one that has moved since is somebody else's decision and is
    left alone. The result slots into the new run's return leg.
    """
    if run.node != node or run.plane != "proxmox":
        raise MaintenanceError(f"run {run.id} was for {run.node}, not a Proxmox run on {node}")
    if run.status in _OPEN:
        raise MaintenanceError(f"run {run.id} is still {run.status.value} — finish or abort it")
    where = {int(v["vmid"]): str(v.get("node")) for v in resources if v.get("vmid") is not None}
    returned: set[int] = set()
    for st in run.steps:
        if st["kind"].startswith("return-") and st["state"] == "succeeded":
            done = await session.get(ProposalLog, uuid.UUID(st["proposal_id"]))
            if done is not None:
                returned.add(int(done.artifact["action"]["target"]["vmid"]))
    drain: list[_Drain] = []
    for step in run.steps:
        if not step["kind"].startswith("drain-") or step["state"] != "succeeded":
            continue
        proposal = await session.get(ProposalLog, uuid.UUID(step["proposal_id"]))
        if proposal is None:
            continue
        target = proposal.artifact["action"]["target"]
        vmid = int(target["vmid"])
        parked = target.get("target_node") if step["kind"] == "drain-migrate" else None
        if vmid in returned or where.get(vmid) != (parked or node):
            continue
        match = _TITLE_RE.match(step["name"])
        guest = _Guest(
            vmid=vmid,
            name=match.group(1) if match else str(vmid),
            kind=str(target["vm_kind"]),
            maxmem=0,
            local_storage=step["kind"] == "drain-shutdown",
        )
        drain.append((step["kind"], step["name"], proposal.artifact, guest, parked))
    return drain


async def plan_maintenance(
    session: AsyncSession,
    adapter: ProxmoxAdapter,
    node: str,
    *,
    actor: str,
    target_node: str | None = None,
    resume_from: MaintenanceRun | None = None,
) -> MaintenanceRun:
    """Draft one run for ``node``: its proposals in order, nothing executed.

    ``resume_from`` is a stopped run on the same node: the guests it drained
    and never returned join this run's return leg, so a run that failed after
    the drain is finished by a second one rather than by hand.
    """
    await _refuse_open_run(session, node)
    host = (await session.execute(select(Host).where(Host.hostname == node))).scalar_one_or_none()
    if host is None:
        raise MaintenanceError(
            f"{node} is not a known host — the update and reboot steps need its SSH credentials; "
            "add it with `helper discover host` first"
        )
    try:
        _ssh_credentials(host)
    except ValueError as exc:
        # Checked here, before a single guest moves: a drain that then stops at
        # the update step leaves the node empty and the guests displaced.
        raise MaintenanceError(
            f"{exc} — the update and reboot steps would refuse after the drain"
        ) from exc
    cluster_name: str | None = (await adapter.cluster_status()).get("name")
    nodes = await adapter.cluster_resources("node")
    if not any(str(n.get("node")) == node for n in nodes):
        raise MaintenanceError(f"{node} is not a node of the Proxmox cluster")
    free, closed = _room_by_node(nodes, node, await _closed_hosts(session))
    local = await _local_storage_vmids(session, cluster_name)
    resources = await adapter.cluster_resources("vm")
    guests = _running_guests(resources, node, local)
    drain = _drain_steps(guests, node, free, closed, target_node)
    planned = [(k, t, a) for k, t, a, _, _ in drain]
    if resume_from is not None:
        drain += await _displaced(session, resume_from, node, resources)
    planned.append(("node-update", f"apt dist-upgrade {node}", build_node_artifact(node=node)))
    planned.append(
        (
            "node-reboot",
            f"reboot {node} and wait for it",
            build_node_artifact(node=node, action_kind="node-reboot"),
        )
    )
    planned += _return_steps(drain, node)

    run = MaintenanceRun(node=node, created_by=actor, status=MaintenanceStatus.PLANNED)
    session.add(run)
    await session.flush()
    return await _record_steps(session, run, planned, actor)


_MACHINE_ORDER = {"controlplane": 0, "init": 0, "worker": 1}


def talos_health_gate(talos: TalosAdapter, control_plane: str) -> HealthCallback:
    """The between-steps check for a Talos run: ``talosctl health`` from the control plane."""

    async def _check() -> tuple[bool, str]:
        return await talos.cluster_health(control_plane)

    return _check


async def plan_talos_maintenance(
    session: AsyncSession,
    talos: TalosAdapter,
    control_plane: str,
    version: str,
    *,
    actor: str,
    kubernetes: str | None = None,
) -> MaintenanceRun:
    """Draft a cluster upgrade: one ``talos-upgrade`` per member — control
    planes first, then workers, members already on ``version`` skipped — and,
    with ``kubernetes``, a closing ``k8s-upgrade``. Nothing executed."""
    await _refuse_open_run(session, control_plane)
    members = await talos.members(control_plane)
    if not members:
        raise MaintenanceError(f"{control_plane} reports no cluster members")
    ordered = sorted(
        members, key=lambda m: (_MACHINE_ORDER.get(m["machine_type"], 2), m["hostname"])
    )
    planned: list[tuple[str, str, dict[str, Any]]] = []
    try:
        for member in ordered:
            if member["talos"] == version or not member["address"]:
                continue
            label = f"{member['hostname']} ({member['machine_type']})"
            planned.append(
                (
                    "talos-upgrade",
                    f"upgrade {label} {member['talos'] or '?'} -> {version}",
                    build_talos_artifact(
                        node=member["address"], version=version, hostnames=(member["hostname"],)
                    ),
                )
            )
        if kubernetes:
            planned.append(
                (
                    "k8s-upgrade",
                    f"upgrade Kubernetes -> {kubernetes}",
                    build_k8s_upgrade_artifact(control_plane=control_plane, version=kubernetes),
                )
            )
    except _ManifestError as exc:
        raise MaintenanceError(str(exc)) from exc
    if not planned:
        raise MaintenanceError(f"every member already runs Talos {version}; nothing to plan")
    run = MaintenanceRun(
        node=control_plane, plane="talos", created_by=actor, status=MaintenanceStatus.PLANNED
    )
    session.add(run)
    await session.flush()
    return await _record_steps(session, run, planned, actor)


def _room_by_node(
    nodes: list[dict[str, Any]], node: str, closed_hosts: set[str]
) -> tuple[dict[str, int], dict[str, int]]:
    """``(open, closed)`` free memory per online node other than ``node``."""
    free: dict[str, int] = {}
    closed: dict[str, int] = {}
    for n in nodes:
        name = str(n.get("node"))
        if name == node or n.get("status") != "online":
            continue
        room = int(n.get("maxmem") or 0) - int(n.get("mem") or 0) - _OS_RESERVE
        (closed if name in closed_hosts else free)[name] = room
    return free, closed


def _running_guests(resources: list[dict[str, Any]], node: str, local: set[int]) -> list[_Guest]:
    guests = [
        _Guest(
            vmid=int(v["vmid"]),
            name=str(v.get("name") or v["vmid"]),
            kind=str(v.get("type") or "qemu"),
            maxmem=int(v.get("maxmem") or 0),
            local_storage=int(v["vmid"]) in local,
        )
        for v in resources
        if str(v.get("node")) == node and v.get("status") == "running" and not v.get("template")
    ]
    guests.sort(key=lambda g: -g.maxmem)
    return guests


_Drain = tuple[str, str, dict[str, Any], _Guest, str | None]


def _drain_steps(
    guests: list[_Guest],
    node: str,
    free: dict[str, int],
    closed: dict[str, int],
    target_node: str | None,
) -> list[_Drain]:
    drain: list[_Drain] = []
    for g in guests:
        if g.local_storage:
            art = build_artifact(action_kind="shutdown", node=node, vmid=g.vmid, vm_kind=g.kind)
            drain.append(
                ("drain-shutdown", f"shut down {g.name} ({g.vmid}) — node-local disk", art, g, None)
            )
            continue
        target = _pick_target(g, free, target_node, closed)
        if target in free:
            free[target] -= g.maxmem
        else:
            closed[target] -= g.maxmem
        art = build_artifact(
            action_kind="migrate", node=node, vmid=g.vmid, vm_kind=g.kind, target_node=target
        )
        note = " (no-new-guests, named by you)" if target in closed else ""
        drain.append(
            ("drain-migrate", f"migrate {g.name} ({g.vmid}) → {target}{note}", art, g, target)
        )
    return drain


def _return_steps(drain: list[_Drain], node: str) -> list[tuple[str, str, dict[str, Any]]]:
    planned: list[tuple[str, str, dict[str, Any]]] = []
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
    return planned


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
    talos_adapter: TalosAdapter | None = None,
    health_cb: HealthCallback | None = None,
    notifier: Notifier | None = None,
) -> MaintenanceRun:
    """Execute the run's steps in order through the executor; stop at the first failure.

    ``confirm_cb`` is the operator's answer, relayed to every step that lands
    at CONFIRM. A refused step (PROPOSE, BLOCK, a declined confirmation, a
    closed window) and a failed dispatch end the run the same way. ``health_cb``
    is asked before every step after the first; a cluster that is not healthy
    stops the run there, with the health check's own words on the step.
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
        healthy, health_detail = (
            await health_cb() if health_cb is not None and index > 1 else (True, "")
        )
        if not healthy:
            failure = f"cluster not healthy before this step: {health_detail}"
        elif proposal is None:
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
                    talos_adapter=talos_adapter,
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
            current["detail"] = None
            run.steps = steps
            # Durable per step: a crash later in the run must not roll back the
            # record of what already changed on the fleet.
            await session.commit()
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
        "plane": run.plane,
        "status": run.status.value,
        "created_by": run.created_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "notes": run.notes,
        "steps": [dict(s) for s in run.steps],
    }


__all__ = [
    "PLANES",
    "STEP_KINDS",
    "MaintenanceError",
    "abort_maintenance",
    "execute_maintenance",
    "plan_maintenance",
    "plan_talos_maintenance",
    "run_summary",
    "talos_health_gate",
]
