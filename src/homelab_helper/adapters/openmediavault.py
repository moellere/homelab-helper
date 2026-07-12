"""OpenMediaVault adapter — read-only NAS management-plane source (Phase 3, L1).

The storage-appliance source. Where Proxmox/K8s/UniFi cover compute and network,
OpenMediaVault owns the NAS facts a headless probe can't reach without vendor
credentials: mounted filesystems/pools, per-disk S.M.A.R.T. identity, the shared
folders it exports, and the state of its services. (This is why the SSH path's
``host.smart`` probe was chosen *over* an OMV adapter earlier — OMV now lands as
its own management-plane source, the same pattern as UniFi/Proxmox.)

**Read-only at L1.** OMV's RPC can obviously reconfigure the box; the harness
only reads until the Phase-6 trust gradient gates writes. No RPC method that
mutates state is exposed here.

OMV speaks JSON-RPC over a single ``/rpc.php`` endpoint: every call POSTs
``{"service", "method", "params", "options"}`` and gets back
``{"response": ..., "error": null}`` (HTTP 200 even on RPC errors, so
:meth:`_rpc` inspects the envelope). Auth is session-based — a ``session.login``
call sets a cookie the httpx client then carries; :meth:`_login` runs it lazily
before the first read.

Configuration::

    HOMELAB_HELPER_OMV_URL         https://nas.example.lan
    HOMELAB_HELPER_OMV_USERNAME    <admin or read-capable user>
    HOMELAB_HELPER_OMV_PASSWORD    <password>

The exact RPC method names track OMV 6/7; they're centralized as constants so a
version skew is a one-line change. Tests inject an ``httpx.AsyncClient`` with
``MockTransport`` — no live appliance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

_HTTP_ERROR_THRESHOLD = 400
_FALSEY = {"0", "false", "no"}

# RPC (service, method) pairs — centralized so an OMV version skew is one edit.
_RPC_LOGIN = ("session", "login")
_RPC_FILESYSTEMS = ("FileSystemMgmt", "enumerateMountedFilesystems")
_RPC_SMART_DEVICES = ("Smart", "enumerateDevices")
_RPC_SHARED_FOLDERS = ("ShareMgmt", "enumerateSharedFolders")
_RPC_SERVICES = ("Services", "getStatus")


class OpenMediaVaultConfigError(RuntimeError):
    """Raised when required OpenMediaVault configuration is missing."""


class OpenMediaVaultAPIError(RuntimeError):
    """Non-2xx HTTP, or an RPC envelope carrying an ``error`` object."""

    def __init__(self, detail: str, *, service: str, method: str) -> None:
        super().__init__(f"OMV {service}.{method} -> {detail}")
        self.detail = detail
        self.service = service
        self.method = method


@dataclass(frozen=True)
class OpenMediaVaultConfig:
    url: str
    username: str
    password: str
    verify_ssl: bool = False  # OMV commonly ships a self-signed cert
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> OpenMediaVaultConfig:
        url = os.environ.get("HOMELAB_HELPER_OMV_URL")
        username = os.environ.get("HOMELAB_HELPER_OMV_USERNAME")
        password = os.environ.get("HOMELAB_HELPER_OMV_PASSWORD")
        if not url or not username or not password:
            raise OpenMediaVaultConfigError(
                "OMV URL + username + password are required. Set HOMELAB_HELPER_OMV_URL, "
                "HOMELAB_HELPER_OMV_USERNAME, and HOMELAB_HELPER_OMV_PASSWORD."
            )
        verify = os.environ.get("HOMELAB_HELPER_OMV_VERIFY_SSL", "false").lower() not in _FALSEY
        return cls(url=url, username=username, password=password, verify_ssl=verify)


def _to_int(value: Any) -> int | None:
    """OMV reports byte sizes as strings; coerce to int, tolerating junk."""
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def parse_filesystem(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one ``enumerateMountedFilesystems`` row."""
    return {
        "device": raw.get("canonicaldevicefile") or raw.get("devicename"),
        "type": raw.get("type"),
        "mountpoint": raw.get("mountpoint") or raw.get("dir"),
        "label": raw.get("label") or None,
        "size_bytes": _to_int(raw.get("size")),
        "used_bytes": _to_int(raw.get("used")),
        "available_bytes": _to_int(raw.get("available")),
        "used_percent": raw.get("percentage"),
    }


def parse_smart_device(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one ``Smart.enumerateDevices`` row (disk identity)."""
    return {
        "device": raw.get("canonicaldevicefile") or raw.get("devicename"),
        "model": raw.get("model") or None,
        "serial": raw.get("serialnumber") or None,
        "vendor": raw.get("vendor") or None,
        "size_bytes": _to_int(raw.get("size")),
        "temperature": raw.get("temperature"),
    }


def parse_shared_folder(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one ``ShareMgmt.enumerateSharedFolders`` row."""
    return {
        "uuid": raw.get("uuid"),
        "name": raw.get("name"),
        "comment": raw.get("comment") or None,
        "path": raw.get("reldirpath") or raw.get("path"),
        "device": raw.get("device") or None,
    }


def parse_service(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one ``Services.getStatus`` row."""
    return {
        "name": raw.get("name"),
        "title": raw.get("title") or raw.get("name"),
        "enabled": bool(raw.get("enabled")),
        "running": bool(raw.get("running")),
    }


class OpenMediaVaultAdapter:
    """Read-only async JSON-RPC client for OpenMediaVault."""

    name = "openmediavault"

    def __init__(
        self,
        config: OpenMediaVaultConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if config is None and client is None:
            raise OpenMediaVaultConfigError(
                "OpenMediaVaultAdapter needs a config or an injected client"
            )
        self.config = config or OpenMediaVaultConfig(
            url="http://injected", username="x", password="x"
        )
        self._client = client
        self._owns_client = client is None
        self._authenticated = False

    @classmethod
    def from_env(cls) -> OpenMediaVaultAdapter:
        return cls(OpenMediaVaultConfig.from_env())

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.url.rstrip("/"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=self.config.timeout_s,
            verify=self.config.verify_ssl,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> OpenMediaVaultAdapter:
        _ = self.client
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _rpc(self, service: str, method: str, params: Any = None) -> Any:
        """POST one JSON-RPC call to /rpc.php and unwrap the response envelope."""
        body = {"service": service, "method": method, "params": params, "options": None}
        response = await self.client.post("/rpc.php", json=body)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            detail = response.text.strip()[:300] or response.reason_phrase
            raise OpenMediaVaultAPIError(
                f"HTTP {response.status_code}: {detail}", service=service, method=method
            )
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            detail = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise OpenMediaVaultAPIError(detail, service=service, method=method)
        return payload.get("response") if isinstance(payload, dict) else payload

    async def _login(self) -> None:
        """Establish a session (cookie carried by the client) once per instance."""
        if self._authenticated:
            return
        service, method = _RPC_LOGIN
        result = await self._rpc(
            service,
            method,
            {"username": self.config.username, "password": self.config.password},
        )
        if isinstance(result, dict) and result.get("authenticated") is False:
            raise OpenMediaVaultAPIError("authentication rejected", service=service, method=method)
        self._authenticated = True

    @staticmethod
    def _rows(response: Any) -> list[dict[str, Any]]:
        """Normalize OMV's two list shapes: a bare list, or ``{total, data:[...]}``."""
        if isinstance(response, dict):
            data = response.get("data")
            return data if isinstance(data, list) else []
        return response if isinstance(response, list) else []

    # ------------------------------------------------------------------ reads

    async def list_filesystems(self) -> list[dict[str, Any]]:
        await self._login()
        rows = self._rows(await self._rpc(*_RPC_FILESYSTEMS))
        return [parse_filesystem(r) for r in rows]

    async def list_smart_devices(self) -> list[dict[str, Any]]:
        await self._login()
        rows = self._rows(await self._rpc(*_RPC_SMART_DEVICES))
        return [parse_smart_device(r) for r in rows]

    async def list_shared_folders(self) -> list[dict[str, Any]]:
        await self._login()
        rows = self._rows(await self._rpc(*_RPC_SHARED_FOLDERS))
        return [parse_shared_folder(r) for r in rows]

    async def list_services(self) -> list[dict[str, Any]]:
        await self._login()
        rows = self._rows(await self._rpc(*_RPC_SERVICES))
        return [parse_service(r) for r in rows]

    async def health_check(self) -> tuple[bool, str | None]:
        """Quick reachability/auth probe — a session login."""
        try:
            await self._login()
        except (OpenMediaVaultAPIError, httpx.HTTPError) as exc:
            return False, str(exc)
        return True, None


__all__ = [
    "OpenMediaVaultAPIError",
    "OpenMediaVaultAdapter",
    "OpenMediaVaultConfig",
    "OpenMediaVaultConfigError",
    "parse_filesystem",
    "parse_service",
    "parse_shared_folder",
    "parse_smart_device",
]
