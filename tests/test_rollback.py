"""Rollback orchestration tests — verification is earned, never claimed.

The load-bearing assertions: a manifest cannot certify its own reversibility,
verification is read-only (no snapshot is taken before the gate authorizes the
action), capture happens only after, and restore drives the guest back to the
state captured before dispatch.
"""

from __future__ import annotations

import httpx
import pytest

from homelab_helper.adapters.proxmox import ProxmoxAdapter, ProxmoxConfig
from homelab_helper.db.enums import TrustDomain
from homelab_helper.engine.executor import ActionManifest
from homelab_helper.engine.rollback import (
    PRIOR_POWER_STATE,
    SNAPSHOT,
    RollbackError,
    RollbackPlan,
    capture_rollback,
    restore,
    select_strategy,
    verify_rollback,
)

_CONFIG = ProxmoxConfig(url="https://pve.test:8006", token_id="t@pam!x", token_secret="s")


def make_adapter(
    requests: list[httpx.Request],
    *,
    status: str = "running",
    status_code: int = 200,
    snapshot_code: int = 200,
) -> ProxmoxAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/status/current"):
            if status_code >= 400:
                return httpx.Response(status_code, json={"errors": "unreachable"})
            return httpx.Response(200, json={"data": {"status": status, "name": "web01"}})
        if path.endswith("/snapshot"):
            if snapshot_code >= 400:
                return httpx.Response(snapshot_code, json={"errors": "no snapshot support"})
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"name": "pre-upgrade"}]})
            return httpx.Response(200, json={"data": "UPID:snapshot"})
        return httpx.Response(200, json={"data": "UPID:action"})

    client = httpx.AsyncClient(
        base_url=_CONFIG.url + "/api2/json", transport=httpx.MockTransport(handler)
    )
    return ProxmoxAdapter(_CONFIG, client=client)


def make_manifest(
    *,
    action_kind: str = "restart",
    strategy: str | None = None,
    claimed: bool = False,
) -> ActionManifest:
    return ActionManifest(
        domain=TrustDomain.CONTAINERS,
        action_kind=action_kind,
        blast_radius="single-host",
        hostnames=("pve1",),
        node="pve1",
        vmid=101,
        vm_kind="lxc",
        rollback_verified=claimed,
        rollback_strategy=strategy,
        action_raw={},
    )


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------


def test_power_actions_default_to_prior_power_state() -> None:
    assert select_strategy(make_manifest()) == PRIOR_POWER_STATE


def test_manifest_may_request_a_strategy() -> None:
    assert select_strategy(make_manifest(strategy="snapshot")) == SNAPSHOT


def test_unknown_requested_strategy_is_kept_for_the_refusal() -> None:
    assert select_strategy(make_manifest(strategy="wishful-thinking")) == "wishful-thinking"


# ---------------------------------------------------------------------------
# Verification — read-only, and not the manifest's to assert
# ---------------------------------------------------------------------------


async def test_verification_is_read_only() -> None:
    """Probing must not write: no snapshot, no power call, before the gate."""
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    await verify_rollback(adapter, make_manifest())
    assert [r.method for r in requests] == ["GET"]
    await adapter.aclose()


async def test_running_guest_verifies_via_prior_power_state() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests, status="running")
    v = await verify_rollback(adapter, make_manifest())
    assert v.verified
    assert v.strategy == PRIOR_POWER_STATE
    assert "running" in v.evidence
    assert v.probe["status"] == "running"
    await adapter.aclose()


async def test_unreadable_guest_does_not_verify() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests, status_code=500)
    v = await verify_rollback(adapter, make_manifest())
    assert not v.verified
    assert "could not read" in v.evidence
    await adapter.aclose()


async def test_unknown_status_does_not_verify() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests, status="migrating")
    v = await verify_rollback(adapter, make_manifest())
    assert not v.verified
    assert "migrating" in v.evidence
    await adapter.aclose()


async def test_a_manifest_cannot_certify_its_own_reversibility() -> None:
    """The whole point of PR D: a false claim is recorded, never believed."""
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests, status_code=500)
    v = await verify_rollback(adapter, make_manifest(claimed=True))
    assert not v.verified
    assert v.claimed
    assert v.claim_was_false
    assert v.evidence.startswith("manifest claimed a verified rollback, but")
    await adapter.aclose()


async def test_unknown_strategy_refuses_by_name() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    v = await verify_rollback(adapter, make_manifest(strategy="wishful-thinking"))
    assert not v.verified
    assert "wishful-thinking" in v.evidence
    assert requests == [], "an unknown strategy needs no probe"
    await adapter.aclose()


async def test_snapshot_support_probe_verifies() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    v = await verify_rollback(adapter, make_manifest(strategy="snapshot"))
    assert v.verified
    assert v.probe["existing_snapshots"] == 1
    assert [r.method for r in requests] == ["GET"]
    await adapter.aclose()


async def test_guest_without_snapshot_support_does_not_verify() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests, snapshot_code=501)
    v = await verify_rollback(adapter, make_manifest(strategy="snapshot"))
    assert not v.verified
    assert "does not support snapshots" in v.evidence
    await adapter.aclose()


# ---------------------------------------------------------------------------
# Capture — after authorization, and only then may it write
# ---------------------------------------------------------------------------


async def test_prior_power_state_capture_writes_nothing() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    v = await verify_rollback(adapter, make_manifest())
    requests.clear()

    plan = await capture_rollback(adapter, make_manifest(), v)
    assert requests == []
    assert plan.state["prior"]["status"] == "running"
    assert plan.verified
    await adapter.aclose()


async def test_snapshot_capture_creates_a_named_snapshot() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    manifest = make_manifest(strategy="snapshot")
    v = await verify_rollback(adapter, manifest)
    requests.clear()

    plan = await capture_rollback(adapter, manifest, v)
    assert [r.method for r in requests] == ["POST"]
    assert requests[0].url.path == "/api2/json/nodes/pve1/lxc/101/snapshot"
    assert plan.state["snapshot"].startswith("helper-")
    assert requests[0].url.params["snapname"] == plan.state["snapshot"]
    await adapter.aclose()


async def test_unverified_snapshot_strategy_captures_nothing() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests, snapshot_code=501)
    manifest = make_manifest(strategy="snapshot")
    v = await verify_rollback(adapter, manifest)
    requests.clear()

    plan = await capture_rollback(adapter, manifest, v)
    assert requests == []
    assert not plan.verified
    assert "snapshot" not in plan.state
    await adapter.aclose()


async def test_capture_failure_is_recorded_not_raised() -> None:
    """A snapshot that fails to take must not abort the authorized action."""
    requests: list[httpx.Request] = []
    manifest = make_manifest(strategy="snapshot")
    adapter = make_adapter(requests)
    v = await verify_rollback(adapter, manifest)
    await adapter.aclose()

    failing = make_adapter(requests, snapshot_code=500)
    plan = await capture_rollback(failing, manifest, v)
    assert plan.capture_error is not None
    assert "snapshot creation failed" in plan.capture_error
    await failing.aclose()


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


async def test_restore_starts_a_guest_that_should_be_running() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests, status="stopped")
    plan = RollbackPlan(
        strategy=PRIOR_POWER_STATE,
        verified=True,
        evidence="",
        state={"prior": {"status": "running"}},
        captured_at="2026-09-03T00:00:00+00:00",
        node="pve1",
        vmid=101,
        vm_kind="lxc",
    )
    detail = await restore(adapter, plan)
    assert "start" in detail
    assert requests[-1].url.path == "/api2/json/nodes/pve1/lxc/101/status/start"
    await adapter.aclose()


async def test_restore_is_a_noop_when_already_in_the_captured_state() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests, status="running")
    plan = RollbackPlan(
        strategy=PRIOR_POWER_STATE,
        verified=True,
        evidence="",
        state={"prior": {"status": "running"}},
        captured_at="2026-09-03T00:00:00+00:00",
        node="pve1",
        vmid=101,
        vm_kind="lxc",
    )
    detail = await restore(adapter, plan)
    assert "already running" in detail
    assert [r.method for r in requests] == ["GET"], "no power call when nothing to undo"
    await adapter.aclose()


async def test_restore_rolls_back_to_the_snapshot() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    plan = RollbackPlan(
        strategy=SNAPSHOT,
        verified=True,
        evidence="",
        state={"snapshot": "helper-20260903-000000"},
        captured_at="2026-09-03T00:00:00+00:00",
        node="pve1",
        vmid=101,
        vm_kind="lxc",
    )
    detail = await restore(adapter, plan)
    assert "helper-20260903-000000" in detail
    assert requests[-1].url.path.endswith("/snapshot/helper-20260903-000000/rollback")
    await adapter.aclose()


async def test_restore_without_a_usable_plan_raises() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    plan = RollbackPlan(
        strategy=SNAPSHOT,
        verified=False,
        evidence="",
        state={},
        captured_at="",
        node="pve1",
        vmid=101,
        vm_kind="lxc",
    )
    with pytest.raises(RollbackError, match="no snapshot was captured"):
        await restore(adapter, plan)
    await adapter.aclose()


# ---------------------------------------------------------------------------
# Receipt round-trip
# ---------------------------------------------------------------------------


async def test_plan_round_trips_through_a_receipt() -> None:
    requests: list[httpx.Request] = []
    adapter = make_adapter(requests)
    manifest = make_manifest()
    v = await verify_rollback(adapter, manifest)
    plan = await capture_rollback(adapter, manifest, v)

    restored = RollbackPlan.from_receipt_state(plan.as_receipt_state())
    assert restored.strategy == plan.strategy
    assert restored.node == "pve1"
    assert restored.vmid == 101
    assert restored.state["prior"]["status"] == "running"
    await adapter.aclose()


def test_pre_orchestrator_receipt_state_refuses_to_restore() -> None:
    """A PR-B receipt has no node/vmid, so it cannot be undone blind."""
    with pytest.raises(RollbackError, match="predates the orchestrator"):
        RollbackPlan.from_receipt_state({"strategy": "prior-power-state", "verified": True})
