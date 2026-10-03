"""Phase 7 — migrate + Kubernetes workload actions, channel approvals, rollback strategies.

Everything here runs against fakes: a MockTransport Proxmox and an injected
kubectl runner. No lab is touched.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from homelab_helper.adapters.kubernetes import K8sAdapter, K8sConfig, KubeError
from homelab_helper.adapters.proxmox import ProxmoxAdapter, ProxmoxConfig
from homelab_helper.db.base import Base
from homelab_helper.db.enums import AutonomyLevel, ProposalOutcome, TrustDomain
from homelab_helper.db.models import ExecutionReceipt, ProposalLog, TrustHistory
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.approval import (
    ApprovalResult,
    HomeAssistantApprovalChannel,
    HomeAssistantApprovalConfig,
)
from homelab_helper.engine.escalation import is_promotable
from homelab_helper.engine.executor import (
    ExecutionRefused,
    execute_proposal,
    parse_manifest,
    rollback_receipt,
)
from homelab_helper.engine.manifest import (
    ManifestError,
    build_artifact,
    build_workload_artifact,
)
from homelab_helper.engine.rollback import (
    PRIOR_NODE,
    PRIOR_REPLICAS,
    ROLLOUT_UNDO,
    RollbackPlan,
    select_strategy,
)
from homelab_helper.engine.trust import grant_cell, seed_domains

_PVE = ProxmoxConfig(url="https://pve.test:8006", token_id="t@pam!x", token_secret="s")


# --------------------------------------------------------------------- fakes


def make_proxmox(requests: list[httpx.Request], *, online_nodes=("pve1", "pve2")) -> ProxmoxAdapter:
    """A two-node cluster with one running guest; migrate moves nothing but is recorded."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/cluster/status"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"type": "cluster", "name": "lab", "quorate": 1, "nodes": 2},
                        *[{"type": "node", "name": n, "online": 1} for n in online_nodes],
                    ]
                },
            )
        if path.endswith("/status/current"):
            return httpx.Response(200, json={"data": {"status": "running", "name": "web01"}})
        if path.endswith("/migrate"):
            return httpx.Response(200, json={"data": "UPID:pve1:0001:qmigrate"})
        return httpx.Response(200, json={"data": "UPID:pve1:0000:other"})

    client = httpx.AsyncClient(
        base_url=_PVE.url.rstrip("/") + "/api2/json", transport=httpx.MockTransport(handler)
    )
    return ProxmoxAdapter(_PVE, client=client)


def make_k8s(
    calls: list[list[str]], *, replicas: int = 2, revisions=(1, 2), fail: str | None = None
):
    """An injected kubectl: records every argv, answers reads, fails the verb named by ``fail``."""

    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        argv = list(args)
        calls.append(argv)
        verb = argv[0] if argv else ""
        if fail and fail in argv:
            return 1, "", f"error: {fail} failed"
        if verb == "get":
            body = {
                "metadata": {"generation": 7},
                "spec": {"replicas": replicas},
                "status": {"readyReplicas": replicas, "observedGeneration": 7},
            }
            return 0, json.dumps(body), ""
        if verb == "rollout" and argv[1] == "history":
            lines = ["REVISION  CHANGE-CAUSE"] + [f"{r}         <none>" for r in revisions]
            return 0, "\n".join(lines), ""
        return 0, f"{' '.join(argv[:2])} ok", ""

    return K8sAdapter(K8sConfig(), runner=runner)


@pytest.fixture
async def sessionmaker():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(eng)
    await eng.dispose()


async def make_proposal(session, artifact: dict[str, Any], blast: str) -> ProposalLog:
    p = ProposalLog(title="t", artifact=artifact, blast_radius=blast, proposed_by="agent:mcp")
    session.add(p)
    await session.flush()
    return p


def migrate_artifact(**kw: Any) -> dict[str, Any]:
    return build_artifact(
        action_kind="migrate", node="pve1", vmid=105, vm_kind="qemu", target_node="pve2", **kw
    )


def workload_artifact(action_kind: str = "workload-restart", **kw: Any) -> dict[str, Any]:
    return build_workload_artifact(
        action_kind=action_kind, namespace="media", kind="deployment", name="jellyfin", **kw
    )


# ------------------------------------------------------------ manifest shapes


async def test_migrate_manifest_parses_and_names_both_nodes(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        p = await make_proposal(s, migrate_artifact(), "single-host")
        m = parse_manifest(p)
    assert m.cell_key == "hypervisor/migrate/single-host"
    assert m.hostnames == ("pve1", "pve2")
    assert m.target_label == "qemu/105 on pve1 -> pve2"
    assert select_strategy(m) == PRIOR_NODE


async def test_workload_manifest_parses_into_the_containers_cell(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        p = await make_proposal(
            s, workload_artifact("workload-scale", replicas=3), "single-service"
        )
        m = parse_manifest(p)
    assert m.is_workload
    assert m.cell_key == "containers/workload-scale/single-service"
    assert m.target_label == "deployment/jellyfin in media"
    assert m.replicas == 3
    assert select_strategy(m) == PRIOR_REPLICAS


@pytest.mark.parametrize(
    ("artifact", "needle"),
    [
        (
            {
                "kind": "action",
                "action": {
                    "domain": "hypervisor",
                    "action_kind": "migrate",
                    "target": {"node": "pve1", "vmid": 1, "vm_kind": "qemu"},
                },
            },
            "target_node",
        ),
        (
            {
                "kind": "action",
                "action": {
                    "domain": "hypervisor",  # a workload may not shop for the hypervisor cell
                    "action_kind": "workload-restart",
                    "target": {"namespace": "a", "kind": "deployment", "name": "b"},
                },
            },
            "does not match a Kubernetes workload",
        ),
        (
            {
                "kind": "action",
                "action": {
                    "domain": "containers",
                    "action_kind": "workload-scale",
                    "target": {"namespace": "a", "kind": "deployment", "name": "b"},
                },
            },
            "replicas",
        ),
        (
            {
                "kind": "action",
                "action": {
                    "domain": "containers",
                    "action_kind": "restart",
                    "target": {"namespace": "a", "kind": "deployment", "name": "b"},
                },
            },
            "needs a guest target",
        ),
    ],
)
async def test_executor_rejects_malformed_phase7_manifests(sessionmaker, artifact, needle) -> None:
    async with session_scope(sessionmaker) as s:
        p = await make_proposal(s, artifact, "single-service")
        with pytest.raises(ManifestError, match=needle):
            parse_manifest(p)


def test_authoring_side_refuses_the_same_holes() -> None:
    with pytest.raises(ManifestError):
        build_artifact(action_kind="migrate", node="pve1", vmid=1, vm_kind="qemu")
    with pytest.raises(ManifestError):
        build_artifact(
            action_kind="migrate", node="pve1", vmid=1, vm_kind="qemu", target_node="pve1"
        )
    with pytest.raises(ManifestError):
        build_workload_artifact(
            action_kind="workload-scale", namespace="a", kind="deployment", name="b"
        )
    with pytest.raises(ManifestError):
        build_workload_artifact(action_kind="start", namespace="a", kind="deployment", name="b")


def test_new_kinds_are_promotable_only_at_low_blast() -> None:
    assert is_promotable("migrate", "single-host")
    assert is_promotable("workload-restart", "single-service")
    assert is_promotable("workload-scale", "single-service")
    assert not is_promotable("migrate", "cluster")


# ------------------------------------------------------------- migrate flow


async def test_migrate_dispatches_after_confirm_and_captures_prior_node(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_proxmox(requests)

    async def confirm(manifest, decision) -> bool:
        return True

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s, TrustDomain.HYPERVISOR, "migrate", "single-host", AutonomyLevel.CONFIRM, actor="op"
        )
        p = await make_proposal(s, migrate_artifact(), "single-host")
        result = await execute_proposal(s, p, adapter, actor="op", confirm_cb=confirm)
        assert result.outcome == "succeeded"
        receipt = (await s.execute(select(ExecutionReceipt))).scalar_one()
    await adapter.aclose()

    migrate = [r for r in requests if r.url.path.endswith("/migrate")]
    assert len(migrate) == 1
    assert migrate[0].method == "POST"
    assert migrate[0].url.params["target"] == "pve2"
    assert migrate[0].url.params["online"] == "1"
    assert receipt.action["dispatched"] == "migrate -> pve2"
    assert receipt.rollback_state["strategy"] == PRIOR_NODE
    assert receipt.rollback_state["verified"] is True
    assert receipt.rollback_state["prior"]["prior_node"] == "pve1"


async def test_migrate_rollback_moves_the_guest_back(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_proxmox(requests)

    async def confirm(manifest, decision) -> bool:
        return True

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s, TrustDomain.HYPERVISOR, "migrate", "single-host", AutonomyLevel.CONFIRM, actor="op"
        )
        p = await make_proposal(s, migrate_artifact(), "single-host")
        await execute_proposal(s, p, adapter, actor="op", confirm_cb=confirm)
        receipt = (await s.execute(select(ExecutionReceipt))).scalar_one()
        requests.clear()
        undo = await rollback_receipt(s, receipt, adapter, actor="op")
    await adapter.aclose()
    assert "back to pve1" in undo.detail
    back = [r for r in requests if r.url.path.endswith("/migrate")]
    assert back[0].url.path == "/api2/json/nodes/pve2/qemu/105/migrate"
    assert back[0].url.params["target"] == "pve1"


async def test_migrate_is_unverifiable_when_a_node_is_offline(sessionmaker) -> None:
    """An offline target node means no way back, so AUTONOMOUS degrades to CONFIRM."""
    requests: list[httpx.Request] = []
    adapter = make_proxmox(requests, online_nodes=("pve1",))

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.HYPERVISOR,
            "migrate",
            "single-host",
            AutonomyLevel.AUTONOMOUS,
            actor="op",
        )
        p = await make_proposal(s, migrate_artifact(), "single-host")
        with pytest.raises(ExecutionRefused, match="confirmation"):
            await execute_proposal(s, p, adapter, actor="op")
    await adapter.aclose()
    assert not [r for r in requests if r.url.path.endswith("/migrate")]


# --------------------------------------------------------- workload flows


async def test_workload_restart_runs_kubectl_and_can_undo(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    calls: list[list[str]] = []
    adapter, k8s = make_proxmox(requests), make_k8s(calls)

    async def confirm(manifest, decision) -> bool:
        return True

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
        p = await make_proposal(s, workload_artifact(), "single-service")
        result = await execute_proposal(
            s, p, adapter, actor="op", confirm_cb=confirm, k8s_adapter=k8s
        )
        assert result.outcome == "succeeded"
        receipt = (await s.execute(select(ExecutionReceipt))).scalar_one()
        assert receipt.rollback_state["strategy"] == ROLLOUT_UNDO
        assert receipt.rollback_state["prior"]["revision"] == 2
        assert receipt.rollback_state["workload_name"] == "jellyfin"
        assert p.outcome is ProposalOutcome.USER_ACCEPTED

        undo = await rollback_receipt(s, receipt, adapter, actor="op", k8s_adapter=k8s)
    await adapter.aclose()

    verbs = [" ".join(c[:2]) for c in calls]
    assert "rollout history" in verbs  # the read-only probe, before authorization
    assert "rollout restart" in verbs
    assert "rollout undo" in verbs
    assert calls[-1][-1] == "--to-revision=2"
    assert "pre-restart" in undo.detail
    assert not [r for r in requests if r.method != "GET"], "no Proxmox write for a workload action"


async def test_workload_scale_captures_prior_replicas(sessionmaker) -> None:
    calls: list[list[str]] = []
    adapter, k8s = make_proxmox([]), make_k8s(calls, replicas=2)

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "workload-scale",
            "single-service",
            AutonomyLevel.AUTONOMOUS,
            actor="op",
        )
        p = await make_proposal(
            s, workload_artifact("workload-scale", replicas=4), "single-service"
        )
        result = await execute_proposal(s, p, adapter, actor="op", k8s_adapter=k8s)
        assert result.decision.level is AutonomyLevel.AUTONOMOUS
        receipt = (await s.execute(select(ExecutionReceipt))).scalar_one()
        assert receipt.rollback_state["prior"]["replicas"] == 2
        assert receipt.action["dispatched"] == "scale --replicas=4"
        requests_after = len(calls)
        await rollback_receipt(s, receipt, adapter, actor="op", k8s_adapter=k8s)
    await adapter.aclose()
    assert calls[requests_after:][-1][-1] == "--replicas=2"


async def test_workload_action_without_k8s_adapter_fails_the_receipt(sessionmaker) -> None:
    """No kubectl is an unverifiable-rollback + failed dispatch, never a crash."""
    adapter = make_proxmox([])

    async def confirm(manifest, decision) -> bool:
        return True

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
        p = await make_proposal(s, workload_artifact(), "single-service")
        result = await execute_proposal(s, p, adapter, actor="op", confirm_cb=confirm)
        assert result.outcome == "failed"
        assert "Kubernetes" in (result.error or "")
        assert p.outcome is ProposalOutcome.PENDING
    await adapter.aclose()


async def test_failed_kubectl_demotes_the_cell(sessionmaker) -> None:
    calls: list[list[str]] = []
    adapter, k8s = make_proxmox([]), make_k8s(calls, fail="restart")

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "workload-restart",
            "single-service",
            AutonomyLevel.AUTONOMOUS,
            actor="op",
        )
        p = await make_proposal(s, workload_artifact(), "single-service")
        result = await execute_proposal(s, p, adapter, actor="op", k8s_adapter=k8s)
        assert result.outcome == "failed"
        assert result.escalation is not None
        assert result.escalation.demoted
    await adapter.aclose()


# ------------------------------------------------------ channel approvals


async def test_channel_approval_is_recorded_on_the_audit_spine(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_proxmox(requests)

    async def confirm(manifest, decision) -> ApprovalResult:
        return ApprovalResult(approved=True, channel="home-assistant", responder="pixel")

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s, TrustDomain.HYPERVISOR, "migrate", "single-host", AutonomyLevel.CONFIRM, actor="op"
        )
        p = await make_proposal(s, migrate_artifact(), "single-host")
        result = await execute_proposal(s, p, adapter, actor="agent:mcp", confirm_cb=confirm)
        assert result.outcome == "succeeded"
        approvals = (
            (await s.execute(select(TrustHistory).where(TrustHistory.event == "approval")))
            .scalars()
            .all()
        )
    await adapter.aclose()
    assert len(approvals) == 1
    assert approvals[0].proposal_id == p.id
    assert approvals[0].detail["channel"] == "home-assistant"
    assert approvals[0].detail["responder"] == "pixel"
    assert approvals[0].detail["approved"] is True


async def test_channel_denial_is_recorded_and_nothing_runs(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    adapter = make_proxmox(requests)

    async def confirm(manifest, decision) -> ApprovalResult:
        return ApprovalResult(
            approved=False, channel="home-assistant", detail={"reason": "timeout"}
        )

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s, TrustDomain.HYPERVISOR, "migrate", "single-host", AutonomyLevel.CONFIRM, actor="op"
        )
        p = await make_proposal(s, migrate_artifact(), "single-host")
        with pytest.raises(ExecutionRefused, match="declined via home-assistant"):
            await execute_proposal(s, p, adapter, actor="agent:mcp", confirm_cb=confirm)
        assert p.outcome is ProposalOutcome.PENDING
        assert (await s.execute(select(ExecutionReceipt))).scalars().all() == []
        denial = (
            await s.execute(select(TrustHistory).where(TrustHistory.event == "approval"))
        ).scalar_one()
    await adapter.aclose()
    assert denial.event == "approval"
    assert denial.detail["approved"] is False
    assert denial.detail["reason"] == "timeout"
    assert not [r for r in requests if r.url.path.endswith("/migrate")]


# --------------------------------------------------- the HA channel itself


def _channel(sent: list[dict[str, Any]], answer: str | None):
    cfg = HomeAssistantApprovalConfig(
        url="https://ha.test", token="t", notify_service="notify.mobile_app_pixel", timeout_s=1
    )

    async def sender(payload: dict[str, Any]) -> None:
        sent.append(payload)

    async def listener(approve_id: str, deny_id: str, timeout_s: float) -> dict[str, Any] | None:
        if answer is None:
            return None
        return {"action": approve_id if answer == "approve" else deny_id, "device_id": "pixel"}

    return HomeAssistantApprovalChannel(cfg, sender=sender, listener=listener)


class _Manifest:
    action_kind = "migrate"
    target_label = "qemu/105 on pve1 -> pve2"
    cell_key = "hypervisor/migrate/single-host"


class _Decision:
    reasons = ("cell hypervisor/migrate/single-host is CONFIRM",)


@pytest.mark.parametrize(
    ("answer", "approved"), [("approve", True), ("deny", False), (None, False)]
)
async def test_home_assistant_channel_round_trip(answer, approved) -> None:
    sent: list[dict[str, Any]] = []
    channel = _channel(sent, answer)
    result = await channel.request(_Manifest(), _Decision(), proposal_id="abc123")  # type: ignore[arg-type]
    assert result.approved is approved
    assert result.channel == "home-assistant"
    assert sent[0]["data"]["actions"] == [
        {"action": "HELPER_APPROVE_abc123", "title": "Approve"},
        {"action": "HELPER_DENY_abc123", "title": "Deny"},
    ]
    assert "hypervisor/migrate/single-host" in sent[0]["message"]
    assert "Expand this notification" in sent[0]["message"]
    assert sent[0]["data"]["clickAction"] == "noAction"  # a plain tap is not an answer
    if answer is None:
        assert "no answer within 1s" in result.detail["reason"]
    else:
        assert result.responder == "pixel"


def test_channel_config_names_what_is_missing(monkeypatch) -> None:
    for var in (
        "HOMELAB_HELPER_HASS_URL",
        "HOMELAB_HELPER_HASS_TOKEN",
        "HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(Exception, match="HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE"):
        HomeAssistantApprovalConfig.from_env()


# --------------------------------------------------------- receipt shapes


def test_workload_plan_round_trips_and_guest_plan_still_requires_ids() -> None:
    plan = RollbackPlan(
        strategy=PRIOR_REPLICAS,
        verified=True,
        evidence="",
        state={"prior": {"replicas": 2}},
        captured_at="2026-10-03T00:00:00+00:00",
        namespace="media",
        workload_kind="deployment",
        workload_name="jellyfin",
    )
    back = RollbackPlan.from_receipt_state(plan.as_receipt_state())
    assert back.is_workload
    assert back.workload_name == "jellyfin"
    assert back.state["prior"]["replicas"] == 2
    with pytest.raises(Exception, match="node, vmid, vm_kind"):
        RollbackPlan.from_receipt_state({"strategy": PRIOR_NODE})


async def test_k8s_adapter_refuses_to_scale_a_daemonset() -> None:
    k8s = make_k8s([])
    with pytest.raises(KubeError):
        await k8s.scale_workload("kube-system", "daemonset", "fluentd", 0)
