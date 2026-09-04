"""MikroTik RouterOS adapter — read-only management-plane source (L1).

The router/switch half of the network picture for labs that aren't UniFi.
RouterOS 7 ships a REST API (``/rest/<menu>/<path>``, HTTP basic auth, JSON),
and everything the harness wants from a router is a ``GET``:

- ``/rest/system/identity`` + ``/rest/system/resource`` — who it is, what it runs
- ``/rest/interface`` — physical/virtual interfaces with MACs
- ``/rest/ip/address`` — the subnets the router serves, per interface
- ``/rest/ip/dhcp-server/lease`` — hostname↔IP↔MAC: the client identity source
- ``/rest/ip/dns/static`` — static DNS: internal ``ServiceEndpoint`` rows

Those map straight onto the UniFi adapter's shapes (``parse_client``,
``parse_network``, ``parse_dns_record``), so the same reconcilers consume
both: static DNS → internal endpoints under resolver ``mikrotik`` (or
``mikrotik:<name>`` for a multi-router lab), and addresses + leases → the
stray-config pass (a served subnet with no leases).

**Read-only at L1.** The REST API can reconfigure anything; nothing here
sends other than ``GET``. Use a user in a read-only group (``read`` policy,
plus ``rest-api``) — that is all the harness needs.

Configuration::

    HOMELAB_HELPER_MIKROTIK_URL         https://router.lan   (RouterOS 7.1+; www-ssl enabled)
    HOMELAB_HELPER_MIKROTIK_USERNAME    <read-only user>
    HOMELAB_HELPER_MIKROTIK_PASSWORD    <password, or a secret reference>
    HOMELAB_HELPER_MIKROTIK_VERIFY_SSL  false   (default; RouterOS ships a self-signed cert)
    HOMELAB_HELPER_MIKROTIK_NAME        default (optional; tags the resolver in multi-router labs)

Tests inject an ``httpx.AsyncClient`` with ``MockTransport`` — no live router.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from homelab_helper.secrets import secret_from_env

_HTTP_ERROR_THRESHOLD = 400
_FALSEY = {"0", "false", "no"}


class MikroTikConfigError(RuntimeError):
    """Raised when required MikroTik configuration is missing."""


class MikroTikAPIError(RuntimeError):
    """Non-2xx response from RouterOS."""

    def __init__(self, status_code: int, detail: str, *, method: str, path: str) -> None:
        super().__init__(f"MikroTik {method} {path} -> {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class MikroTikConfig:
    url: str
    username: str
    password: str
    verify_ssl: bool = False  # RouterOS ships a self-signed cert
    timeout_s: float = 10.0
    name: str = "default"
    """Router label; lands in the DNS resolver tag so two routers' endpoints never collide."""

    @property
    def resolver(self) -> str:
        return "mikrotik" if self.name == "default" else f"mikrotik:{self.name}"

    @classmethod
    def from_env(cls) -> MikroTikConfig:
        url = os.environ.get("HOMELAB_HELPER_MIKROTIK_URL")
        username = os.environ.get("HOMELAB_HELPER_MIKROTIK_USERNAME")
        password = secret_from_env("HOMELAB_HELPER_MIKROTIK_PASSWORD")
        if not url or not username or not password:
            raise MikroTikConfigError(
                "MikroTik URL, username, and password are required. Set "
                "HOMELAB_HELPER_MIKROTIK_URL, HOMELAB_HELPER_MIKROTIK_USERNAME, and "
                "HOMELAB_HELPER_MIKROTIK_PASSWORD."
            )
        verify = (
            os.environ.get("HOMELAB_HELPER_MIKROTIK_VERIFY_SSL", "false").lower() not in _FALSEY
        )
        name = os.environ.get("HOMELAB_HELPER_MIKROTIK_NAME") or "default"
        return cls(url=url, username=username, password=password, verify_ssl=verify, name=name)


def _truthy(value: Any) -> bool:
    return str(value).lower() in {"true", "yes", "1"}


def parse_identity(raw: dict[str, Any]) -> str | None:
    return raw.get("name") or None


def parse_resource(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape ``/system/resource`` into the identity facts the harness keeps."""
    return {
        "version": raw.get("version"),
        "board": raw.get("board-name"),
        "architecture": raw.get("architecture-name"),
        "cpu": raw.get("cpu"),
        "cpu_count": _to_int(raw.get("cpu-count")),
        "memory_bytes": _to_int(raw.get("total-memory")),
        "uptime": raw.get("uptime"),
    }


def parse_interface(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": raw.get("name"),
        "type": raw.get("type"),
        "mac": (raw.get("mac-address") or "").lower() or None,
        "running": _truthy(raw.get("running")),
        "disabled": _truthy(raw.get("disabled")),
        "comment": raw.get("comment") or None,
    }


def parse_address(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one ``/ip/address`` row into the UniFi ``parse_network`` shape.

    ``address`` is ``10.0.1.1/24``; the subnet is what stray-config reasons
    over, and the interface name stands in for a network name.
    """
    address = str(raw.get("address") or "")
    return {
        "name": raw.get("interface"),
        "vlan_id": None,
        "subnet": address or None,
        "purpose": "corporate",
        "enabled": not _truthy(raw.get("disabled")),
        "vlan_enabled": False,
        "router_ip": address.split("/", 1)[0] or None,
        "comment": raw.get("comment") or None,
    }


def parse_lease(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one DHCP lease into the UniFi ``parse_client`` shape."""
    dynamic = _truthy(raw.get("dynamic"))
    return {
        "hostname": raw.get("host-name") or raw.get("comment") or None,
        "ip": raw.get("address") or None,
        "mac": (raw.get("mac-address") or "").lower() or None,
        "network_id": raw.get("server"),
        "fixed": not dynamic,
        "last_seen": raw.get("last-seen"),
        "status": raw.get("status"),
    }


def parse_static_dns(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one ``/ip/dns/static`` row into the UniFi ``parse_dns_record`` shape.

    RouterOS omits ``type`` for A records and carries CNAME targets in
    ``cname``; ``address`` holds A/AAAA targets.
    """
    record_type = str(raw.get("type") or "A").upper()
    value = raw.get("address") if record_type in {"A", "AAAA"} else raw.get("cname")
    return {
        "hostname": raw.get("name"),
        "value": value,
        "record_type": record_type,
        "enabled": not _truthy(raw.get("disabled")),
        "ttl": raw.get("ttl"),
    }


def _to_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value) != "" else None
    except (TypeError, ValueError):
        return None


class MikroTikAdapter:
    """Read-only async client for a RouterOS 7 REST API."""

    name = "mikrotik"

    def __init__(
        self,
        config: MikroTikConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if config is None and client is None:
            raise MikroTikConfigError("MikroTikAdapter needs a config or an injected client")
        self.config = config or MikroTikConfig(url="http://injected", username="x", password="x")
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_env(cls) -> MikroTikAdapter:
        return cls(MikroTikConfig.from_env())

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.url.rstrip("/") + "/rest",
            auth=(self.config.username, self.config.password),
            headers={"Accept": "application/json"},
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

    async def __aenter__(self) -> MikroTikAdapter:
        _ = self.client
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str) -> Any:
        response = await self.client.get(path)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            detail = response.text.strip()[:300] or response.reason_phrase
            raise MikroTikAPIError(response.status_code, detail, method="GET", path=path)
        if not response.content:
            return None
        return response.json()

    async def _rows(self, path: str) -> list[dict[str, Any]]:
        payload = await self._get(path) or []
        return [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []

    # ------------------------------------------------------------------ reads

    async def identity(self) -> str | None:
        raw = await self._get("/system/identity") or {}
        return parse_identity(raw if isinstance(raw, dict) else {})

    async def resource(self) -> dict[str, Any]:
        raw = await self._get("/system/resource") or {}
        return parse_resource(raw if isinstance(raw, dict) else {})

    async def list_interfaces(self) -> list[dict[str, Any]]:
        return [parse_interface(r) for r in await self._rows("/interface")]

    async def list_addresses(self) -> list[dict[str, Any]]:
        """Served subnets, shaped like UniFi networks for the stray-config pass."""
        return [parse_address(r) for r in await self._rows("/ip/address")]

    async def list_leases(self) -> list[dict[str, Any]]:
        """DHCP leases, shaped like UniFi clients (hostname↔IP↔MAC)."""
        return [parse_lease(r) for r in await self._rows("/ip/dhcp-server/lease")]

    async def list_dns_records(self) -> list[dict[str, Any]]:
        """Static DNS entries, shaped like UniFi DNS records."""
        return [parse_static_dns(r) for r in await self._rows("/ip/dns/static")]

    async def health_check(self) -> tuple[bool, str | None]:
        """Quick reachability/auth probe. Returns ``(ok, error_message)``."""
        try:
            await self._get("/system/identity")
        except (MikroTikAPIError, httpx.HTTPError) as exc:
            return False, str(exc)
        return True, None


__all__ = [
    "MikroTikAPIError",
    "MikroTikAdapter",
    "MikroTikConfig",
    "MikroTikConfigError",
    "parse_address",
    "parse_identity",
    "parse_interface",
    "parse_lease",
    "parse_resource",
    "parse_static_dns",
]
