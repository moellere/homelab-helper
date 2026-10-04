"""Phase 7 slice 4 — the operator is told after a run they were not asked about."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from homelab_helper.db.base import Base
from homelab_helper.db.enums import AutonomyLevel, TrustDomain
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.approval import ApprovalResult, HomeAssistantApprovalConfig
from homelab_helper.engine.escalation import EscalationResult
from homelab_helper.engine.executor import execute_proposal
from homelab_helper.engine.notify import (
    ExecutionNotice,
    HomeAssistantNotifier,
    notifier_from_env,
    notify_after_run,
    render,
    should_notify,
)
from homelab_helper.engine.trust import grant_cell, seed_domains
from tests.test_phase7_execution import (
    make_k8s,
    make_proposal,
    make_proxmox,
    migrate_artifact,
    workload_artifact,
)


@pytest.fixture
async def sessionmaker():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(eng)
    await eng.dispose()


class _Recorder:
    name = "recorder"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[ExecutionNotice] = []
        self.fail = fail

    async def send(self, notice: ExecutionNotice) -> None:
        if self.fail:
            raise RuntimeError("phone unreachable")
        self.sent.append(notice)


def _notice(**kw: Any) -> ExecutionNotice:
    base: dict[str, Any] = {
        "receipt_id": uuid.uuid4(),
        "proposal_id": uuid.uuid4(),
        "title": "Restart homepage",
        "cell": "containers/workload-restart/single-service",
        "target": "deployment/homepage in default",
        "level": AutonomyLevel.AUTONOMOUS,
        "outcome": "succeeded",
        "error": None,
        "duration_ms": 420,
        "actor": "listener",
        "rollback_available": True,
    }
    return ExecutionNotice(**{**base, **kw})


def _escalation(event: str) -> EscalationResult:
    return EscalationResult(
        event=event,
        previous_level=AutonomyLevel.CONFIRM,
        level=AutonomyLevel.AUTONOMOUS if event == "auto-promote" else AutonomyLevel.PROPOSE,
        clean_streak=0,
        on_probation=event == "demote",
        reason=event,
    )


@pytest.mark.parametrize(
    ("level", "outcome", "event", "expected"),
    [
        (AutonomyLevel.AUTONOMOUS, "succeeded", None, True),
        (AutonomyLevel.AUTONOMOUS, "failed", None, True),
        (AutonomyLevel.CONFIRM, "succeeded", None, False),
        (AutonomyLevel.CONFIRM, "failed", None, True),
        (AutonomyLevel.CONFIRM, "succeeded", "streak", False),
        (AutonomyLevel.CONFIRM, "succeeded", "auto-promote", True),
        (AutonomyLevel.CONFIRM, "failed", "demote", True),
    ],
)
def test_should_notify_policy(level, outcome, event, expected) -> None:
    n = _notice(level=level, outcome=outcome, escalation=_escalation(event) if event else None)
    assert should_notify(n) is expected


def test_render_carries_the_undo_one_liner_only_for_reversible_successes() -> None:
    rid = uuid.uuid4()
    title, body = render(_notice(receipt_id=rid))
    assert title.startswith("✓")
    assert f"helper exec rollback {str(rid)[:8]}" in body
    assert "unattended" in body
    _, failed = render(_notice(outcome="failed", error="kubectl: boom"))
    assert "rollback" not in failed
    assert "kubectl: boom" in failed
    _, promoted = render(
        _notice(level=AutonomyLevel.CONFIRM, escalation=_escalation("auto-promote"))
    )
    assert "auto-promote: confirm → autonomous" in promoted


async def test_notify_after_run_never_raises_and_reports_what_happened() -> None:
    assert await notify_after_run(None, _notice(level=AutonomyLevel.CONFIRM)) is None
    assert await notify_after_run(None, _notice()) == "unconfigured"
    ok = _Recorder()
    assert await notify_after_run(ok, _notice()) == "sent via recorder"
    assert len(ok.sent) == 1
    bad = _Recorder(fail=True)
    assert (await notify_after_run(bad, _notice()) or "").startswith("failed: phone unreachable")


async def test_autonomous_run_notifies_after_the_receipt_is_written(sessionmaker) -> None:
    calls: list[list[str]] = []
    adapter, k8s = make_proxmox([]), make_k8s(calls)
    recorder = _Recorder()

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
        result = await execute_proposal(
            s, p, adapter, actor="listener", k8s_adapter=k8s, notifier=recorder
        )
    await adapter.aclose()
    assert result.outcome == "succeeded"
    assert result.notification == "sent via recorder"
    (notice,) = recorder.sent
    assert notice.receipt_id == result.receipt_id
    assert notice.level is AutonomyLevel.AUTONOMOUS
    assert notice.actor == "listener"
    assert notice.rollback_available is True


async def test_confirmed_success_is_silent_but_a_failure_is_not(sessionmaker) -> None:
    recorder = _Recorder()

    async def confirm(manifest, decision) -> ApprovalResult:
        return ApprovalResult(approved=True, channel="home-assistant", responder="pixel")

    async with session_scope(sessionmaker) as s:
        await seed_domains(s)
        await grant_cell(
            s, TrustDomain.HYPERVISOR, "migrate", "single-host", AutonomyLevel.CONFIRM, actor="op"
        )
        adapter = make_proxmox([])
        p = await make_proposal(s, migrate_artifact(), "single-host")
        quiet = await execute_proposal(
            s, p, adapter, actor="agent:mcp", confirm_cb=confirm, notifier=recorder
        )
        await adapter.aclose()
        assert quiet.outcome == "succeeded"
        assert quiet.notification is None
        assert recorder.sent == []

        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "workload-restart",
            "single-service",
            AutonomyLevel.CONFIRM,
            actor="op",
        )
        adapter = make_proxmox([])
        p2 = await make_proposal(s, workload_artifact(), "single-service")
        loud = await execute_proposal(  # no k8s adapter: dispatch fails
            s, p2, adapter, actor="agent:mcp", confirm_cb=confirm, notifier=recorder
        )
        await adapter.aclose()
    assert loud.outcome == "failed"
    assert loud.notification == "sent via recorder"
    assert recorder.sent[-1].outcome == "failed"


async def test_a_dead_phone_does_not_change_the_run(sessionmaker) -> None:
    calls: list[list[str]] = []
    adapter, k8s = make_proxmox([]), make_k8s(calls)
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
        result = await execute_proposal(
            s, p, adapter, actor="listener", k8s_adapter=k8s, notifier=_Recorder(fail=True)
        )
    await adapter.aclose()
    assert result.outcome == "succeeded"
    assert (result.notification or "").startswith("failed:")


async def test_home_assistant_notifier_posts_a_plain_notification() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"path": request.url.path, **json.loads(request.content)})
        return httpx.Response(200, json=[])

    config = HomeAssistantApprovalConfig(
        url="https://ha.test", token="t", notify_service="notify.mobile_app_pixel"
    )
    notifier = HomeAssistantNotifier(config)
    original = httpx.AsyncClient

    class _Client(original):
        def __init__(self, *a: Any, **kw: Any) -> None:
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    httpx.AsyncClient = _Client  # type: ignore[misc]
    try:
        await notifier.send(_notice())
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]
    (call,) = sent
    assert call["path"] == "/api/services/notify/mobile_app_pixel"
    assert call["title"].startswith("✓ homelab-helper")
    assert call["data"]["group"] == "homelab-helper"
    assert "actions" not in call["data"]


def test_notifier_from_env_is_none_when_unconfigured(monkeypatch) -> None:
    for var in (
        "HOMELAB_HELPER_HASS_URL",
        "HOMELAB_HELPER_HASS_TOKEN",
        "HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE",
    ):
        monkeypatch.delenv(var, raising=False)
    assert notifier_from_env() is None
    monkeypatch.setenv("HOMELAB_HELPER_HASS_URL", "https://ha.test")
    monkeypatch.setenv("HOMELAB_HELPER_HASS_TOKEN", "t")
    monkeypatch.setenv("HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE", "notify.mobile_app_pixel")
    assert isinstance(notifier_from_env(), HomeAssistantNotifier)
