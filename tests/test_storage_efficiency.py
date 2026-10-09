"""Storage efficiency (Phase 8.5) — the five checks, as pure functions.

Each check takes already-fetched source data, so these run without a
hypervisor. The load-bearing assertions are that a projection needs a real
trend (one reading is not a slope), that the harness's own leftover snapshots
are called out as its own litter, and that a referenced ISO is never clutter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homelab_helper import mcp_server as mcp_srv
from homelab_helper.adapters.proxmox import ProxmoxAdapter, ProxmoxConfig
from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.storage import (
    HEADROOM_MEDIUM_DAYS,
    MIN_HEADROOM_SAMPLES,
    STALE_SNAPSHOT_DAYS,
    detached_disk_issues,
    headroom_issues,
    project_headroom,
    released_pv_issues,
    snapshot_issues,
    template_clutter_issues,
)

GIB = 1024**3
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def series(start_used: int, per_day: int, days: int) -> list[tuple[datetime, int]]:
    base = NOW - timedelta(days=days)
    return [(base + timedelta(days=i), start_used + per_day * i) for i in range(days)]


# ---------------------------------------------------------------- headroom


def test_a_growing_pool_projects_a_date() -> None:
    projection = project_headroom(series(100 * GIB, 10 * GIB, 14), total=500 * GIB)
    assert projection is not None
    assert projection.days_to_full is not None
    # 14 days at 10 GiB/day → 230 GiB used, 270 GiB left, ~27 days.
    assert 25 <= projection.days_to_full <= 29
    assert abs(projection.bytes_per_day - 10 * GIB) < GIB


def test_too_few_samples_is_not_a_trend() -> None:
    short = series(100 * GIB, 10 * GIB, MIN_HEADROOM_SAMPLES - 1)
    assert project_headroom(short, total=500 * GIB) is None


def test_a_flat_pool_gets_no_projection() -> None:
    projection = project_headroom(series(100 * GIB, 0, 30), total=500 * GIB)
    assert projection is not None
    assert projection.days_to_full is None, "flat pools never fill"


def test_a_shrinking_pool_gets_no_projection() -> None:
    projection = project_headroom(series(300 * GIB, -5 * GIB, 30), total=500 * GIB)
    assert projection is not None
    assert projection.days_to_full is None


def test_churn_below_the_noise_floor_is_not_growth() -> None:
    projection = project_headroom(series(100 * GIB, 1024 * 1024, 30), total=500 * GIB)
    assert projection is not None
    assert projection.days_to_full is None, "~1 MiB/day is not a pool filling up"


def test_a_pool_with_no_total_is_skipped() -> None:
    assert project_headroom(series(100 * GIB, 10 * GIB, 14), total=0) is None


def test_headroom_severity_tracks_urgency() -> None:
    soon = project_headroom(series(400 * GIB, 10 * GIB, 10), total=520 * GIB)
    # 118 GiB used of 220, 102 left at 2 GiB/day → ~51 days: inside the horizon.
    later = project_headroom(series(100 * GIB, 2 * GIB, 10), total=220 * GIB)
    issues = {i.target_id: i for i in headroom_issues([("urgent", soon), ("later", later)])}
    assert issues["urgent"].severity is FindingSeverity.HIGH
    assert issues["later"].severity is FindingSeverity.MEDIUM
    assert all(i.kind is FindingKind.STORAGE_EFFICIENCY for i in issues.values())


def test_a_pool_filling_beyond_the_horizon_is_quiet() -> None:
    far = project_headroom(series(10 * GIB, 1 * GIB, 10), total=10_000 * GIB)
    assert far is not None
    assert far.days_to_full is not None
    assert far.days_to_full > HEADROOM_MEDIUM_DAYS
    assert headroom_issues([("roomy", far)]) == []


def test_headroom_description_names_the_rate_and_the_date() -> None:
    projection = project_headroom(series(400 * GIB, 10 * GIB, 10), total=520 * GIB)
    issue = headroom_issues([("tank", projection)])[0]
    assert "GiB/day" in issue.description
    assert "full around" in issue.description
    assert issue.evidence["bytes_per_day"] > 0


# --------------------------------------------------------------- snapshots


def snap(name: str, days_old: int) -> dict[str, object]:
    return {"name": name, "snaptime": int((NOW - timedelta(days=days_old)).timestamp())}


def guest(**kw: object) -> dict[str, object]:
    return {"vmid": 101, "node": "bmax0", "kind": "lxc", "name": "web01", **kw}


def test_old_snapshots_are_flagged() -> None:
    issues = snapshot_issues(
        [guest(snapshots=[snap("before-upgrade", STALE_SNAPSHOT_DAYS + 5)])], now=NOW
    )
    assert len(issues) == 1
    assert issues[0].target_id == "bmax0/101"
    assert "before-upgrade" in issues[0].description


def test_recent_snapshots_are_left_alone() -> None:
    assert snapshot_issues([guest(snapshots=[snap("yesterday", 1)])], now=NOW) == []


def test_the_live_state_row_is_not_a_snapshot() -> None:
    assert snapshot_issues([guest(snapshots=[{"name": "current"}])], now=NOW) == []


def test_the_harness_own_snapshots_are_called_out() -> None:
    """A rollback capture nobody deleted is the harness littering, so it is
    named as such and raised a level."""
    issues = snapshot_issues(
        [
            guest(
                snapshots=[
                    snap("helper-20260801-120000", STALE_SNAPSHOT_DAYS + 10),
                    snap("manual", STALE_SNAPSHOT_DAYS + 1),
                ]
            )
        ],
        now=NOW,
    )
    assert len(issues) == 1
    assert issues[0].severity is FindingSeverity.MEDIUM, "ours, so we own cleaning it"
    assert "harness's own rollback captures" in issues[0].description
    assert issues[0].evidence["harness_snapshots"] == ["helper-20260801-120000"]


def test_a_foreign_stale_snapshot_stays_low() -> None:
    issues = snapshot_issues([guest(snapshots=[snap("manual", STALE_SNAPSHOT_DAYS + 1)])], now=NOW)
    assert issues[0].severity is FindingSeverity.LOW


# ----------------------------------------------------------- detached disks


def test_unused_disks_are_flagged() -> None:
    issues = detached_disk_issues(
        [guest(config={"unused0": "local-zfs:vm-101-disk-1", "scsi0": "local-zfs:vm-101-disk-0"})]
    )
    assert len(issues) == 1
    assert "unused0" in issues[0].description
    assert "scsi0" not in issues[0].description


def test_a_guest_with_only_attached_disks_is_quiet() -> None:
    assert detached_disk_issues([guest(config={"scsi0": "local-zfs:vm-101-disk-0"})]) == []


# ------------------------------------------------------------- iso clutter


def test_an_unreferenced_iso_is_clutter() -> None:
    volumes = [{"volid": "local:iso/debian-12.iso", "size": 700 * 1024 * 1024}]
    issues = template_clutter_issues(volumes, [guest(config={"scsi0": "local-zfs:vm-101-disk-0"})])
    assert len(issues) == 1
    assert issues[0].target_id == "local"
    assert "debian-12.iso" in issues[0].description


def test_a_mounted_iso_is_not_clutter() -> None:
    volumes = [{"volid": "local:iso/debian-12.iso", "size": 1}]
    guests = [guest(config={"ide2": "local:iso/debian-12.iso,media=cdrom"})]
    assert template_clutter_issues(volumes, guests) == []


def test_a_referenced_container_template_is_not_clutter() -> None:
    volumes = [{"volid": "local:vztmpl/debian-12-standard.tar.zst", "size": 1}]
    guests = [guest(config={"ostemplate": "local:vztmpl/debian-12-standard.tar.zst"})]
    assert template_clutter_issues(volumes, guests) == []


def test_an_iso_referenced_by_an_unused_slot_still_counts_as_referenced() -> None:
    volumes = [{"volid": "local:iso/old.iso", "size": 1}]
    guests = [guest(config={"unused3": "local:iso/old.iso"})]
    assert template_clutter_issues(volumes, guests) == []


def test_clutter_is_grouped_per_storage() -> None:
    volumes = [
        {"volid": "local:iso/a.iso", "size": GIB},
        {"volid": "local:iso/b.iso", "size": GIB},
        {"volid": "nas:iso/c.iso", "size": GIB},
    ]
    issues = template_clutter_issues(volumes, [])
    assert {i.target_id for i in issues} == {"local", "nas"}
    local = next(i for i in issues if i.target_id == "local")
    assert local.evidence["bytes"] == 2 * GIB


# ------------------------------------------------------------- released PVs


def pv(name: str, phase: str, **kw: object) -> dict[str, object]:
    return {
        "metadata": {"name": name},
        "status": {"phase": phase},
        "spec": {
            "capacity": {"storage": "20Gi"},
            "claimRef": {"namespace": "media", "name": "old-claim"},
            "persistentVolumeReclaimPolicy": "Retain",
            **kw,
        },
    }


def test_released_volumes_are_flagged() -> None:
    issues = released_pv_issues([pv("pvc-1", "Released"), pv("pvc-2", "Bound")])
    assert len(issues) == 1
    assert issues[0].target_id == "pvc-1"
    assert "media/old-claim" in issues[0].description
    assert issues[0].evidence["capacity"] == "20Gi"


def test_bound_and_available_volumes_are_quiet() -> None:
    assert released_pv_issues([pv("a", "Bound"), pv("b", "Available")]) == []


# ------------------------------------------------------------- fingerprints


def test_each_category_gets_its_own_fingerprint() -> None:
    """A pool can be both filling and cluttered without the rows colliding."""
    projection = project_headroom(series(400 * GIB, 10 * GIB, 10), total=520 * GIB)
    headroom = headroom_issues([("local", projection)])[0]
    clutter = template_clutter_issues([{"volid": "local:iso/a.iso", "size": 1}], [])[0]
    assert headroom.target_id == clutter.target_id == "local"
    assert headroom.fingerprint != clutter.fingerprint


# ---------------------------------------------------------------------------
# The discovery pass end to end (MockTransport; no hypervisor)
# ---------------------------------------------------------------------------


def _proxmox(handler) -> ProxmoxAdapter:
    config = ProxmoxConfig(url="https://pve.test:8006", token_id="t@pam!x", token_secret="s")
    client = httpx.AsyncClient(
        base_url=config.url + "/api2/json", transport=httpx.MockTransport(handler)
    )
    return ProxmoxAdapter(config, client=client)


_VM_ROW = {
    "vmid": 101,
    "name": "web01",
    "node": "bmax0",
    "type": "lxc",
    "status": "running",
}
_RESOURCES = {
    "storage": [{"storage": "local", "node": "bmax0", "status": "available"}],
    "vm": [_VM_ROW],
}
_GUEST_CONFIG = {"scsi0": "local-zfs:vm-101-disk-0", "unused0": "local-zfs:vm-101-disk-1"}


def _handler(request: httpx.Request) -> httpx.Response:
    """A small Proxmox: one node, one pool, one guest with an old snapshot and a
    detached disk, and one unreferenced ISO."""
    path, params = request.url.path, request.url.params
    body: object = []
    if path.endswith("/cluster/resources"):
        body = _RESOURCES.get(str(params.get("type")), [])
    elif path.endswith("/cluster/status"):
        body = [{"type": "cluster", "name": "homelab"}]
    elif path.endswith("/config"):
        body = _GUEST_CONFIG
    elif path.endswith("/snapshot"):
        body = [{"name": "ancient", "snaptime": int((NOW - timedelta(days=90)).timestamp())}]
    elif "/storage/local/content" in path and params.get("content") == "iso":
        body = [{"volid": "local:iso/unused.iso", "size": 900 * 1024 * 1024}]
    return httpx.Response(200, json={"data": body})


class _StubK8s:
    async def get_resource(self, kind: str) -> list[dict[str, object]]:
        assert kind == "pv"
        return [pv("pvc-orphan", "Released")]


@pytest.fixture
async def storage_session():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    await engine.dispose()


async def test_discovery_pass_opens_one_finding_per_category(
    storage_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_srv, "_load_proxmox_adapter", lambda: _proxmox(_handler))
    monkeypatch.setattr(mcp_srv, "_load_k8s_adapter", _StubK8s)
    monkeypatch.setattr(
        mcp_srv,
        "_proxmox_storage_facts",
        mcp_srv._proxmox_storage_facts,  # noqa: SLF001 - exercising the real gather
    )

    async with session_scope(storage_session) as session:
        # One guest for the gather to walk.
        result = await mcp_srv._discover_storage(session)  # noqa: SLF001

    assert result["errors"] == {}, result["errors"]
    assert result["findings"]["opened"] >= 1


async def test_a_dead_source_resolves_nothing(
    storage_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 1: a source that did not answer leaves its findings alone."""

    def boom() -> ProxmoxAdapter:
        raise RuntimeError("pve unreachable")

    monkeypatch.setattr(mcp_srv, "_load_proxmox_adapter", boom)
    monkeypatch.setattr(mcp_srv, "_load_k8s_adapter", _StubK8s)

    async with session_scope(storage_session) as session:
        result = await mcp_srv._discover_storage(session)  # noqa: SLF001

    assert "proxmox" in result["errors"]
    assert result["findings"]["resolved"] == 0
