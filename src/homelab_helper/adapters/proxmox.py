"""Proxmox VE adapter — read-only management-plane source (Phase 3, L1).

The first management-plane adapter: it reads cluster + VM/LXC + node + storage
state from the Proxmox REST API (``/api2/json``) so the harness can reconcile a
hypervisor's view against kernel-probe ground truth and propose it into NetBox.

**Read-only at L1.** Per the architecture, management-plane adapters expose only
reads until the Phase-6 trust gradient gates writes; there are deliberately no
mutate methods here.

Auth is an API token (no ticket/cookie dance): the ``Authorization:
PVEAPIToken=<id>=<secret>`` header. Proxmox ships a self-signed cert, so
``verify_ssl`` defaults to ``False`` (override per-instance once you've pinned a
CA). Responses wrap their payload in ``{"data": ...}``, which :meth:`_request`
unwraps.

Configuration (all required for live use)::

    HOMELAB_HELPER_PROXMOX_URL           https://pve.example.lan:8006
    HOMELAB_HELPER_PROXMOX_TOKEN_ID      user@realm!tokenname
    HOMELAB_HELPER_PROXMOX_TOKEN_SECRET  <uuid>

Tests inject an ``httpx.AsyncClient`` with ``MockTransport`` — no live cluster.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

_HTTP_ERROR_THRESHOLD = 400
_FALSEY = {"0", "false", "no"}


class ProxmoxConfigError(RuntimeError):
    """Raised when required Proxmox configuration is missing."""


class ProxmoxAPIError(RuntimeError):
    """Non-2xx response from the Proxmox API."""

    def __init__(self, status_code: int, detail: str, *, method: str, path: str) -> None:
        super().__init__(f"Proxmox {method} {path} -> {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ProxmoxConfig:
    url: str
    token_id: str
    token_secret: str
    verify_ssl: bool = False  # Proxmox ships a self-signed cert by default
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> ProxmoxConfig:
        url = os.environ.get("HOMELAB_HELPER_PROXMOX_URL")
        token_id = os.environ.get("HOMELAB_HELPER_PROXMOX_TOKEN_ID")
        token_secret = os.environ.get("HOMELAB_HELPER_PROXMOX_TOKEN_SECRET")
        if not url or not token_id or not token_secret:
            raise ProxmoxConfigError(
                "Proxmox URL + token are required. Set HOMELAB_HELPER_PROXMOX_URL, "
                "HOMELAB_HELPER_PROXMOX_TOKEN_ID, HOMELAB_HELPER_PROXMOX_TOKEN_SECRET."
            )
        verify = os.environ.get("HOMELAB_HELPER_PROXMOX_VERIFY_SSL", "false").lower() not in _FALSEY
        return cls(url=url, token_id=token_id, token_secret=token_secret, verify_ssl=verify)


def parse_cluster_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape ``/cluster/status`` into ``{name, quorate, node_count, nodes}``.

    Single-node installs have no ``type == cluster`` row — name is then ``None``.
    """
    cluster = next((r for r in rows if r.get("type") == "cluster"), None)
    nodes = [
        {
            "name": r.get("name"),
            "ip": r.get("ip"),
            "online": bool(r.get("online")),
            "nodeid": r.get("nodeid"),
            "local": bool(r.get("local")),
        }
        for r in rows
        if r.get("type") == "node"
    ]
    return {
        "name": cluster.get("name") if cluster else None,
        "quorate": bool(cluster.get("quorate")) if cluster else None,
        "node_count": len(nodes),
        "nodes": nodes,
    }


def parse_vm_resource(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one ``/cluster/resources?type=vm`` row into a stable VM/LXC dict."""
    return {
        "vmid": raw.get("vmid"),
        "name": raw.get("name"),
        "node": raw.get("node"),
        "type": raw.get("type"),  # "qemu" | "lxc"
        "status": raw.get("status"),  # "running" | "stopped"
        "template": bool(raw.get("template")),
        "maxcpu": raw.get("maxcpu"),
        "maxmem_bytes": raw.get("maxmem"),
        "maxdisk_bytes": raw.get("maxdisk"),
        "uptime_s": raw.get("uptime"),
    }


class ProxmoxAdapter:
    """Read-only async client for the Proxmox VE REST API."""

    name = "proxmox"

    def __init__(
        self,
        config: ProxmoxConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if config is None and client is None:
            raise ProxmoxConfigError("ProxmoxAdapter needs a config or an injected client")
        self.config = config or ProxmoxConfig(url="http://injected", token_id="x", token_secret="x")
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_env(cls) -> ProxmoxAdapter:
        return cls(ProxmoxConfig.from_env())

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.url.rstrip("/") + "/api2/json",
            headers={
                "Authorization": f"PVEAPIToken={self.config.token_id}={self.config.token_secret}",
                "Accept": "application/json",
            },
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

    async def __aenter__(self) -> ProxmoxAdapter:
        _ = self.client
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        response = await self.client.request(method, path, params=params)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            detail = response.text.strip()[:300] or response.reason_phrase
            raise ProxmoxAPIError(response.status_code, detail, method=method, path=path)
        if not response.content:
            return None
        payload = response.json()
        # Proxmox wraps the real payload under "data".
        return payload.get("data") if isinstance(payload, dict) else payload

    # ------------------------------------------------------------------ reads

    async def version(self) -> dict[str, Any]:
        return await self._request("GET", "/version") or {}

    async def cluster_status(self) -> dict[str, Any]:
        rows = await self._request("GET", "/cluster/status") or []
        return parse_cluster_status(rows)

    async def cluster_resources(self, resource_type: str) -> list[dict[str, Any]]:
        return (
            await self._request("GET", "/cluster/resources", params={"type": resource_type}) or []
        )

    async def list_vms(self) -> list[dict[str, Any]]:
        """All VMs + LXC containers across the cluster (templates included)."""
        return [parse_vm_resource(r) for r in await self.cluster_resources("vm")]

    async def list_nodes(self) -> list[dict[str, Any]]:
        return await self.cluster_resources("node")

    async def list_storage(self) -> list[dict[str, Any]]:
        return await self.cluster_resources("storage")

    async def health_check(self) -> tuple[bool, str | None]:
        """Quick reachability/auth probe. Returns ``(ok, error_message)``."""
        try:
            await self.version()
        except (ProxmoxAPIError, httpx.HTTPError) as exc:
            return False, str(exc)
        return True, None


__all__ = [
    "ProxmoxAPIError",
    "ProxmoxAdapter",
    "ProxmoxConfig",
    "ProxmoxConfigError",
    "parse_cluster_status",
    "parse_vm_resource",
]
