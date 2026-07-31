"""OpenMediaVaultAdapter unit tests against httpx.MockTransport — no live NAS."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from homelab_helper.adapters.openmediavault import (
    OpenMediaVaultAdapter,
    OpenMediaVaultAPIError,
    OpenMediaVaultConfig,
    OpenMediaVaultConfigError,
    parse_filesystem,
    parse_nfs_export,
    parse_service,
    parse_shared_folder,
    parse_smart_device,
    parse_smb_share,
)

_CONFIG = OpenMediaVaultConfig(url="https://nas.example.lan", username="admin", password="pw")


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> OpenMediaVaultAdapter:
    client = httpx.AsyncClient(
        base_url=_CONFIG.url.rstrip("/"), transport=httpx.MockTransport(handler)
    )
    return OpenMediaVaultAdapter(_CONFIG, client=client)


def _ok(response: object) -> dict:
    """OMV wraps every RPC result in a {response, error} envelope."""
    return {"response": response, "error": None}


def _rpc_of(request: httpx.Request) -> tuple[str, str]:
    body = json.loads(request.content)
    return body["service"], body["method"]


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_from_env_requires_all_three(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_OMV_URL", "https://nas.lan")
    monkeypatch.setenv("HOMELAB_HELPER_OMV_USERNAME", "admin")
    monkeypatch.delenv("HOMELAB_HELPER_OMV_PASSWORD", raising=False)
    with pytest.raises(OpenMediaVaultConfigError):
        OpenMediaVaultConfig.from_env()


def test_config_from_env_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_OMV_URL", "https://nas.lan")
    monkeypatch.setenv("HOMELAB_HELPER_OMV_USERNAME", "admin")
    monkeypatch.setenv("HOMELAB_HELPER_OMV_PASSWORD", "pw")
    monkeypatch.delenv("HOMELAB_HELPER_OMV_VERIFY_SSL", raising=False)
    cfg = OpenMediaVaultConfig.from_env()
    assert cfg.username == "admin"
    assert cfg.verify_ssl is False


def test_adapter_requires_config_or_client() -> None:
    with pytest.raises(OpenMediaVaultConfigError):
        OpenMediaVaultAdapter()


# ---------------------------------------------------------------------------
# parse functions (pure)
# ---------------------------------------------------------------------------


def test_parse_filesystem_coerces_byte_strings() -> None:
    out = parse_filesystem(
        {
            "canonicaldevicefile": "/dev/sda1",
            "type": "ext4",
            "mountpoint": "/srv/dev-disk-by-uuid-x",
            "label": "data",
            "size": "1000204886016",
            "used": "500000000000",
            "available": "500204886016",
            "percentage": 50,
        }
    )
    assert out["device"] == "/dev/sda1"
    assert out["size_bytes"] == 1000204886016
    assert out["used_percent"] == 50


def test_parse_filesystem_tolerates_missing_sizes() -> None:
    out = parse_filesystem({"devicename": "/dev/sdb1", "type": "xfs"})
    assert out["device"] == "/dev/sdb1"
    assert out["size_bytes"] is None


def test_parse_smart_device_maps_identity() -> None:
    out = parse_smart_device(
        {"canonicaldevicefile": "/dev/sda", "model": "WDC", "serialnumber": "WD-123"}
    )
    assert out["device"] == "/dev/sda"
    assert out["model"] == "WDC"
    assert out["serial"] == "WD-123"


def test_parse_shared_folder_maps_path() -> None:
    out = parse_shared_folder(
        {"uuid": "u1", "name": "media", "reldirpath": "media/", "comment": "movies"}
    )
    assert out["name"] == "media"
    assert out["path"] == "media/"
    assert out["comment"] == "movies"


def test_parse_service_coerces_bools() -> None:
    out = parse_service({"name": "smb", "title": "SMB/CIFS", "enabled": True, "running": False})
    assert out["name"] == "smb"
    assert out["enabled"] is True
    assert out["running"] is False


# ---------------------------------------------------------------------------
# reads over the mock transport (with session login)
# ---------------------------------------------------------------------------


_RESPONSES: dict[tuple[str, str], object] = {
    ("session", "login"): {"authenticated": True, "username": "admin"},
    ("FileSystemMgmt", "enumerateMountedFilesystems"): [
        {"canonicaldevicefile": "/dev/sda1", "type": "ext4", "size": "1000", "used": "400"}
    ],
    ("Smart", "enumerateDevices"): [
        {"canonicaldevicefile": "/dev/sda", "model": "WDC", "serialnumber": "S1"}
    ],
    ("ShareMgmt", "enumerateSharedFolders"): [
        {"uuid": "u1", "name": "media", "reldirpath": "media/"}
    ],
    # OMV's getStatus returns the {total, data} shape.
    ("Services", "getStatus"): {
        "total": 1,
        "data": [{"name": "smb", "enabled": True, "running": True}],
    },
    ("NFS", "getShareList"): [{"uuid": "n1", "sharedfoldername": "media", "client": "10.0.1.0/24"}],
    ("SMB", "getShareList"): [{"uuid": "s1", "name": "media", "sharedfoldername": "media"}],
}


def _default_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_ok(_RESPONSES.get(_rpc_of(request), [])))


async def test_login_happens_before_reads() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_rpc_of(request))
        return _default_handler(request)

    adapter = _adapter(handler)
    try:
        fs = await adapter.list_filesystems()
    finally:
        await adapter.aclose()
    assert seen[0] == ("session", "login")  # login precedes the read
    assert fs[0]["device"] == "/dev/sda1"


async def test_login_only_once_across_reads() -> None:
    logins = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal logins
        if _rpc_of(request) == ("session", "login"):
            logins += 1
        return _default_handler(request)

    adapter = _adapter(handler)
    try:
        await adapter.list_filesystems()
        await adapter.list_services()
        await adapter.list_smart_devices()
    finally:
        await adapter.aclose()
    assert logins == 1  # session reused


async def test_services_unwraps_total_data_shape() -> None:
    adapter = _adapter(_default_handler)
    try:
        services = await adapter.list_services()
    finally:
        await adapter.aclose()
    assert services == [{"name": "smb", "title": "smb", "enabled": True, "running": True}]


async def test_shared_folders_read() -> None:
    adapter = _adapter(_default_handler)
    try:
        shares = await adapter.list_shared_folders()
    finally:
        await adapter.aclose()
    assert shares[0]["name"] == "media"


def test_parse_nfs_export_maps_client() -> None:
    out = parse_nfs_export({"uuid": "n1", "sharedfoldername": "media", "client": "10.0.1.0/24"})
    assert out["protocol"] == "nfs"
    assert out["shared_folder"] == "media"
    assert out["client"] == "10.0.1.0/24"


def test_parse_smb_share_maps_readonly() -> None:
    out = parse_smb_share({"uuid": "s1", "name": "media", "readonly": True})
    assert out["protocol"] == "smb"
    assert out["name"] == "media"
    assert out["readonly"] is True


async def test_nfs_and_smb_exports_read() -> None:
    adapter = _adapter(_default_handler)
    try:
        nfs = await adapter.list_nfs_exports()
        smb = await adapter.list_smb_shares()
    finally:
        await adapter.aclose()
    assert nfs[0]["shared_folder"] == "media"
    assert smb[0]["name"] == "media"


async def test_rpc_error_envelope_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _rpc_of(request) == ("session", "login"):
            return httpx.Response(200, json=_ok({"authenticated": True}))
        return httpx.Response(
            200, json={"response": None, "error": {"code": 5000, "message": "boom"}}
        )

    adapter = _adapter(handler)
    try:
        with pytest.raises(OpenMediaVaultAPIError) as excinfo:
            await adapter.list_filesystems()
    finally:
        await adapter.aclose()
    assert "boom" in str(excinfo.value)


async def test_health_check_ok() -> None:
    adapter = _adapter(_default_handler)
    try:
        ok, err = await adapter.health_check()
    finally:
        await adapter.aclose()
    assert ok is True
    assert err is None


async def test_health_check_reports_login_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    adapter = _adapter(handler)
    try:
        ok, err = await adapter.health_check()
    finally:
        await adapter.aclose()
    assert ok is False
    assert err is not None


async def test_share_list_rpcs_send_omv_paging_params() -> None:
    """OMV validates getShareList against its paged-query schema.

    Omitting start/limit/sortfield/sortdir is a 400 ("Missing 'required'
    attribute 'start'"), not an unpaged result — so the params are part of the
    contract, not an optimization. The enumerate-style RPCs take none.
    """
    bodies: dict[tuple[str, str], object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        bodies[(payload["service"], payload["method"])] = payload["params"]
        return _default_handler(request)

    adapter = _adapter(handler)
    try:
        await adapter.list_nfs_exports()
        await adapter.list_smb_shares()
        await adapter.list_filesystems()
    finally:
        await adapter.aclose()

    for rpc in (("NFS", "getShareList"), ("SMB", "getShareList")):
        params = bodies[rpc]
        assert isinstance(params, dict), f"{rpc} sent {params!r}"
        assert params["start"] == 0
        assert params["limit"] == -1  # OMV's "no limit"
        assert "sortfield" in params
        assert "sortdir" in params

    # Enumerate-style RPCs are unchanged — no params.
    assert bodies[("FileSystemMgmt", "enumerateMountedFilesystems")] is None


async def test_parse_smb_share_falls_back_to_shared_folder_for_label() -> None:
    """Real OMV rows leave `name` empty; the shared folder is the label."""
    parsed = parse_smb_share({"uuid": "s2", "sharedfoldername": "backup", "readonly": False})
    assert parsed["name"] is None
    assert parsed["shared_folder"] == "backup"
