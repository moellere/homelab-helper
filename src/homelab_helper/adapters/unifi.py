"""UniFi Network adapter — read-only management-plane source (Phase 3, L1).

The third management-plane adapter (after Proxmox and Kubernetes). It reads the
UniFi Network controller's internal DNS records, known clients (hostname↔IP,
the internal-resolution ground truth), and network/VLAN definitions — the data
that feeds internal ``ServiceEndpoint`` reconciliation and, later, stray-config
detection.

**Read-only at L1.** Like the other management-plane adapters, this exposes
only reads until the Phase-6 trust gradient gates writes.

Auth is an API key sent in the ``X-API-KEY`` header (UniFi OS 3.x / Network
9.x+). UniFi ships a self-signed cert, so ``verify_ssl`` defaults to ``False``.
The controller proxies the Network app under ``/proxy/network`` and scopes most
reads to a site (default ``default``). Responses wrap their payload in
``{"meta": ..., "data": [...]}``, which :meth:`_request` unwraps.

Configuration (all required for live use)::

    HOMELAB_HELPER_UNIFI_URL       https://unifi.example.lan
    HOMELAB_HELPER_UNIFI_API_KEY   <api-key from UniFi OS control-plane>
    HOMELAB_HELPER_UNIFI_SITE      default        (optional; defaults to "default")

Tests inject an ``httpx.AsyncClient`` with ``MockTransport`` — no live
controller.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

_HTTP_ERROR_THRESHOLD = 400
_FALSEY = {"0", "false", "no"}


class UniFiConfigError(RuntimeError):
    """Raised when required UniFi configuration is missing."""


class UniFiAPIError(RuntimeError):
    """Non-2xx response from the UniFi controller."""

    def __init__(self, status_code: int, detail: str, *, method: str, path: str) -> None:
        super().__init__(f"UniFi {method} {path} -> {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class UniFiConfig:
    url: str
    api_key: str
    site: str = "default"
    verify_ssl: bool = False  # UniFi ships a self-signed cert by default
    timeout_s: float = 10.0
    name: str = "default"
    """Controller label. Distinguishes gateways in a multi-site lab, and lands
    in the DNS resolver tag so two controllers' endpoints never collide."""

    @property
    def resolver(self) -> str:
        """Resolver tag for ServiceEndpoints produced by this controller."""
        return "unifi" if self.name == "default" else f"unifi:{self.name}"

    @classmethod
    def from_env(cls) -> UniFiConfig:
        url = os.environ.get("HOMELAB_HELPER_UNIFI_URL")
        api_key = os.environ.get("HOMELAB_HELPER_UNIFI_API_KEY")
        if not url or not api_key:
            raise UniFiConfigError(
                "UniFi URL + API key are required. Set HOMELAB_HELPER_UNIFI_URL and "
                "HOMELAB_HELPER_UNIFI_API_KEY."
            )
        site = os.environ.get("HOMELAB_HELPER_UNIFI_SITE") or "default"
        verify = os.environ.get("HOMELAB_HELPER_UNIFI_VERIFY_SSL", "false").lower() not in _FALSEY
        name = os.environ.get("HOMELAB_HELPER_UNIFI_NAME") or "default"
        return cls(url=url, api_key=api_key, site=site, verify_ssl=verify, name=name)

    @classmethod
    def all_from_env(cls) -> list[UniFiConfig]:
        """Every configured controller, for labs with more than one gateway.

        ``HOMELAB_HELPER_UNIFI_CONTROLLERS=covington,wyola`` opts in; each name
        then reads ``HOMELAB_HELPER_UNIFI_<NAME>_URL`` / ``_API_KEY`` (and
        optional ``_SITE`` / ``_VERIFY_SSL``). An API key is per-controller —
        one gateway's key is rejected by another. With the list unset this is
        the single unsuffixed controller, so existing setups are unaffected.
        """
        raw = os.environ.get("HOMELAB_HELPER_UNIFI_CONTROLLERS") or ""
        names = [n.strip() for n in raw.split(",") if n.strip()]
        if not names:
            return [cls.from_env()]
        configs: list[UniFiConfig] = []
        for name in names:
            prefix = f"HOMELAB_HELPER_UNIFI_{name.upper().replace('-', '_')}_"
            url = os.environ.get(prefix + "URL")
            api_key = os.environ.get(prefix + "API_KEY")
            if not url or not api_key:
                raise UniFiConfigError(
                    f"UniFi controller {name!r} is listed in "
                    f"HOMELAB_HELPER_UNIFI_CONTROLLERS but needs {prefix}URL and {prefix}API_KEY."
                )
            verify = os.environ.get(prefix + "VERIFY_SSL", "false").lower() not in _FALSEY
            configs.append(
                cls(
                    url=url,
                    api_key=api_key,
                    site=os.environ.get(prefix + "SITE") or "default",
                    verify_ssl=verify,
                    name=name,
                )
            )
        return configs


def parse_dns_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one UniFi ``/rest/dnsrecord`` row into a stable record dict.

    UniFi names the hostname ``key`` and the target ``value``; ``record_type``
    is ``A`` / ``AAAA`` / ``CNAME`` / ``TXT`` / … Disabled records still list.
    """
    return {
        "hostname": raw.get("key"),
        "value": raw.get("value"),
        "record_type": raw.get("record_type") or "A",
        "enabled": raw.get("enabled", True),
        "ttl": raw.get("ttl"),
    }


def parse_client(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one UniFi ``/rest/user`` (known client) row.

    The internal-resolution IP is the DHCP reservation (``fixed_ip`` when
    ``use_fixedip``) if present, else the last-seen lease ``ip``. ``hostname``
    is preferred over the user-set ``name`` for the resolvable label.
    """
    fixed = raw.get("use_fixedip") and raw.get("fixed_ip")
    return {
        "hostname": raw.get("hostname") or raw.get("name"),
        "ip": (raw.get("fixed_ip") if fixed else raw.get("ip")) or None,
        "mac": (raw.get("mac") or "").lower() or None,
        "network_id": raw.get("network_id"),
        "fixed": bool(fixed),
        "last_seen": raw.get("last_seen"),
    }


def parse_network(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one UniFi ``/rest/networkconf`` (network/VLAN) row."""
    return {
        "name": raw.get("name"),
        "vlan_id": raw.get("vlan") if isinstance(raw.get("vlan"), int) else None,
        "subnet": raw.get("ip_subnet"),
        "purpose": raw.get("purpose"),  # "corporate" | "vlan-only" | "wan" | ...
        "enabled": raw.get("enabled", True),
        "vlan_enabled": bool(raw.get("vlan_enabled")),
    }


class UniFiAdapter:
    """Read-only async client for the UniFi Network controller."""

    name = "unifi"

    def __init__(
        self,
        config: UniFiConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if config is None and client is None:
            raise UniFiConfigError("UniFiAdapter needs a config or an injected client")
        self.config = config or UniFiConfig(url="http://injected", api_key="x")
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_env(cls) -> UniFiAdapter:
        return cls(UniFiConfig.from_env())

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.url.rstrip("/") + "/proxy/network",
            headers={"X-API-KEY": self.config.api_key, "Accept": "application/json"},
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

    async def __aenter__(self) -> UniFiAdapter:
        _ = self.client
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _site_path(self, tail: str) -> str:
        return f"/api/s/{self.config.site}/{tail.lstrip('/')}"

    def _v2_site_path(self, tail: str) -> str:
        """v2 API path. Static DNS lives here; the v1 ``rest/dnsrecord`` route
        answers ``api.err.InvalidObject`` on current controllers."""
        return f"/v2/api/site/{self.config.site}/{tail.lstrip('/')}"

    async def _request(self, method: str, path: str) -> Any:
        response = await self.client.request(method, path)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            detail = response.text.strip()[:300] or response.reason_phrase
            raise UniFiAPIError(response.status_code, detail, method=method, path=path)
        if not response.content:
            return None
        payload = response.json()
        # UniFi wraps list payloads under "data".
        return payload.get("data") if isinstance(payload, dict) else payload

    # ------------------------------------------------------------------ reads

    async def list_dns_records(self) -> list[dict[str, Any]]:
        """Static DNS entries. The v2 route returns a bare JSON array rather
        than the ``{meta, data}`` envelope — ``_request`` passes both through."""
        rows = await self._request("GET", self._v2_site_path("static-dns")) or []
        return [parse_dns_record(r) for r in rows]

    async def list_clients(self) -> list[dict[str, Any]]:
        """Known clients (hostname↔IP). The internal-resolution ground truth."""
        rows = await self._request("GET", self._site_path("rest/user")) or []
        return [parse_client(r) for r in rows]

    async def list_networks(self) -> list[dict[str, Any]]:
        rows = await self._request("GET", self._site_path("rest/networkconf")) or []
        return [parse_network(r) for r in rows]

    async def health_check(self) -> tuple[bool, str | None]:
        """Quick reachability/auth probe. Returns ``(ok, error_message)``."""
        try:
            await self._request("GET", self._site_path("self"))
        except (UniFiAPIError, httpx.HTTPError) as exc:
            return False, str(exc)
        return True, None


__all__ = [
    "UniFiAPIError",
    "UniFiAdapter",
    "UniFiConfig",
    "UniFiConfigError",
    "parse_client",
    "parse_dns_record",
    "parse_network",
]
