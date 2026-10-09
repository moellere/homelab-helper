"""Phase 7 slice 3 — workload health findings, remediation playbooks, the approval listener,
and the daemon's --once pass. Fakes only."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from homelab_helper.adapters.kubernetes import K8sAdapter, K8sConfig, parse_workload
from homelab_helper.adapters.proxmox import ProxmoxAdapter, ProxmoxConfig
from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    AutonomyLevel,
    FindingKind,
    FindingStatus,
    ProposalOutcome,
    TrustDomain,
)
from homelab_helper.db.models import (
    ExecutionReceipt,
    ProposalLog,
    ReconciliationFinding,
    TrustHistory,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.approval import ApprovalResult
from homelab_helper.engine.argocd_drift import reconcile_argocd_drift
from homelab_helper.engine.k8s_workloads import reconcile_workload_health
from homelab_helper.engine.listener import MAX_ASKS, REASK_AFTER, ask_pending
from homelab_helper.engine.playbooks import PLAYBOOKS, playbook_for, run_playbooks
from homelab_helper.engine.trust import grant_cell, seed_domains

NOW = timedelta(0)  # drafts immediately: the debounce is tested on its own

_PVE = ProxmoxConfig(url="https://pve.test:8006", token_id="t@pam!x", token_secret="s")


@pytest.fixture
async def sessionmaker():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(eng)
    await eng.dispose()


def _workload(
    name: str,
    *,
    desired: int = 2,
    ready: int = 2,
    kind: str = "deployment",
    generation: int = 5,
    observed: int = 5,
    updated: int | None = None,
) -> dict[str, Any]:
    return parse_workload(
        {
            "kind": {
                "deployment": "Deployment",
                "statefulset": "StatefulSet",
                "daemonset": "DaemonSet",
            }[kind],
            "metadata": {"name": name, "namespace": "media", "generation": generation},
            "spec": {"replicas": desired},
            "status": {
                "readyReplicas": ready,
                "updatedReplicas": desired if updated is None else updated,
                "observedGeneration": observed,
                "desiredNumberScheduled": desired,
                "numberReady": ready,
                "updatedNumberScheduled": desired if updated is None else updated,
                "conditions": [{"type": "Available", "status": "True" if ready else "False"}],
            },
        }
    )


def _app(name: str, *, sync: str = "Synced", health: str = "Healthy") -> dict[str, Any]:
    return {
        "name": name,
        "namespace": name,
        "repo_url": "https://git.example/infra",
        "target_revision": "main",
        "sync_status": sync,
        "health_status": health,
        "out_of_sync_resources": [],
    }


# ---------------------------------------------------------- workload health


def test_parse_workload_reads_each_kind() -> None:
    d = _workload("web", desired=3, ready=1)
    assert (d["kind"], d["desired"], d["ready"], d["settled"]) == ("deployment", 3, 1, True)
    ds = _workload("agent", kind="daemonset", desired=4, ready=4)
    assert (ds["kind"], ds["desired"], ds["ready"]) == ("daemonset", 4, 4)
    rolling = _workload("web", desired=3, ready=1, updated=1)
    assert rolling["settled"] is False
    stale = _workload("web", desired=3, ready=1, generation=6, observed=5)
    assert stale["settled"] is False


async def test_unhealthy_workload_opens_then_resolves(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        r = await reconcile_workload_health(s, [_workload("web", ready=0), _workload("ok")])
        assert r.opened == ["media/deployment/web"]
        assert r.resolved == []
        row = (await s.execute(select(ReconciliationFinding))).scalar_one()
        assert row.kind is FindingKind.WORKLOAD_UNHEALTHY
        assert row.affected[0] == {"target_type": "workload", "target_id": "media/deployment/web"}
        assert row.severity.value == "HIGH".lower() or row.severity.name == "HIGH"
    async with session_scope(sessionmaker) as s:
        r = await reconcile_workload_health(s, [_workload("web", ready=0)])
        assert r.updated == ["media/deployment/web"]
    async with session_scope(sessionmaker) as s:
        r = await reconcile_workload_health(s, [_workload("web")])
        assert r.resolved == ["media/deployment/web"]
        row = (await s.execute(select(ReconciliationFinding))).scalar_one()
        assert row.status is FindingStatus.RESOLVED
    async with session_scope(sessionmaker) as s:  # absent workload: untouched (invariant #1)
        r = await reconcile_workload_health(s, [])
        assert r.resolved == []
        assert r.seen == 0


async def test_rollout_in_progress_is_not_a_finding(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        r = await reconcile_workload_health(s, [_workload("web", ready=1, updated=1)])
        assert r.unhealthy == []
        assert (await s.execute(select(ReconciliationFinding))).scalars().all() == []


# ---------------------------------------------------------------- playbooks


async def test_playbooks_draft_one_proposal_per_covered_finding(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_argocd_drift(s, [_app("app-loki", sync="OutOfSync")])
        await reconcile_workload_health(s, [_workload("web", ready=0)])
        r = await run_playbooks(s, min_age=NOW)
        assert len(r.drafted) == 2
        proposals = (
            (await s.execute(select(ProposalLog).order_by(ProposalLog.title))).scalars().all()
        )
        by_src = {p.proposed_by: p for p in proposals}
        assert set(by_src) == {"playbook:argocd-resync", "playbook:workload-restart"}
        sync = by_src["playbook:argocd-resync"]
        assert sync.artifact["action"]["action_kind"] == "argocd-sync"
        assert sync.artifact["action"]["target"]["application"] == "app-loki"
        assert sync.blast_radius == "single-service"
        assert sync.finding_id is not None
        restart = by_src["playbook:workload-restart"]
        assert restart.artifact["action"]["target"] == {
            "namespace": "media",
            "kind": "deployment",
            "name": "web",
        }
        finding = (
            await s.execute(
                select(ReconciliationFinding).where(ReconciliationFinding.id == restart.finding_id)
            )
        ).scalar_one()
        assert finding.proposed_actions[0]["playbook"] == "workload-restart"
        assert finding.proposed_actions[0]["proposal_id"] == str(restart.id)

        again = await run_playbooks(s, min_age=NOW)  # a pending proposal blocks a second draft
        assert again.drafted == []
        assert len(again.skipped_live) == 2


async def test_playbooks_respect_the_cooldown_after_a_decision(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_argocd_drift(s, [_app("app-loki", sync="OutOfSync")])
        first = await run_playbooks(s, min_age=NOW)
        assert len(first.drafted) == 1
        p = (await s.execute(select(ProposalLog))).scalar_one()
        p.outcome = ProposalOutcome.USER_REJECTED
        p.outcome_at = datetime.now(UTC)
        await s.flush()
        blocked = await run_playbooks(s, min_age=NOW)
        assert blocked.drafted == []
        assert len(blocked.skipped_cooldown) == 1
        later = await run_playbooks(s, when=datetime.now(UTC) + timedelta(hours=7), min_age=NOW)
        assert len(later.drafted) == 1


async def test_young_findings_wait_for_the_platform_to_self_heal(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        t0 = datetime.now(UTC)
        await reconcile_argocd_drift(s, [_app("app-blip", sync="OutOfSync")], when=t0)
        fresh = await run_playbooks(s, when=t0 + timedelta(minutes=1))
        assert fresh.drafted == []
        assert len(fresh.skipped_young) == 1
        aged = await run_playbooks(s, when=t0 + timedelta(minutes=16))
        assert len(aged.drafted) == 1


async def test_pending_draft_is_withdrawn_when_its_finding_resolves(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_argocd_drift(s, [_app("app-blip", sync="OutOfSync")])
        drafted = await run_playbooks(s, min_age=NOW)
        assert len(drafted.drafted) == 1
        await reconcile_argocd_drift(s, [_app("app-blip")])  # Argo healed it
        r = await run_playbooks(s, min_age=NOW)
        p = (await s.execute(select(ProposalLog))).scalar_one()
        assert p.outcome is ProposalOutcome.EXPIRED
        assert r.withdrawn == [str(p.id)]
        assert r.drafted == []


async def test_degraded_but_synced_app_gets_no_resync(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_argocd_drift(s, [_app("app-gate", health="Degraded")])
        row = (await s.execute(select(ReconciliationFinding))).scalar_one()
        assert {
            "type": "argocd_status",
            "sync": "Synced",
            "health": "Degraded",
        } in row.evidence_refs
        r = await run_playbooks(s, min_age=NOW)
        assert r.drafted == []
        assert r.no_playbook == 1


async def test_findings_without_a_playbook_are_left_alone(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(
            ReconciliationFinding(
                kind=FindingKind.INVENTORY_GAP,
                severity="low",
                fingerprint="x" * 16,
                title="dimm without serial",
                description="no serial reported",
                affected=[{"target_type": "host", "target_id": "h1"}],
                status=FindingStatus.OPEN,
                first_seen=datetime.now(UTC),
                last_seen=datetime.now(UTC),
            )
        )
        await s.flush()
        row = (await s.execute(select(ReconciliationFinding))).scalar_one()
        assert playbook_for(row) is None
        r = await run_playbooks(s, min_age=NOW)
        assert r.drafted == []
        assert r.no_playbook == 1
    assert {pb.name for pb in PLAYBOOKS} == {
        "argocd-resync",
        "workload-restart",
        "rightsize",
        "node-update",
    }


# ----------------------------------------------------------------- listener


def _proxmox_fake(requests: list[httpx.Request]) -> ProxmoxAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"status": "running"}})

    return ProxmoxAdapter(
        _PVE,
        client=httpx.AsyncClient(
            base_url=_PVE.url + "/api2/json", transport=httpx.MockTransport(handler)
        ),
    )


def _k8s_fake(calls: list[list[str]]) -> K8sAdapter:
    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        calls.append(list(args))
        if args[0] == "rollout" and args[1] == "history":
            return 0, "REVISION  CHANGE-CAUSE\n1  <none>\n2  <none>", ""
        if args[0] == "get":
            return 0, json.dumps({"spec": {"replicas": 2}, "status": {"readyReplicas": 0}}), ""
        return 0, "ok", ""

    return K8sAdapter(K8sConfig(), runner=runner)


class _Channel:
    name = "fake"

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def request(
        self, manifest, decision, *, proposal_id: str, title=None, why=None
    ) -> ApprovalResult:
        self.asked.append(proposal_id)
        self.titles = [*getattr(self, "titles", []), title]
        return ApprovalResult(approved=self.answer, channel=self.name, responder="test-phone")


async def test_listener_asks_once_and_executes_on_approve(sessionmaker) -> None:
    calls: list[list[str]] = []
    channel = _Channel(answer=True)

    async def adapters_for(manifest):
        return _proxmox_fake([]), _k8s_fake(calls), None, None, None

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "workload-restart",
            "single-service",
            AutonomyLevel.CONFIRM,
            actor="op",
        )
        await reconcile_workload_health(s, [_workload("web", ready=0)])
        await run_playbooks(s, min_age=NOW)
        r = await ask_pending(s, channel=channel, adapters_for=adapters_for)
        assert len(r.asked) == 1
        assert len(r.executed) == 1
        assert "containers/workload-restart/single-service -> succeeded" in r.executed[0]
        receipt = (await s.execute(select(ExecutionReceipt))).scalar_one()
        assert receipt.actor == "listener"
        assert receipt.approval["responder"] == "test-phone"
        proposal = (await s.execute(select(ProposalLog))).scalar_one()
        assert proposal.outcome is ProposalOutcome.USER_ACCEPTED
        again = await ask_pending(s, channel=channel, adapters_for=adapters_for)
        assert again.asked == []
        assert again.already_asked == 0
    assert any(c[:2] == ["rollout", "restart"] for c in calls)


async def test_listener_never_reasks_a_denied_proposal(sessionmaker) -> None:
    channel = _Channel(answer=False)

    async def adapters_for(manifest):
        return _proxmox_fake([]), _k8s_fake([]), None, None, None

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "workload-restart",
            "single-service",
            AutonomyLevel.CONFIRM,
            actor="op",
        )
        await reconcile_workload_health(s, [_workload("web", ready=0)])
        await run_playbooks(s, min_age=NOW)
        first = await ask_pending(s, channel=channel, adapters_for=adapters_for)
        assert len(first.asked) == 1
        assert len(first.declined) == 1
        second = await ask_pending(s, channel=channel, adapters_for=adapters_for)
        assert second.asked == []
        assert second.already_asked == 1
        proposal = (await s.execute(select(ProposalLog))).scalar_one()
        assert proposal.outcome is ProposalOutcome.PENDING
        assert (await s.execute(select(ExecutionReceipt))).scalars().all() == []
        denial = (
            await s.execute(select(TrustHistory).where(TrustHistory.event == "approval"))
        ).scalar_one()
        assert denial.detail["approved"] is False
    assert channel.asked
    assert len(channel.asked) == 1


async def _two_pending_restarts(s) -> None:
    await seed_domains(s)
    await grant_cell(
        s,
        TrustDomain.CONTAINERS,
        "workload-restart",
        "single-service",
        AutonomyLevel.CONFIRM,
        actor="op",
    )
    await reconcile_workload_health(s, [_workload("web", ready=0), _workload("api", ready=0)])
    await run_playbooks(s, min_age=NOW)


async def test_every_prompt_goes_out_before_any_waits_out(sessionmaker) -> None:
    """Concurrent asking: each fake prompt only approves once *both* are on the phone."""
    both_out = asyncio.Event()
    sent: list[str] = []

    class _Together(_Channel):
        async def request(self, manifest, decision, *, proposal_id, title=None, why=None):
            sent.append(title)
            if len(sent) == 2:  # noqa: PLR2004 - the two proposals
                both_out.set()
            try:
                await asyncio.wait_for(both_out.wait(), timeout=1)
            except TimeoutError:
                return ApprovalResult(
                    approved=False, channel="fake", detail={"reason": "no answer"}
                )
            return ApprovalResult(approved=True, channel="fake", responder="phone")

    async def adapters_for(manifest):
        return _proxmox_fake([]), _k8s_fake([]), None, None, None

    async with session_scope(sessionmaker) as s:
        await _two_pending_restarts(s)
        r = await ask_pending(s, channel=_Together(answer=True), adapters_for=adapters_for)
    assert len(r.executed) == 2
    assert all(
        t and t.startswith("Rollout restart") for t in sent
    )  # the proposal title, not boilerplate


async def test_an_expired_prompt_is_asked_again_later_but_not_forever(sessionmaker) -> None:
    class _Unseen(_Channel):
        async def request(self, manifest, decision, *, proposal_id, title=None, why=None):
            self.asked.append(proposal_id)
            return ApprovalResult(
                approved=False, channel="fake", detail={"reason": "no answer within 900s"}
            )

    async def adapters_for(manifest):
        return _proxmox_fake([]), _k8s_fake([]), None, None, None

    channel = _Unseen(answer=False)
    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "workload-restart",
            "single-service",
            AutonomyLevel.CONFIRM,
            actor="op",
        )
        await reconcile_workload_health(s, [_workload("web", ready=0)])
        await run_playbooks(s, min_age=NOW)

        async def age_asks() -> None:
            for event in (
                await s.execute(select(TrustHistory).where(TrustHistory.event == "approval"))
            ).scalars():
                event.at = event.at - REASK_AFTER - timedelta(minutes=1)
            await s.flush()

        assert len((await ask_pending(s, channel=channel, adapters_for=adapters_for)).asked) == 1
        soon = await ask_pending(s, channel=channel, adapters_for=adapters_for)
        assert soon.asked == []  # too soon after the last expiry
        for _ in range(MAX_ASKS - 1):
            await age_asks()
            assert (
                len((await ask_pending(s, channel=channel, adapters_for=adapters_for)).asked) == 1
            )
        await age_asks()
        done = await ask_pending(s, channel=channel, adapters_for=adapters_for)
        assert done.asked == []  # MAX_ASKS reached; the proposal waits for the operator's CLI
        proposal = (await s.execute(select(ProposalLog))).scalar_one()
        assert proposal.outcome is ProposalOutcome.PENDING
    assert len(channel.asked) == MAX_ASKS


async def test_listener_skips_cells_at_propose_without_asking(sessionmaker) -> None:
    channel = _Channel(answer=True)

    async def adapters_for(manifest):
        raise AssertionError("adapters must not be built for a refused proposal")

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)  # every cell at PROPOSE
        await reconcile_workload_health(s, [_workload("web", ready=0)])
        await run_playbooks(s, min_age=NOW)
        r = await ask_pending(s, channel=channel, adapters_for=adapters_for)
        assert r.asked == []
        assert len(r.refused) == 1
        assert "propose" in r.refused[0]
    assert channel.asked == []


async def test_listener_ignores_operator_authored_proposals(sessionmaker) -> None:
    channel = _Channel(answer=True)

    async def adapters_for(manifest):
        return _proxmox_fake([]), _k8s_fake([]), None, None, None

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "workload-restart",
            "single-service",
            AutonomyLevel.CONFIRM,
            actor="op",
        )
        await reconcile_workload_health(s, [_workload("web", ready=0)])
        await run_playbooks(s, min_age=NOW)
        p = (await s.execute(select(ProposalLog))).scalar_one()
        p.proposed_by = "moellere"  # hand-authored at the CLI: the listener leaves it alone
        await s.flush()
        r = await ask_pending(s, channel=channel, adapters_for=adapters_for)
        assert r.asked == []
        assert r.executed == []


# -------------------------------------------------------------------- daemon


def test_daemon_once_runs_each_job(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from homelab_helper.cli import daemon as mod
    from homelab_helper.cli.main import app

    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/d.db")
    seen: list[str] = []

    async def fake_discovery(sources):
        seen.append(f"discovery:{','.join(sources)}")
        return {"k8s": {"nodes_seen": 0}}

    async def fake_playbooks():
        seen.append("playbooks")
        return {"drafted": []}

    async def fake_listen():
        seen.append("listen")
        return {"asked": []}

    monkeypatch.setattr(mod, "run_discovery_pass", fake_discovery)
    monkeypatch.setattr(mod, "run_playbook_pass", fake_playbooks)
    monkeypatch.setattr(mod, "run_listen_pass", fake_listen)
    result = CliRunner().invoke(app, ["daemon", "run", "--once", "--sources", "k8s"])
    assert result.exit_code == 0, result.output
    assert seen == ["discovery:k8s", "playbooks", "listen"]

    result = CliRunner().invoke(app, ["daemon", "run", "--once", "--sources", "", "--no-ask"])
    assert result.exit_code == 0
    assert seen[-1] == "playbooks"


def test_daemon_default_sources_are_the_configured_ones(tmp_path, monkeypatch) -> None:
    """Phase 9.2: no --sources means what `helper config` says is configured —
    not the author's stack. conftest strips every HOMELAB_HELPER_* variable, so
    Argo CD is unconfigured here; k8s needs no variables and is always listed."""
    from typer.testing import CliRunner

    from homelab_helper.cli import daemon as mod
    from homelab_helper.cli.main import app

    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/d.db")
    monkeypatch.setenv("HOMELAB_HELPER_PROXMOX_URL", "https://pve.test")
    monkeypatch.setenv("HOMELAB_HELPER_PROXMOX_TOKEN_ID", "u@pve!t")
    monkeypatch.setenv("HOMELAB_HELPER_PROXMOX_TOKEN_SECRET", "s")
    seen: list[list[str]] = []

    async def fake_discovery(sources):
        seen.append(list(sources))
        return {}

    monkeypatch.setattr(mod, "run_discovery_pass", fake_discovery)
    result = CliRunner().invoke(app, ["daemon", "run", "--once", "--no-ask", "--no-playbooks"])
    assert result.exit_code == 0, result.output
    assert seen == [["proxmox", "k8s"]]
