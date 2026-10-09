"""Remediation playbooks — findings become proposals, deterministically (Phase 7 slice 3).

A playbook is a pure mapping from one finding kind (and the identity its
``affected`` list carries) to one action manifest. The registry below is the
"Triage agent" the roadmap named, and it is deliberately not an LLM: a
finding's own fields decide which manifest is drafted, so nothing an
adversarial finding description says can pick a different action. An LLM may
*narrate* a finding (``helper findings narrate``); it never drafts here.

Drafting writes a PENDING ``ProposalLog`` (``proposed_by="playbook:<name>"``,
``finding_id`` set) and records the proposal on the finding's
``proposed_actions``. Policy and the operator decide from there, as for any
other proposal. Five guards keep this from looping or asking about noise:

- a finding must have persisted for ``min_age`` (default 15 min) before it is
  drafted for — a transient blip that the platform heals itself (an Argo CD app
  briefly OutOfSync under automated sync) never reaches a phone;
- one live proposal per finding — an existing PENDING proposal for the finding
  means nothing new is drafted;
- a cooldown after any decided proposal (accepted, rejected, deferred), so a
  fix that did not clear the finding is not retried every pass;
- a playbook marked ``redraft_after_success=False`` never drafts an action
  identical to one already executed for the same finding: a resize applied to a
  VM that has not restarted yet still *measures* as the old size, and drafting
  it again would only ask the operator for something already done;
- a pending playbook proposal whose finding has RESOLVED is withdrawn
  (``EXPIRED``) so the listener never asks about a problem that is gone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingStatus, ProposalOutcome
from homelab_helper.db.models import ProposalLog, ReconciliationFinding
from homelab_helper.engine.category_findings import category_of
from homelab_helper.engine.manifest import (
    ManifestError,
    build_argocd_artifact,
    build_artifact,
    build_workload_artifact,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_COOLDOWN = timedelta(hours=6)
DEFAULT_MIN_AGE = timedelta(minutes=15)


@dataclass(frozen=True)
class Draft:
    artifact: dict[str, Any]
    title: str
    blast_radius: str
    summary: str


Builder = Callable[[ReconciliationFinding], Draft | None]


@dataclass(frozen=True)
class Playbook:
    name: str
    finding_kind: FindingKind
    target_type: str
    build: Builder
    description: str
    redraft_after_success: bool = True
    """False when an executed draft's effect can lag its finding (see module docstring)."""


def _target(finding: ReconciliationFinding, target_type: str) -> str | None:
    for item in finding.affected or []:
        if item.get("target_type") == target_type and item.get("target_id"):
            return str(item["target_id"])
    return None


def _argocd_status(finding: ReconciliationFinding) -> dict[str, Any] | None:
    for ref in finding.evidence_refs or []:
        if ref.get("type") == "argocd_status":
            return ref
    return None


def _argocd_resync(finding: ReconciliationFinding) -> Draft | None:
    app = _target(finding, "argocd-app")
    status = _argocd_status(finding)
    if not app or status is None or status.get("sync") != "OutOfSync":
        return None
    try:
        artifact = build_argocd_artifact(application=app)
    except ManifestError:
        return None
    return Draft(
        artifact=artifact,
        title=f"Re-sync {app} (Argo CD drift)",
        blast_radius="single-service",
        summary=f"argocd-resync: sync application {app} to git",
    )


def _workload_restart(finding: ReconciliationFinding) -> Draft | None:
    wid = _target(finding, "workload")
    if not wid or wid.count("/") != 2:  # noqa: PLR2004 - namespace/kind/name
        return None
    namespace, kind, name = wid.split("/")
    try:
        artifact = build_workload_artifact(
            action_kind="workload-restart", namespace=namespace, kind=kind, name=name
        )
    except ManifestError:
        return None
    return Draft(
        artifact=artifact,
        title=f"Rollout restart {kind}/{name} in {namespace} (unhealthy)",
        blast_radius="single-service",
        summary=f"workload-restart: rollout restart {wid}",
    )


_RESIZE_CATEGORIES = {"cpu-grow", "cpu-shrink", "mem-grow", "mem-shrink"}
_MIB = 1024**2


def _rightsize(finding: ReconciliationFinding) -> Draft | None:
    """A rightsizing finding's proposed cores or memory → an executable resize.

    ``idle`` is deliberately not drafted: stopping or retiring a guest is the
    operator's call, not a sizing correction.
    """
    category = category_of(finding)
    if category not in _RESIZE_CATEGORIES:
        return None
    ev: dict[str, Any] = next(
        (dict(r) for r in finding.evidence_refs or [] if r.get("type") == "evidence"), {}
    )
    node, vmid, vm_kind = ev.get("node"), ev.get("vmid"), ev.get("vm_kind")
    if not node or not isinstance(vmid, int) or vm_kind not in ("qemu", "lxc"):
        return None
    name = ev.get("name") or f"{vm_kind}/{vmid}"
    cores: int | None = None
    memory_mib: int | None = None
    if category.startswith("cpu"):
        cores = ev.get("proposed_cores")
        if not isinstance(cores, int):
            return None
        change = f"cores {ev.get('allocated_cores'):g} → {cores}"
    else:
        proposed = ev.get("proposed_bytes")
        if not isinstance(proposed, int):
            return None
        memory_mib = proposed // _MIB
        change = f"memory {int(ev.get('allocated_bytes') or 0) // _MIB} → {memory_mib} MiB"
    try:
        artifact = build_artifact(
            action_kind="resize",
            node=str(node),
            vmid=vmid,
            vm_kind=str(vm_kind),
            cores=cores,
            memory_mib=memory_mib,
        )
    except ManifestError:
        return None
    return Draft(
        artifact=artifact,
        title=f"Resize {name}: {change}",
        blast_radius="single-host",
        summary=f"rightsize ({category}): {change}",
    )


PLAYBOOKS: tuple[Playbook, ...] = (
    Playbook(
        name="argocd-resync",
        finding_kind=FindingKind.DRIFT_CANDIDATE,
        target_type="argocd-app",
        build=_argocd_resync,
        description="Argo CD reports the app out of sync or unhealthy → sync it to git.",
    ),
    Playbook(
        name="workload-restart",
        finding_kind=FindingKind.WORKLOAD_UNHEALTHY,
        target_type="workload",
        build=_workload_restart,
        description="A settled workload has fewer ready replicas than desired → rollout restart.",
    ),
    Playbook(
        name="rightsize",
        finding_kind=FindingKind.RIGHTSIZING,
        target_type="guest",
        build=_rightsize,
        description="Usage history says a guest's cores or memory are wrong → resize to the proposed value.",
        redraft_after_success=False,
    ),
)


def playbook_for(finding: ReconciliationFinding) -> Playbook | None:
    for pb in PLAYBOOKS:
        if pb.finding_kind is finding.kind and _target(finding, pb.target_type):
            return pb
    return None


@dataclass
class PlaybookResult:
    drafted: list[str] = field(default_factory=list)
    """``"<playbook> → <proposal id>"`` per new proposal."""
    skipped_live: list[str] = field(default_factory=list)
    """Findings that already have a pending proposal."""
    skipped_cooldown: list[str] = field(default_factory=list)
    """Findings whose last proposal was decided within the cooldown."""
    skipped_young: list[str] = field(default_factory=list)
    """Findings not yet open for ``min_age``."""
    withdrawn: list[str] = field(default_factory=list)
    """Pending playbook proposals expired because their finding resolved."""
    skipped_done: list[str] = field(default_factory=list)
    """Findings whose identical action was already executed (awaiting its effect)."""
    no_playbook: int = 0


async def _already_done(
    session: AsyncSession, finding: ReconciliationFinding, artifact: dict[str, Any]
) -> bool:
    accepted = (
        (
            await session.execute(
                select(ProposalLog).where(
                    ProposalLog.finding_id == finding.id,
                    ProposalLog.outcome == ProposalOutcome.USER_ACCEPTED,
                )
            )
        )
        .scalars()
        .all()
    )
    return any((p.artifact or {}).get("action") == artifact.get("action") for p in accepted)


async def _blocking_proposal(
    session: AsyncSession, finding: ReconciliationFinding, cooldown: timedelta, now: datetime
) -> str | None:
    rows = (
        (
            await session.execute(
                select(ProposalLog)
                .where(ProposalLog.finding_id == finding.id)
                .order_by(ProposalLog.proposed_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for p in rows:
        if p.outcome is ProposalOutcome.PENDING:
            return "live"
        if p.outcome in (
            ProposalOutcome.USER_ACCEPTED,
            ProposalOutcome.USER_REJECTED,
            ProposalOutcome.USER_DEFERRED,
        ):
            decided = p.outcome_at or p.proposed_at
            if decided.tzinfo is None:
                decided = decided.replace(tzinfo=UTC)
            if now - decided < cooldown:
                return "cooldown"
            return None
    return None


async def _withdraw_stale(session: AsyncSession, now: datetime) -> list[str]:
    rows = (
        (
            await session.execute(
                select(ProposalLog)
                .join(ReconciliationFinding, ReconciliationFinding.id == ProposalLog.finding_id)
                .where(
                    ProposalLog.outcome == ProposalOutcome.PENDING,
                    ProposalLog.proposed_by.like("playbook:%"),
                    ReconciliationFinding.status == FindingStatus.RESOLVED,
                )
            )
        )
        .scalars()
        .all()
    )
    for p in rows:
        p.outcome = ProposalOutcome.EXPIRED
        p.outcome_at = now
    return [str(p.id) for p in rows]


def _age(finding: ReconciliationFinding, now: datetime) -> timedelta:
    seen = finding.first_seen
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    return now - seen


async def run_playbooks(
    session: AsyncSession,
    *,
    when: datetime | None = None,
    cooldown: timedelta = DEFAULT_COOLDOWN,
    min_age: timedelta = DEFAULT_MIN_AGE,
) -> PlaybookResult:
    """Draft a proposal for every OPEN finding a playbook covers, with the guards above."""
    now = when or datetime.now(UTC)
    result = PlaybookResult()
    result.withdrawn = await _withdraw_stale(session, now)
    findings = (
        (
            await session.execute(
                select(ReconciliationFinding)
                .where(ReconciliationFinding.status == FindingStatus.OPEN)
                .order_by(ReconciliationFinding.first_seen)
            )
        )
        .scalars()
        .all()
    )
    for finding in findings:
        pb = playbook_for(finding)
        if pb is None:
            result.no_playbook += 1
            continue
        draft = pb.build(finding)
        if draft is None:
            result.no_playbook += 1
            continue
        if _age(finding, now) < min_age:
            result.skipped_young.append(finding.fingerprint)
            continue
        blocker = await _blocking_proposal(session, finding, cooldown, now)
        if blocker == "live":
            result.skipped_live.append(finding.fingerprint)
            continue
        if blocker == "cooldown":
            result.skipped_cooldown.append(finding.fingerprint)
            continue
        if not pb.redraft_after_success and await _already_done(session, finding, draft.artifact):
            result.skipped_done.append(finding.fingerprint)
            continue
        proposal = ProposalLog(
            proposed_by=f"playbook:{pb.name}",
            finding_id=finding.id,
            title=draft.title[:512],
            description=f"{pb.description}\n\nFinding: {finding.title}\n{finding.description or ''}".strip(),
            artifact=draft.artifact,
            affected=list(finding.affected or []),
            blast_radius=draft.blast_radius,
        )
        session.add(proposal)
        await session.flush()
        finding.proposed_actions = [
            *[a for a in (finding.proposed_actions or []) if a.get("playbook") != pb.name],
            {"summary": draft.summary, "playbook": pb.name, "proposal_id": str(proposal.id)},
        ]
        result.drafted.append(f"{pb.name} → {proposal.id}")
    await session.flush()
    return result


__all__ = [
    "DEFAULT_COOLDOWN",
    "DEFAULT_MIN_AGE",
    "PLAYBOOKS",
    "Playbook",
    "PlaybookResult",
    "playbook_for",
    "run_playbooks",
]
