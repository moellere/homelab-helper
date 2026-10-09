"""Phase 8.1 — version currency: package lag, mixed versions, OS end of life, HA updates."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from homelab_helper.adapters.homeassistant import parse_state
from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.versions import (
    ceph_daemon_versions,
    ceph_issues,
    hass_update_issues,
    k8s_eol_issues,
    k8s_issues,
    load_eol_table,
    os_eol_issues,
    os_release,
    proxmox_issues,
    reconcile_version_findings,
)
from tests.test_proxmox_adapter import _adapter

TODAY = date(2026, 10, 5)


@pytest.fixture
async def sessionmaker():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(eng)
    await eng.dispose()


def _pkg(name: str, origin: str = "Debian", old: str = "1", new: str = "2") -> dict[str, Any]:
    return {"Package": name, "Origin": origin, "OldVersion": old, "Version": new}


def _node(name: str, version: str, pending: list[dict[str, Any]]) -> dict[str, Any]:
    return {"node": name, "version": version, "pending": pending}


# ------------------------------------------------------------------- proxmox


def test_pending_updates_and_mixed_versions_are_named() -> None:
    nodes = [
        _node("pve0", "9.2.20", [_pkg("pve-manager", "Proxmox", "9.2.20", "9.2.21")]),
        _node("pve1", "9.2.11", [_pkg(f"lib{i}") for i in range(60)]),
        _node("pve2", "9.2.11", []),
    ]
    issues = {(i.category, i.target_id): i for i in proxmox_issues("lab", nodes)}
    assert set(issues) == {("pve-updates", "pve0"), ("pve-updates", "pve1"), ("pve-mixed", "lab")}
    assert issues["pve-updates", "pve0"].severity is FindingSeverity.MEDIUM  # pve-manager itself
    assert "9.2.20 → 9.2.21" in issues["pve-updates", "pve0"].description
    assert issues["pve-updates", "pve1"].severity is FindingSeverity.MEDIUM  # 60 >= 50
    assert issues["pve-updates", "pve1"].evidence["origins"] == {"Debian": 60}
    mixed = issues["pve-mixed", "lab"]
    assert "pve0 9.2.20" in mixed.description
    assert "pve1 9.2.11" in mixed.description


def test_a_few_ordinary_updates_are_low_and_an_even_cluster_is_quiet() -> None:
    nodes = [_node("pve0", "9.2.21", [_pkg("curl")]), _node("pve1", "9.2.21", [])]
    (only,) = proxmox_issues("lab", nodes)
    assert only.category == "pve-updates"
    assert only.severity is FindingSeverity.LOW


# -------------------------------------------------------------------- os eol


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        ({"os_id": "debian", "os_pretty_name": "Debian GNU/Linux 13 (trixie)"}, ("debian", "13")),
        (
            {"os_id": "raspbian", "os_pretty_name": "Raspbian GNU/Linux 11 (bullseye)"},
            ("raspbian", "11"),
        ),
        ({"os_id": "ubuntu", "os_pretty_name": "Ubuntu 26.04 LTS"}, ("ubuntu", "26.04")),
        (
            {"os_id": "debian", "os_version_id": "12", "os_pretty_name": "whatever"},
            ("debian", "12"),
        ),
        ({"os_pretty_name": "Debian GNU/Linux 13"}, None),
    ],
)
def test_os_release_parsing(caps, expected) -> None:
    assert os_release(caps) == expected


def test_eol_table_ships_and_parses() -> None:
    table = load_eol_table()
    assert table["aliases"]["raspbian"] == "debian"
    assert {"10", "11", "12", "13"} <= set(table["debian"])
    assert "26.04" in table["ubuntu"]


def test_past_eol_is_high_soon_is_medium_far_is_silent() -> None:
    table = load_eol_table()
    hosts = [
        ("h1", "old-pi", {"os_id": "raspbian", "os_pretty_name": "Raspbian GNU/Linux 10 (buster)"}),
        ("h2", "soon", {"os_id": "debian", "os_pretty_name": "Debian GNU/Linux 11 (bullseye)"}),
        ("h3", "fresh", {"os_id": "debian", "os_pretty_name": "Debian GNU/Linux 13 (trixie)"}),
        ("h4", "unknown", {"os_id": "gentoo", "os_pretty_name": "Gentoo 2.17"}),
    ]
    by_host = {i.target_id: i for i in os_eol_issues(hosts, table, date(2026, 6, 1))}
    assert set(by_host) == {"h1", "h2"}
    assert by_host["h1"].severity is FindingSeverity.HIGH
    assert "2024-06-30" in by_host["h1"].description
    assert by_host["h2"].severity is FindingSeverity.MEDIUM
    assert by_host["h2"].evidence["days_remaining"] == (date(2026, 8, 31) - date(2026, 6, 1)).days


# ------------------------------------------------------------ kubernetes/talos


def test_kubelet_and_talos_skew() -> None:
    hosts = [
        (
            "a",
            "cp1",
            {
                "k8s_kubelet_version": "v1.35.2",
                "os_id": "talos",
                "os_pretty_name": "Talos Linux v1.12.6",
            },
        ),
        (
            "b",
            "cp2",
            {
                "k8s_kubelet_version": "v1.35.1",
                "os_id": "talos",
                "os_pretty_name": "Talos Linux v1.12.6",
            },
        ),
    ]
    issues, observed = k8s_issues(hosts)
    assert observed == {"k8s-skew", "talos-skew"}
    assert [i.category for i in issues] == ["k8s-skew"]
    assert "cp2 v1.35.1" in issues[0].description


def test_no_kubernetes_data_observes_nothing() -> None:
    issues, observed = k8s_issues([("a", "nas", {"os_id": "debian"})])
    assert issues == []
    assert observed == set()


# ------------------------------------------------------------- home assistant


def _update(
    entity_id: str, state: str = "on", installed: str = "1", latest: str = "2"
) -> dict[str, Any]:
    return parse_state(
        {
            "entity_id": entity_id,
            "state": state,
            "attributes": {
                "installed_version": installed,
                "latest_version": latest,
                "title": entity_id,
            },
        }
    )


def test_parse_state_keeps_versions_only_for_update_entities() -> None:
    assert _update("update.x")["latest_version"] == "2"
    plain = parse_state(
        {"entity_id": "light.x", "state": "on", "attributes": {"latest_version": "9"}}
    )
    assert "latest_version" not in plain


def test_hass_platform_update_is_medium_devices_low_none_quiet() -> None:
    (core,) = hass_update_issues(
        "ha", [_update("update.home_assistant_core_update"), _update("update.wled_firmware")]
    )
    assert core.severity is FindingSeverity.MEDIUM
    assert "platform:" in core.description
    (devices,) = hass_update_issues("ha", [_update("update.wled_firmware")])
    assert devices.severity is FindingSeverity.LOW
    assert hass_update_issues("ha", [_update("update.wled_firmware", state="off")]) == []


# ------------------------------------------------------------------ reconcile


async def test_open_update_resolve_and_absence_never_resolves(sessionmaker) -> None:
    t0 = datetime(2026, 10, 5, tzinfo=UTC)
    lagging = proxmox_issues(
        "lab", [_node("pve0", "9.2.20", [_pkg("curl")]), _node("pve1", "9.2.21", [])]
    )
    eol = os_eol_issues(
        [("h1", "old", {"os_id": "debian", "os_pretty_name": "Debian GNU/Linux 10 (buster)"})],
        load_eol_table(),
        TODAY,
    )
    async with session_scope(sessionmaker) as s:
        first = await reconcile_version_findings(
            s, lagging + eol, {"pve-updates", "pve-mixed", "os-eol"}, when=t0
        )
        assert len(first.opened) == 3
        rows = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert {r.kind for r in rows} == {FindingKind.VERSION_DRIFT}

        # Proxmox unreachable this run: only os-eol observed — the Proxmox findings must stay open.
        skipped = await reconcile_version_findings(s, eol, {"os-eol"}, when=t0)
        assert skipped.resolved == []

        # Proxmox observed and clean: its findings resolve, the EOL one stays.
        clean = proxmox_issues("lab", [_node("pve0", "9.2.21", []), _node("pve1", "9.2.21", [])])
        healed = await reconcile_version_findings(
            s, clean + eol, {"pve-updates", "pve-mixed", "os-eol"}, when=t0
        )
        assert len(healed.resolved) == 2
        open_now = (
            (
                await s.execute(
                    select(ReconciliationFinding).where(
                        ReconciliationFinding.status == FindingStatus.OPEN
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [r.title for r in open_now] == [eol[0].title]

        # Recurrence reopens the same row.
        again = await reconcile_version_findings(
            s, lagging + eol, {"pve-updates", "pve-mixed", "os-eol"}, when=t0
        )
        assert len(again.reopened) == 2


# -------------------------------------------------------------------- adapter


async def test_proxmox_reads_are_plain_gets() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/version"):
            return httpx.Response(200, json={"data": {"version": "9.2.21", "release": "9.2"}})
        return httpx.Response(200, json={"data": [_pkg("curl")]})

    adapter = _adapter(handler)
    try:
        assert (await adapter.node_version("pve0"))["version"] == "9.2.21"
        assert (await adapter.pending_updates("pve0"))[0]["Package"] == "curl"
    finally:
        await adapter.aclose()
    assert seen == [
        ("GET", "/api2/json/nodes/pve0/version"),
        ("GET", "/api2/json/nodes/pve0/apt/update"),
    ]


# ---------------------------------------------------------------------- ceph


def _ceph_meta(versions: dict[str, str]) -> dict[str, Any]:
    """Build /cluster/ceph/metadata from ``{"mon.a": "19.2.6", "osd.0": "19.2.5"}``."""
    meta: dict[str, Any] = {"mon": {}, "mgr": {}, "mds": {}, "osd": [], "node": {}}
    for daemon, v in versions.items():
        kind, name = daemon.split(".", 1)
        entry = {"ceph_version": f"ceph version {v} (deadbeef) squid (stable)"}
        if kind == "osd":
            meta["osd"].append({"id": int(name), **entry})
        else:
            meta[kind][name] = entry
    return meta


def test_ceph_daemon_versions_cover_every_daemon_kind() -> None:
    meta = _ceph_meta({"mon.a": "19.2.6", "mgr.a": "19.2.6", "mds.a": "19.2.6", "osd.0": "19.2.5"})
    meta["mon"]["a"]["ceph_version_short"] = "19.2.6"  # the short form wins when present
    assert ceph_daemon_versions(meta) == {
        "mon.a": "19.2.6",
        "mgr.a": "19.2.6",
        "mds.a": "19.2.6",
        "osd.0": "19.2.5",
    }
    assert ceph_daemon_versions({}) == {}


def test_ceph_eol_and_mixed_versions() -> None:
    table = load_eol_table()
    assert table["ceph"]["19"]["codename"] == "squid"
    # Squid, 22 days before its 2026-10-31 EOL, mid-upgrade.
    mixed = _ceph_meta({"mon.a": "19.2.6", "mon.b": "19.2.5", "osd.0": "19.2.6"})
    issues, observed = ceph_issues("lab", mixed, table, date(2026, 10, 9))
    assert observed == {"ceph-eol", "ceph-mixed"}
    by = {i.category: i for i in issues}
    assert by["ceph-mixed"].severity is FindingSeverity.MEDIUM
    assert by["ceph-mixed"].evidence["versions"] == {
        "19.2.5": ["mon.b"],
        "19.2.6": ["mon.a", "osd.0"],
    }
    assert by["ceph-eol"].severity is FindingSeverity.MEDIUM
    assert by["ceph-eol"].evidence == {
        "version": "19.2.6",
        "eol": "2026-10-31",
        "days_remaining": 22,
    }
    assert "2026-10-31" in by["ceph-eol"].description
    # Past EOL is HIGH; a current release far from EOL is silent; no Ceph observes nothing.
    past, _ = ceph_issues("lab", _ceph_meta({"mon.a": "18.2.4"}), table, date(2026, 10, 9))
    assert [i.severity for i in past] == [FindingSeverity.HIGH]
    quiet, _ = ceph_issues("lab", _ceph_meta({"mon.a": "20.2.4"}), table, date(2026, 10, 9))
    assert quiet == []
    assert ceph_issues("lab", {}, table, date(2026, 10, 9)) == ([], set())


async def test_proxmox_ceph_metadata_is_a_plain_get() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"data": {"mon": {}, "osd": []}})

    adapter = _adapter(handler)
    try:
        assert (await adapter.ceph_metadata())["osd"] == []
    finally:
        await adapter.aclose()
    assert seen == [("GET", "/api2/json/cluster/ceph/metadata")]


def test_kubernetes_and_talos_minors_past_support_are_findings() -> None:
    hosts = [
        (
            "a",
            "cp1",
            {
                "k8s_kubelet_version": "v1.34.12",
                "os_id": "talos",
                "os_pretty_name": "Talos Linux v1.12.6",
            },
        ),
        ("b", "w1", {"k8s_kubelet_version": "v1.34.12"}),
        ("c", "ubuntu-box", {"os_id": "ubuntu", "os_version_id": "24.04"}),
    ]
    table = load_eol_table()
    # 10/05/2026: Kubernetes 1.34 ends 10/27/2026 (22 days), Talos 1.12 ended 09/03/2026.
    issues, observed = k8s_eol_issues(hosts, table, TODAY)
    assert observed == {"k8s-eol", "talos-eol"}
    by_cat = {i.category: i for i in issues}
    assert by_cat["k8s-eol"].severity is FindingSeverity.MEDIUM
    assert "22 days" in by_cat["k8s-eol"].title
    assert by_cat["k8s-eol"].evidence["nodes"] == ["cp1", "w1"]
    assert by_cat["talos-eol"].severity is FindingSeverity.HIGH
    assert "past end of support" in by_cat["talos-eol"].title
    assert by_cat["k8s-eol"].fingerprint != by_cat["talos-eol"].fingerprint

    # A current minor is quiet; an unknown one is observed but silent, never invented.
    current = [
        (
            "a",
            "cp1",
            {
                "k8s_kubelet_version": "v1.37.1",
                "os_pretty_name": "Talos Linux v1.14.2",
                "os_id": "talos",
            },
        )
    ]
    issues, observed = k8s_eol_issues(current, table, TODAY)
    assert issues == []
    assert observed == {"k8s-eol", "talos-eol"}
    # Two minors in use give two findings with distinct identities.
    mixed = [
        ("a", "cp1", {"k8s_kubelet_version": "v1.33.9"}),
        ("b", "w1", {"k8s_kubelet_version": "v1.34.1"}),
    ]
    issues, _ = k8s_eol_issues(mixed, table, TODAY)
    assert len({i.fingerprint for i in issues}) == 2
