"""Agent-drafted proposals: propose_* tools that write ProposalLog rows, and reading them back. Nothing executes here."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.config import database_url
from homelab_helper.db.enums import (
    ProposalOutcome,
)
from homelab_helper.db.lookup import rows_by_id_prefix
from homelab_helper.db.models import (
    ProposalLog,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.executor import (
    ManifestError,
    parse_manifest,
)
from homelab_helper.engine.manifest import (
    BLAST_RADII,
    DNS_RECORD_TYPES,
    K8S_BLAST_RADIUS,
    WORKLOAD_KINDS,
    argocd_radius_error,
    build_argocd_artifact,
    build_artifact,
    build_dns_artifact,
    build_k8s_upgrade_artifact,
    build_talos_artifact,
    build_workload_artifact,
)
from homelab_helper.engine.playbooks import PLAYBOOKS, run_playbooks
from homelab_helper.engine.trust import decide, load_trust_context

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from homelab_helper.mcp_server._app import (
    _iso,
    server,
)


def _proposal_dict(p: ProposalLog) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "proposed_at": _iso(p.proposed_at),
        "proposed_by": p.proposed_by,
        "title": p.title,
        "description": p.description,
        "kind": (p.artifact or {}).get("kind"),
        "blast_radius": p.blast_radius,
        "affected": list(p.affected or []),
        "outcome": p.outcome.value,
        "outcome_at": _iso(p.outcome_at),
        "outcome_by": p.outcome_by,
        "outcome_notes": p.outcome_notes,
    }


async def _pessimistic_preview(session: AsyncSession, proposal: ProposalLog) -> dict[str, Any]:
    manifest = parse_manifest(proposal)
    action = manifest.request(proposal.proposed_by)
    decision = decide(action, await load_trust_context(session, action))
    return {
        "cell": manifest.cell_key,
        "target": manifest.target_label,
        "decision_if_run_now": decision.level.value,
        "decision_reasons": list(decision.reasons),
        "decision_basis": "pessimistic: rollback treated as unverified",
    }


@server.tool()
async def propose_action(
    action_kind: str,
    node: str,
    vmid: int,
    vm_kind: str,
    title: str,
    description: str | None = None,
    blast_radius: str = "single-host",
    hostnames: list[str] | None = None,
    target_node: str | None = None,
    online: bool = True,
    cpu_type: str | None = None,
    cores: int | None = None,
    memory_mib: int | None = None,
) -> dict[str, Any]:
    """Draft a Proxmox guest action — start/stop/shutdown/restart, migrate
    (give `target_node`; `online=False` for an offline move), cpu-type
    (give `cpu_type`, e.g. "x86-64-v3"; QEMU only, applied at the guest's
    next stop/start), or resize (give `cores` and/or `memory_mib`; a QEMU
    guest without hotplug applies it at its next stop/start, a container
    live) of a VM or container — as a PENDING proposal. Validates the manifest, writes only to
    the harness DB, and returns what policy would decide right now. Never
    executes on its own: follow with `execute_proposal` (policy + the
    operator's tap decide) or leave it for `helper exec`. An agent cannot
    grant, override, or open a window."""
    if blast_radius not in BLAST_RADII:
        return {"error": f"blast_radius must be one of {', '.join(BLAST_RADII)}"}
    try:
        artifact = build_artifact(
            action_kind=action_kind,
            node=node,
            vmid=vmid,
            vm_kind=vm_kind,
            hostnames=tuple(hostnames) if hostnames else None,
            target_node=target_node,
            online=online,
            cpu_type=cpu_type,
            cores=cores,
            memory_mib=memory_mib,
        )
    except ManifestError as exc:
        return {"error": str(exc)}
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            proposal = ProposalLog(
                title=title.strip()[:512],
                description=description,
                artifact=artifact,
                affected=[
                    {"target_type": "host", "target_id": h} for h in artifact["action"]["hostnames"]
                ],
                blast_radius=blast_radius,
                proposed_by="agent:mcp",
            )
            session.add(proposal)
            await session.flush()
            preview = await _pessimistic_preview(session, proposal)
            return {
                **_proposal_dict(proposal),
                **preview,
                "next": f"`execute_proposal({proposal.id})`, or an operator runs "
                f"`helper exec run {proposal.id}` / `helper exec reject {proposal.id}`",
            }
    finally:
        await engine.dispose()


@server.tool()
async def propose_workload_action(
    action_kind: str,
    namespace: str,
    kind: str,
    name: str,
    title: str,
    replicas: int | None = None,
    description: str | None = None,
    blast_radius: str = "single-service",
) -> dict[str, Any]:
    """Draft a Kubernetes workload action — `workload-restart` (rollout
    restart) or `workload-scale` (give `replicas`) of a deployment,
    statefulset or daemonset — as a PENDING proposal in the `containers`
    domain. Same contract as `propose_action`: validates, writes only to the
    harness DB, never executes on its own."""
    if blast_radius not in BLAST_RADII:
        return {"error": f"blast_radius must be one of {', '.join(BLAST_RADII)}"}
    if kind not in WORKLOAD_KINDS:
        return {"error": f"kind must be one of {', '.join(WORKLOAD_KINDS)}"}
    try:
        artifact = build_workload_artifact(
            action_kind=action_kind,
            namespace=namespace,
            kind=kind,
            name=name,
            replicas=replicas,
        )
    except ManifestError as exc:
        return {"error": str(exc)}
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            proposal = ProposalLog(
                title=title.strip()[:512],
                description=description,
                artifact=artifact,
                affected=[{"target_type": "workload", "target_id": f"{namespace}/{kind}/{name}"}],
                blast_radius=blast_radius,
                proposed_by="agent:mcp",
            )
            session.add(proposal)
            await session.flush()
            preview = await _pessimistic_preview(session, proposal)
            return {
                **_proposal_dict(proposal),
                **preview,
                "next": f"`execute_proposal({proposal.id})`, or an operator runs "
                f"`helper exec run {proposal.id}` / `helper exec reject {proposal.id}`",
            }
    finally:
        await engine.dispose()


async def _persist_proposal(
    artifact: dict[str, Any],
    *,
    title: str,
    description: str | None,
    blast_radius: str,
    affected: list[dict[str, str]],
) -> dict[str, Any]:
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            proposal = ProposalLog(
                title=title.strip()[:512],
                description=description,
                artifact=artifact,
                affected=affected,
                blast_radius=blast_radius,
                proposed_by="agent:mcp",
            )
            session.add(proposal)
            await session.flush()
            preview = await _pessimistic_preview(session, proposal)
            return {
                **_proposal_dict(proposal),
                **preview,
                "next": f"`execute_proposal({proposal.id})`, or an operator runs "
                f"`helper exec run {proposal.id}` / `helper exec reject {proposal.id}`",
            }
    finally:
        await engine.dispose()


@server.tool()
async def propose_argocd_sync(
    application: str,
    title: str,
    revision: str | None = None,
    prune: bool = False,
    description: str | None = None,
    blast_radius: str = "single-service",
) -> dict[str, Any]:
    """Draft an Argo CD sync of one application (optionally pinned to a git
    `revision`, optionally pruning) as a PENDING proposal in the `containers`
    domain. A sync with `revision` or `prune` must be `blast_radius="cluster"`:
    it gets its own cell, never the plain resync's. Rollback is Argo CD's own
    history (the current deployed entry).
    Same contract as `propose_action`: validates, writes only to the harness
    DB, never executes on its own."""
    if blast_radius not in BLAST_RADII:
        return {"error": f"blast_radius must be one of {', '.join(BLAST_RADII)}"}
    refusal = argocd_radius_error(revision=revision, prune=prune, blast_radius=blast_radius)
    if refusal:
        return {"error": refusal}
    try:
        artifact = build_argocd_artifact(application=application, revision=revision, prune=prune)
    except ManifestError as exc:
        return {"error": str(exc)}
    return await _persist_proposal(
        artifact,
        title=title,
        description=description,
        blast_radius=blast_radius,
        affected=[{"target_type": "argocd-app", "target_id": application}],
    )


@server.tool()
async def propose_dns_record(
    hostname: str,
    value: str,
    title: str,
    record_type: str = "A",
    ttl: int = 0,
    controller: str | None = None,
    description: str | None = None,
    blast_radius: str = "single-service",
) -> dict[str, Any]:
    """Draft a static DNS upsert (create or update one `hostname` -> `value`
    record of `record_type` on a UniFi controller; `controller` names one of
    HOMELAB_HELPER_UNIFI_CONTROLLERS, default the single configured one) as a
    PENDING proposal in the `dns` domain. Rollback restores the prior record
    or deletes the created one. Never executes on its own."""
    if blast_radius not in BLAST_RADII:
        return {"error": f"blast_radius must be one of {', '.join(BLAST_RADII)}"}
    if record_type not in DNS_RECORD_TYPES:
        return {"error": f"record_type must be one of {', '.join(DNS_RECORD_TYPES)}"}
    try:
        artifact = build_dns_artifact(
            hostname=hostname, value=value, record_type=record_type, ttl=ttl, controller=controller
        )
    except ManifestError as exc:
        return {"error": str(exc)}
    return await _persist_proposal(
        artifact,
        title=title,
        description=description,
        blast_radius=blast_radius,
        affected=[{"target_type": "dns-record", "target_id": f"{record_type} {hostname}"}],
    )


@server.tool()
async def propose_talos_upgrade(
    node: str,
    version: str,
    title: str,
    hostname: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Draft a Talos upgrade of one node (`node` as `talosctl -n` knows it,
    `version` a release tag like "v1.14.2"; `hostname` names the inventory host
    when it differs) as a PENDING proposal in the `host-os` domain, blast
    radius single-host. The installer image is derived from the node's own
    `machine.install.image` at execution time — a proposal names a version,
    never an image. Rollback is `talosctl rollback` (the previous install stays
    in the other boot partition), verified against the live node before each
    run. Never executes on its own."""
    try:
        artifact = build_talos_artifact(
            node=node, version=version, hostnames=(hostname,) if hostname else None
        )
    except ManifestError as exc:
        return {"error": str(exc)}
    return await _persist_proposal(
        artifact,
        title=title,
        description=description,
        blast_radius="single-host",
        affected=[{"target_type": "host", "target_id": hostname or node}],
    )


@server.tool()
async def propose_k8s_upgrade(
    control_plane: str,
    version: str,
    title: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Draft a Kubernetes upgrade (`talosctl upgrade-k8s --to <version>` run
    from `control_plane`, a control-plane node as `talosctl -n` knows it;
    `version` like "1.37.2") as a PENDING proposal in the `containers` domain
    at blast radius `cluster`. Preflight at run time: cluster healthy, every
    member on one Talos release, target newer and at most one minor ahead. A
    Kubernetes upgrade has no inverse, so the cell can never reach AUTONOMOUS.
    Never executes on its own."""
    try:
        artifact = build_k8s_upgrade_artifact(control_plane=control_plane, version=version)
    except ManifestError as exc:
        return {"error": str(exc)}
    return await _persist_proposal(
        artifact,
        title=title,
        description=description,
        blast_radius=K8S_BLAST_RADIUS,
        affected=[{"target_type": "cluster", "target_id": control_plane}],
    )


@server.tool()
async def draft_remediations() -> dict[str, Any]:
    """Run the remediation playbooks once: every OPEN finding a playbook covers
    (Argo CD drift → argocd-sync, unhealthy workload → workload-restart) gets a
    PENDING proposal, unless it is younger than 15 minutes (platform self-heal
    gets first go), one is already pending, or one was decided within the
    cooldown; a pending draft whose finding resolved is withdrawn.
    Deterministic — the finding's own fields pick the action. Nothing
    executes; follow with `execute_proposal` or `helper daemon run --ask`."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            result = await run_playbooks(session)
            return {
                "drafted": result.drafted,
                "skipped_pending": len(result.skipped_live),
                "skipped_cooldown": len(result.skipped_cooldown),
                "skipped_young": len(result.skipped_young),
                "withdrawn": result.withdrawn,
                "skipped_done": len(result.skipped_done),
                "findings_without_playbook": result.no_playbook,
                "playbooks": [f"{pb.name}: {pb.description}" for pb in PLAYBOOKS],
            }
    finally:
        await engine.dispose()


@server.tool()
async def list_proposals(
    outcome: str | None = None, limit: int = 50
) -> list[dict[str, Any]] | dict[str, Any]:
    """Proposals in the harness DB, newest first. ``outcome`` filters by
    pending / user-accepted / user-rejected / user-deferred / superseded /
    expired; omit for all."""
    wanted: ProposalOutcome | None = None
    if outcome is not None:
        try:
            wanted = ProposalOutcome(outcome)
        except ValueError:
            return {
                "error": f"unknown outcome {outcome!r}; expected one of {[o.value for o in ProposalOutcome]}"
            }
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            stmt = (
                select(ProposalLog)
                .order_by(ProposalLog.proposed_at.desc())
                .limit(max(1, min(limit, 500)))
            )
            if wanted is not None:
                stmt = stmt.where(ProposalLog.outcome == wanted)
            rows = (await session.execute(stmt)).scalars().all()
            return [_proposal_dict(p) for p in rows]
    finally:
        await engine.dispose()


@server.tool()
async def get_proposal(proposal_id: str) -> dict[str, Any]:
    """One proposal by id (or a unique id prefix), with its artifact and, for
    an action, the pessimistic policy preview."""
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with sm() as session:
            matches = await rows_by_id_prefix(session, ProposalLog, proposal_id)
            if not matches:
                return {"error": f"no proposal with id {proposal_id!r}"}
            if len(matches) > 1:
                return {"error": f"prefix {proposal_id!r} is ambiguous (more than one match)"}
            proposal = matches[0]
            out = _proposal_dict(proposal)
            out["artifact"] = proposal.artifact
            if (proposal.artifact or {}).get("kind") == "action":
                try:
                    out.update(await _pessimistic_preview(session, proposal))
                except ManifestError as exc:
                    out["error"] = str(exc)
            return out
    finally:
        await engine.dispose()
