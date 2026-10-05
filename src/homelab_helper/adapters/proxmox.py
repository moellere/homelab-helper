"""Proxmox VE adapter — read-only management-plane source (Phase 3, L1).

The first management-plane adapter: it reads cluster + VM/LXC + node + storage
state from the Proxmox REST API (``/api2/json``) so the harness can reconcile a
hypervisor's view against kernel-probe ground truth and propose it into NetBox.

**Read-only at L1**, with one Phase-6 exception: the guest power methods under
the "writes" section exist solely for ``engine/executor.py``, which routes
every call through ``engine.trust.decide()`` first. Nothing else may call them.

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

from homelab_helper.secrets import secret_from_env

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
        token_secret = secret_from_env("HOMELAB_HELPER_PROXMOX_TOKEN_SECRET")
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

    # ----------------------------------------------------------------- writes
    #
    # The ONLY write surface at L2, and it exists solely for the executor:
    # every call site must have routed through engine.trust.decide() first.
    # Nothing else in the codebase may call these — the executor is the gate's
    # enforcement point, and grep-ability is part of the safety story.

    async def vm_current_status(self, node: str, vmid: int, kind: str) -> dict[str, Any]:
        """Live power state for one guest — rollback capture + verification."""
        return await self._request("GET", f"/nodes/{node}/{kind}/{vmid}/status/current") or {}

    async def vm_power(self, node: str, vmid: int, kind: str, action: str) -> Any:
        """Dispatch one power action (start|stop|shutdown|reboot) to a guest.

        ``kind`` is "qemu" or "lxc". Returns the Proxmox task UPID. Executor
        use only — see the block comment above.
        """
        if kind not in {"qemu", "lxc"}:
            raise ValueError(f"kind must be qemu or lxc, not {kind!r}")
        if action not in {"start", "stop", "shutdown", "reboot"}:
            raise ValueError(f"unsupported power action {action!r}")
        return await self._request("POST", f"/nodes/{node}/{kind}/{vmid}/status/{action}")

    async def list_snapshots(self, node: str, vmid: int, kind: str) -> list[dict[str, Any]]:
        """Existing snapshots for one guest. Read-only — also the support probe:
        a clean response means this guest's storage can snapshot at all."""
        return await self._request("GET", f"/nodes/{node}/{kind}/{vmid}/snapshot") or []

    async def create_snapshot(
        self, node: str, vmid: int, kind: str, name: str, *, description: str = ""
    ) -> Any:
        """Take a named snapshot. Executor-only — see the block comment above."""
        return await self._request(
            "POST",
            f"/nodes/{node}/{kind}/{vmid}/snapshot",
            params={"snapname": name, "description": description},
        )

    async def rollback_snapshot(self, node: str, vmid: int, kind: str, name: str) -> Any:
        """Restore a guest to a named snapshot. Executor-only."""
        return await self._request("POST", f"/nodes/{node}/{kind}/{vmid}/snapshot/{name}/rollback")

    async def vm_config(
        self, node: str, vmid: int, kind: str, *, pending: bool = False
    ) -> dict[str, Any]:
        """A guest's configuration. Read-only — the rollback orchestrator's probe for
        ``prior-config``. ``pending=True`` returns the QEMU pending view (current vs
        pending per key) so a change waiting on the next stop/start is visible."""
        if kind not in {"qemu", "lxc"}:
            raise ValueError(f"kind must be qemu or lxc, not {kind!r}")
        if pending and kind == "qemu":
            rows = await self._request("GET", f"/nodes/{node}/qemu/{vmid}/pending") or []
            return {str(r.get("key")): r for r in rows if isinstance(r, dict)}
        return await self._request("GET", f"/nodes/{node}/{kind}/{vmid}/config") or {}

    async def set_vm_config(self, node: str, vmid: int, kind: str, **options: Any) -> Any:
        """Change guest configuration keys (e.g. ``cpu="x86-64-v3"``). Executor-only.

        Uses the synchronous PUT; QEMU applies most keys at the next full
        stop/start and reports them under ``pending`` until then.
        """
        if kind not in {"qemu", "lxc"}:
            raise ValueError(f"kind must be qemu or lxc, not {kind!r}")
        if not options:
            raise ValueError("no configuration keys to set")
        return await self._request("PUT", f"/nodes/{node}/{kind}/{vmid}/config", params=options)

    async def migrate_guest(
        self, node: str, vmid: int, kind: str, target_node: str, *, online: bool = True
    ) -> Any:
        """Move a guest to another cluster node. Executor-only — see the block comment above.

        ``online`` keeps a running QEMU guest up during the move; an LXC guest
        cannot live-migrate, so the same flag asks Proxmox to restart it on the
        target (``restart=1``). Returns the Proxmox task UPID.
        """
        if kind not in {"qemu", "lxc"}:
            raise ValueError(f"kind must be qemu or lxc, not {kind!r}")
        if not target_node or target_node == node:
            raise ValueError("target_node must name a different node")
        params: dict[str, Any] = {"target": target_node}
        if online:
            params["online" if kind == "qemu" else "restart"] = 1
        return await self._request("POST", f"/nodes/{node}/{kind}/{vmid}/migrate", params=params)

    # ------------------------------------------------------------------ reads

    async def node_version(self, node: str) -> dict[str, Any]:
        """``pveversion`` for one node: ``version``, ``release``, ``repoid``."""
        return await self._request("GET", f"/nodes/{node}/version") or {}

    async def pending_updates(self, node: str) -> list[dict[str, Any]]:
        """The node's cached list of upgradable packages.

        A plain GET: it reads what the node's own daily ``apt update`` cached and
        never refreshes it (that would be a POST, and this adapter does not).
        """
        return await self._request("GET", f"/nodes/{node}/apt/update") or []

    async def list_backup_jobs(self) -> list[dict[str, Any]]:
        """Cluster-wide scheduled backup jobs (``/cluster/backup``)."""
        return await self._request("GET", "/cluster/backup") or []

    async def storage_content(
        self, node: str, storage: str, content: str = "backup"
    ) -> list[dict[str, Any]]:
        """Volumes of one content type on one storage, as seen from ``node``."""
        return (
            await self._request(
                "GET", f"/nodes/{node}/storage/{storage}/content", params={"content": content}
            )
            or []
        )

    async def rrd(
        self,
        node: str,
        timeframe: str,
        cf: str = "AVERAGE",
        *,
        vmid: int | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Round-robin usage points for a node, or a guest when ``vmid``/``kind`` are given."""
        path = (
            f"/nodes/{node}/{kind}/{vmid}/rrddata" if vmid is not None else f"/nodes/{node}/rrddata"
        )
        return await self._request("GET", path, params={"timeframe": timeframe, "cf": cf}) or []

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
