"""NetBoxAdapter unit tests against httpx.MockTransport.

No live NetBox is involved — every request the adapter makes is dispatched
through a hand-built mock transport that simulates the relevant pieces of
the NetBox REST surface. The adapter still runs its real httpx calls,
JSON marshaling, pagination, and error handling — the mock just answers
on the other side.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from homelab_helper.adapters.netbox import (
    _DEVICE_CFS,
    CustomFieldSpec,
    NetBoxAdapter,
    NetBoxAPIError,
    NetBoxConfig,
    NetBoxConfigError,
    _build_host_cf_patch,
)
from homelab_helper.db.enums import (
    Architecture,
    DiscoverySource,
    PowerPolicy,
    PowerState,
)
from homelab_helper.db.models import Host

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Mock transport helper
# ---------------------------------------------------------------------------


def _make_mock_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
) -> NetBoxAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://netbox.example/",
        headers={
            "Authorization": "Token test-token",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    return NetBoxAdapter(
        NetBoxConfig(url="http://netbox.example/", token="test-token"),
        client=client,
    )


def _host(hostname: str = "bmax0", **overrides: Any) -> Host:
    """Build a Host *instance* (not persisted) with sensible defaults."""
    defaults: dict[str, Any] = {
        "hostname": hostname,
        "primary_ip": "10.250.6.20",
        "arch": Architecture.AMD64,
        "power_policy": PowerPolicy.ALWAYS_ON,
        "expected_power_state": PowerState.ON,
        "discovery_source": DiscoverySource.KERNEL_PROBE,
        "capabilities": {"kernel": "6.8.0", "cpu_cores": 8},
        "discovery_last_run": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        "last_verified": datetime(2026, 5, 30, 9, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Host(**defaults)


# ---------------------------------------------------------------------------
# Config + auth
# ---------------------------------------------------------------------------


def test_config_from_env_requires_both_url_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_NETBOX_URL", raising=False)
    monkeypatch.delenv("HOMELAB_HELPER_NETBOX_TOKEN", raising=False)
    with pytest.raises(NetBoxConfigError):
        NetBoxConfig.from_env()


def test_config_from_env_reads_both_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_NETBOX_URL", "https://netbox.example")
    monkeypatch.setenv("HOMELAB_HELPER_NETBOX_TOKEN", "abc123")
    cfg = NetBoxConfig.from_env()
    assert cfg.url == "https://netbox.example"
    assert cfg.token == "abc123"
    assert cfg.verify_ssl is True


def test_config_verify_ssl_can_be_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_NETBOX_URL", "https://netbox.example")
    monkeypatch.setenv("HOMELAB_HELPER_NETBOX_TOKEN", "abc")
    monkeypatch.setenv("HOMELAB_HELPER_NETBOX_VERIFY_SSL", "false")
    cfg = NetBoxConfig.from_env()
    assert cfg.verify_ssl is False


async def test_authorization_header_is_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"netbox-version": "4.0.0"})

    adapter = _make_mock_adapter(handler)
    try:
        await adapter.health_check()
    finally:
        await adapter.aclose()
    assert seen["auth"] == "Token test-token"


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


async def test_health_check_returns_status_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/status/"
        return httpx.Response(
            200,
            json={
                "netbox-version": "4.1.2",
                "plugins": {"netbox-topology-views": "4.1.0"},
            },
        )

    adapter = _make_mock_adapter(handler)
    try:
        status = await adapter.health_check()
    finally:
        await adapter.aclose()
    assert status["netbox-version"] == "4.1.2"


async def test_health_check_propagates_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid token."})

    adapter = _make_mock_adapter(handler)
    try:
        with pytest.raises(NetBoxAPIError) as excinfo:
            await adapter.health_check()
    finally:
        await adapter.aclose()
    assert excinfo.value.status_code == 401
    assert "Invalid token" in excinfo.value.detail


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


async def test_get_device_by_name_returns_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/dcim/devices/"
        assert request.url.params.get("name") == "bmax0"
        return httpx.Response(
            200,
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"id": 42, "name": "bmax0"}],
            },
        )

    adapter = _make_mock_adapter(handler)
    try:
        device = await adapter.get_device_by_name("bmax0")
    finally:
        await adapter.aclose()
    assert device == {"id": 42, "name": "bmax0"}


async def test_get_device_by_name_returns_none_when_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0, "next": None, "previous": None, "results": []})

    adapter = _make_mock_adapter(handler)
    try:
        device = await adapter.get_device_by_name("ghost")
    finally:
        await adapter.aclose()
    assert device is None


async def test_get_device_by_name_filters_case_insensitive_matches() -> None:
    """NetBox's ``name`` filter is case-insensitive; adapter prefers exact match."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 2,
                "next": None,
                "previous": None,
                "results": [
                    {"id": 1, "name": "BMAX0"},
                    {"id": 2, "name": "bmax0"},
                ],
            },
        )

    adapter = _make_mock_adapter(handler)
    try:
        device = await adapter.get_device_by_name("bmax0")
    finally:
        await adapter.aclose()
    assert device is not None
    assert device["id"] == 2


async def test_update_device_sends_patch() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json={"id": 7, "name": "bmax0"})

    adapter = _make_mock_adapter(handler)
    try:
        await adapter.update_device(7, {"custom_fields": {"cf_arch": "amd64"}})
    finally:
        await adapter.aclose()
    assert seen["method"] == "PATCH"
    assert seen["path"] == "/api/dcim/devices/7/"
    assert b"cf_arch" in seen["body"]


async def test_list_devices_paginates() -> None:
    """Pagination follows ``next`` until null."""
    pages = [
        {
            "count": 3,
            "next": "http://netbox.example/api/dcim/devices/?cursor=2",
            "previous": None,
            "results": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
        },
        {
            "count": 3,
            "next": None,
            "previous": None,
            "results": [{"id": 3, "name": "c"}],
        },
    ]
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        idx = call_count[0]
        call_count[0] += 1
        return httpx.Response(200, json=pages[idx])

    adapter = _make_mock_adapter(handler)
    try:
        devices = await adapter.list_devices()
    finally:
        await adapter.aclose()
    assert [d["name"] for d in devices] == ["a", "b", "c"]
    assert call_count[0] == 2


# ---------------------------------------------------------------------------
# Custom-field bootstrap
# ---------------------------------------------------------------------------


_SAMPLE_SPECS = (
    CustomFieldSpec(
        name="cf_arch",
        label="Arch",
        description="d",
        type="select",
        choices=("amd64", "arm64"),
    ),
    CustomFieldSpec(
        name="cf_capabilities",
        label="Caps",
        description="d",
        type="json",
    ),
)


def _bootstrap_handler(existing: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    created: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/extras/custom-fields/":
            results = [{"name": n} for n in (existing + created)]
            return httpx.Response(
                200,
                json={"count": len(results), "next": None, "previous": None, "results": results},
            )
        if request.method == "POST" and request.url.path == "/api/extras/custom-fields/":
            import json as _json

            payload = _json.loads(request.content)
            created.append(payload["name"])
            return httpx.Response(201, json={"id": 100 + len(created), "name": payload["name"]})
        return httpx.Response(404, json={"detail": "not found"})

    return handler


async def test_bootstrap_creates_only_missing_fields() -> None:
    handler = _bootstrap_handler(existing=["cf_arch"])
    adapter = _make_mock_adapter(handler)
    try:
        result = await adapter.bootstrap_custom_fields(specs=_SAMPLE_SPECS)
    finally:
        await adapter.aclose()

    assert result.created == ["cf_capabilities"]
    assert result.already_present == ["cf_arch"]
    assert result.failed == []


async def test_bootstrap_is_idempotent_on_second_call() -> None:
    handler = _bootstrap_handler(existing=[])
    adapter = _make_mock_adapter(handler)
    try:
        first = await adapter.bootstrap_custom_fields(specs=_SAMPLE_SPECS)
        second = await adapter.bootstrap_custom_fields(specs=_SAMPLE_SPECS)
    finally:
        await adapter.aclose()
    assert first.created == ["cf_arch", "cf_capabilities"]
    assert second.created == []
    assert second.already_present == ["cf_arch", "cf_capabilities"]


async def test_bootstrap_dry_run_makes_no_post_calls() -> None:
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"count": 0, "next": None, "previous": None, "results": []},
            )
        return httpx.Response(201, json={"id": 1, "name": "x"})

    adapter = _make_mock_adapter(handler)
    try:
        result = await adapter.bootstrap_custom_fields(specs=_SAMPLE_SPECS, dry_run=True)
    finally:
        await adapter.aclose()

    assert result.created == ["cf_arch", "cf_capabilities"]
    assert "POST" not in seen_methods


async def test_bootstrap_records_per_field_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"count": 0, "next": None, "previous": None, "results": []},
            )
        return httpx.Response(400, json={"detail": "bad spec"})

    adapter = _make_mock_adapter(handler)
    try:
        result = await adapter.bootstrap_custom_fields(specs=_SAMPLE_SPECS)
    finally:
        await adapter.aclose()

    assert result.created == []
    assert {name for name, _ in result.failed} == {"cf_arch", "cf_capabilities"}


def test_shipped_device_cfs_cover_walkthrough_set() -> None:
    """Sanity check: the cf names map to the walkthrough's day-one list."""
    names = {spec.name for spec in _DEVICE_CFS}
    must_have = {
        "cf_power_policy",
        "cf_expected_power_state",
        "cf_discovery_source",
        "cf_discovery_last_run",
        "cf_last_verified",
        "cf_capabilities",
        "cf_arch",
        "cf_hypervisor_type",
    }
    assert must_have.issubset(names)


# ---------------------------------------------------------------------------
# sync_host
# ---------------------------------------------------------------------------


async def test_sync_host_skips_when_device_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0, "next": None, "previous": None, "results": []})

    adapter = _make_mock_adapter(handler)
    try:
        result = await adapter.sync_host(_host("ghost"))
    finally:
        await adapter.aclose()

    assert result.found is False
    assert result.device_id is None
    assert "no NetBox Device" in (result.skipped_reason or "")


async def test_sync_host_sends_patch_for_existing_device() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/dcim/devices/":
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{"id": 42, "name": "bmax0"}],
                },
            )
        if request.method == "PATCH":
            import json as _json

            seen["path"] = request.url.path
            seen["body"] = _json.loads(request.content)
            return httpx.Response(200, json={"id": 42, "name": "bmax0"})
        return httpx.Response(404)

    adapter = _make_mock_adapter(handler)
    try:
        result = await adapter.sync_host(_host("bmax0"))
    finally:
        await adapter.aclose()

    assert result.found is True
    assert result.device_id == 42
    assert seen["path"] == "/api/dcim/devices/42/"
    cf = seen["body"]["custom_fields"]
    assert cf["cf_arch"] == "amd64"
    assert cf["cf_power_policy"] == "always-on"
    assert cf["cf_capabilities"] == {"kernel": "6.8.0", "cpu_cores": 8}
    assert "T" in cf["cf_discovery_last_run"]  # ISO datetime
    assert cf["cf_last_verified"] == "2026-05-30"


async def test_sync_host_dry_run_does_not_patch() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{"id": 1, "name": "bmax0"}],
                },
            )
        return httpx.Response(200, json={"id": 1})

    adapter = _make_mock_adapter(handler)
    try:
        result = await adapter.sync_host(_host("bmax0"), dry_run=True)
    finally:
        await adapter.aclose()

    assert result.found is True
    assert result.patch["custom_fields"]["cf_arch"] == "amd64"
    assert "PATCH" not in methods


def test_build_host_cf_patch_omits_none_timestamps() -> None:
    """If discovery_last_run / last_verified are None, those keys aren't sent."""
    host = _host(discovery_last_run=None, last_verified=None)
    patch = _build_host_cf_patch(host)
    assert "cf_discovery_last_run" not in patch
    assert "cf_last_verified" not in patch
    # The harness-owned scalars are still present.
    assert patch["cf_arch"] == "amd64"
    assert patch["cf_capabilities"] == {"kernel": "6.8.0", "cpu_cores": 8}


def test_build_host_cf_patch_emits_empty_dict_when_capabilities_missing() -> None:
    host = _host(capabilities={})
    patch = _build_host_cf_patch(host)
    assert patch["cf_capabilities"] == {}


# ---------------------------------------------------------------------------
# Misc API behavior
# ---------------------------------------------------------------------------


async def test_api_error_includes_method_and_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "internal"})

    adapter = _make_mock_adapter(handler)
    try:
        with pytest.raises(NetBoxAPIError) as excinfo:
            await adapter.health_check()
    finally:
        await adapter.aclose()
    err = excinfo.value
    assert err.status_code == 500
    assert err.method == "GET"
    assert err.path == "/api/status/"


async def test_api_error_handles_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html><body>Bad Gateway</body></html>")

    adapter = _make_mock_adapter(handler)
    try:
        with pytest.raises(NetBoxAPIError) as excinfo:
            await adapter.health_check()
    finally:
        await adapter.aclose()
    assert "Bad Gateway" in excinfo.value.detail


# ---------------------------------------------------------------------------
# InventoryItem sync
# ---------------------------------------------------------------------------


from homelab_helper.adapters.netbox import (  # noqa: E402
    PartPlacement,
    _build_inventory_payload,
    _format_bytes,
    _inventory_matches,
)


def _dimm(slot: str = "DIMM_A1", serial: str = "DIMM-AAA") -> PartPlacement:
    return PartPlacement(
        slot=slot,
        part_kind="dimm",
        serial=serial,
        manufacturer="Crucial",
        model="CT16G4SFRA32A",
        capacity_bytes=16 * 1024**3,
    )


def _ssd(slot: str = "/dev/nvme0n1", wwid: str = "nvme.wwn-aaa") -> PartPlacement:
    return PartPlacement(
        slot=slot,
        part_kind="nvme",
        serial=None,
        wwid=wwid,
        manufacturer="Samsung",
        model="SSD 980 1TB",
        capacity_bytes=1000 * 1024**3,
    )


class _InventoryServer:
    """Minimal NetBox InventoryItem state machine for MockTransport handlers."""

    def __init__(
        self,
        *,
        device_id: int = 99,
        initial: list[dict[str, Any]] | None = None,
    ) -> None:
        self.device_id = device_id
        self.next_id = 1000
        self.items: dict[int, dict[str, Any]] = {}
        for it in initial or []:
            self.items[it["id"]] = it

    def list(self) -> list[dict[str, Any]]:
        return list(self.items.values())

    def handler(self) -> Callable[[httpx.Request], httpx.Response]:
        import json as _json

        def h(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/api/dcim/inventory-items/":
                return httpx.Response(
                    200,
                    json={
                        "count": len(self.items),
                        "next": None,
                        "previous": None,
                        "results": self.list(),
                    },
                )
            if request.method == "POST" and request.url.path == "/api/dcim/inventory-items/":
                payload = _json.loads(request.content)
                new_id = self.next_id
                self.next_id += 1
                row = {**payload, "id": new_id}
                self.items[new_id] = row
                return httpx.Response(201, json=row)
            if request.method == "PATCH" and request.url.path.startswith(
                "/api/dcim/inventory-items/"
            ):
                item_id = int(request.url.path.rstrip("/").rsplit("/", 1)[-1])
                payload = _json.loads(request.content)
                self.items[item_id].update(payload)
                return httpx.Response(200, json=self.items[item_id])
            if request.method == "DELETE" and request.url.path.startswith(
                "/api/dcim/inventory-items/"
            ):
                item_id = int(request.url.path.rstrip("/").rsplit("/", 1)[-1])
                self.items.pop(item_id, None)
                return httpx.Response(204)
            return httpx.Response(404, json={"detail": "not found"})

        return h


# --- payload + helpers ----


def test_build_inventory_payload_dimm() -> None:
    payload = _build_inventory_payload(_dimm(), device_id=42)
    assert payload["device"] == 42
    assert payload["name"] == "DIMM_A1"
    assert payload["serial"] == "DIMM-AAA"
    assert payload["part_id"] == "Crucial CT16G4SFRA32A"
    assert "DIMM" in payload["description"]
    assert payload["discovered"] is True


def test_build_inventory_payload_storage_prefers_wwid() -> None:
    payload = _build_inventory_payload(_ssd(), device_id=42)
    assert payload["serial"] == "nvme.wwn-aaa"
    assert "NVME" in payload["description"]


def test_build_inventory_payload_truncates_long_fields() -> None:
    p = PartPlacement(
        slot="x",
        part_kind="dimm",
        serial="S" * 80,
        manufacturer="M" * 30,
        model="N" * 30,
    )
    payload = _build_inventory_payload(p, device_id=1)
    assert len(payload["serial"]) <= 50
    assert len(payload["part_id"]) <= 50


def test_inventory_matches_treats_missing_keys_as_empty() -> None:
    existing = {
        "name": "DIMM_A1",
        "serial": "AAA",
        "part_id": "X",
        "description": "DIMM 16 GB",
        "discovered": True,
    }
    desired = {
        "name": "DIMM_A1",
        "serial": "AAA",
        "part_id": "X",
        "description": "DIMM 16 GB",
        "discovered": True,
    }
    assert _inventory_matches(existing, desired)
    desired["serial"] = "BBB"
    assert not _inventory_matches(existing, desired)


def test_format_bytes_humanizes() -> None:
    assert _format_bytes(16 * 1024**3) == "16 GB"
    assert _format_bytes(1024**4) == "1 TB"
    assert _format_bytes(500) == "500 B"


# --- sync_inventory_items orchestration ----


async def test_sync_inventory_creates_missing_items() -> None:
    server = _InventoryServer()
    adapter = _make_mock_adapter(server.handler())
    try:
        result = await adapter.sync_inventory_items(99, [_dimm(), _ssd()])
    finally:
        await adapter.aclose()

    assert sorted(result.created) == ["/dev/nvme0n1", "DIMM_A1"]
    assert result.deleted == []
    assert result.updated == []
    assert len(server.items) == 2


async def test_sync_inventory_is_idempotent_when_nothing_changes() -> None:
    # Pre-seed the server with what the harness would create.
    seeded = [
        {"id": 1, **_build_inventory_payload(_dimm(), device_id=99)},
        {"id": 2, **_build_inventory_payload(_ssd(), device_id=99)},
    ]
    server = _InventoryServer(initial=seeded)
    adapter = _make_mock_adapter(server.handler())
    try:
        result = await adapter.sync_inventory_items(99, [_dimm(), _ssd()])
    finally:
        await adapter.aclose()

    assert result.created == []
    assert result.updated == []
    assert result.deleted == []
    assert sorted(result.unchanged) == ["/dev/nvme0n1", "DIMM_A1"]


async def test_sync_inventory_updates_when_fields_diverge() -> None:
    # Existing item has a stale serial.
    stale = {
        "id": 7,
        **_build_inventory_payload(_dimm(serial="OLD-SERIAL"), device_id=99),
    }
    server = _InventoryServer(initial=[stale])
    adapter = _make_mock_adapter(server.handler())
    try:
        result = await adapter.sync_inventory_items(99, [_dimm(serial="NEW-SERIAL")])
    finally:
        await adapter.aclose()

    assert result.updated == ["DIMM_A1"]
    assert result.created == []
    assert server.items[7]["serial"] == "NEW-SERIAL"


async def test_sync_inventory_deletes_items_no_longer_in_harness() -> None:
    seeded = [
        {"id": 1, **_build_inventory_payload(_dimm(), device_id=99)},
        {"id": 2, **_build_inventory_payload(_ssd(), device_id=99)},
    ]
    server = _InventoryServer(initial=seeded)
    adapter = _make_mock_adapter(server.handler())
    try:
        # Harness now says only the DIMM is placed; the SSD was removed.
        result = await adapter.sync_inventory_items(99, [_dimm()])
    finally:
        await adapter.aclose()

    assert result.deleted == ["/dev/nvme0n1"]
    assert 2 not in server.items
    assert 1 in server.items  # DIMM survived


async def test_sync_inventory_dry_run_makes_no_writes() -> None:
    server = _InventoryServer()
    adapter = _make_mock_adapter(server.handler())
    try:
        result = await adapter.sync_inventory_items(99, [_dimm()], dry_run=True)
    finally:
        await adapter.aclose()

    assert result.created == ["DIMM_A1"]
    # Nothing actually persisted on the mock server.
    assert server.items == {}


async def test_sync_inventory_lists_only_discovered_items() -> None:
    """The list query MUST pass ``discovered=true`` so human items aren't reaped."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/dcim/inventory-items/":
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={"count": 0, "next": None, "previous": None, "results": []},
            )
        return httpx.Response(201, json={"id": 1})

    adapter = _make_mock_adapter(handler)
    try:
        await adapter.sync_inventory_items(99, [_dimm()])
    finally:
        await adapter.aclose()

    assert captured["params"]["discovered"] == "true"
    assert captured["params"]["device_id"] == "99"
