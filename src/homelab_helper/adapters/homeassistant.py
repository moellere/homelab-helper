"""Home Assistant adapter — read-only automation-hub source (L1).

The most common service in a homelab, and until now the only one on the list
with no adapter. Home Assistant carries two kinds of cross-source signal the
harness wants: which *integrations* it has configured (a second opinion on
what management planes and devices exist — ``proxmoxve``, ``unifi``, ``mqtt``,
…), and an entity population that says what the hub actually controls.

**Read-only at L1.** HA's REST API can call services (turn things on, restart
add-ons); nothing here does. Only ``GET`` routes are used, and a token from a
non-admin HA user is sufficient.

Auth is a long-lived access token as a Bearer credential. The REST API lives
under ``/api``; every route answers JSON. Reads in this slice:

- ``GET /api/``          liveness (``{"message": "API running."}``)
- ``GET /api/config``    version, location, loaded components
- ``GET /api/states``    every entity's state; summarized by domain
- ``GET /api/services``  service domains the hub exposes

Configuration::

    HOMELAB_HELPER_HASS_URL         http://homeassistant.lan:8123
    HOMELAB_HELPER_HASS_TOKEN       <long-lived access token>
    HOMELAB_HELPER_HASS_VERIFY_SSL  true   (optional; HA is often plain http)

The device registry (websocket API) and ``device_tracker`` IP identity are a
later slice. Tests inject an ``httpx.AsyncClient`` with ``MockTransport``.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from typing import Any

import httpx

from homelab_helper.secrets import secret_from_env

_HTTP_ERROR_THRESHOLD = 400
_FALSEY = {"0", "false", "no"}

INTEGRATIONS_OF_INTEREST: frozenset[str] = frozenset(
    {
        "adguard",
        "cloudflare",
        "esphome",
        "frigate",
        "glances",
        "jellyfin",
        "mqtt",
        "nut",
        "pi_hole",
        "plex",
        "proxmoxve",
        "synology_dsm",
        "systemmonitor",
        "tailscale",
        "unifi",
        "unifiprotect",
        "wireguard",
        "zha",
        "zwave_js",
    }
)
"""Loaded HA components that name infrastructure the harness models elsewhere.

Surfaced separately from the full component list so the cross-source pass can
compare "HA talks to a Proxmox" against the Proxmox the harness knows.
"""


class HomeAssistantConfigError(RuntimeError):
    """Raised when required Home Assistant configuration is missing."""


class HomeAssistantAPIError(RuntimeError):
    """Non-2xx response from Home Assistant."""

    def __init__(self, status_code: int, detail: str, *, method: str, path: str) -> None:
        super().__init__(f"Home Assistant {method} {path} -> {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class HomeAssistantConfig:
    url: str
    token: str
    verify_ssl: bool = True
    timeout_s: float = 10.0
    name: str = "default"
    """Instance label. Distinguishes hubs in a multi-site lab, and lands in the
    Service name and endpoint resolver tag so two instances never collide."""

    @property
    def service_name(self) -> str:
        return "home-assistant" if self.name == "default" else f"home-assistant-{self.name}"

    @property
    def resolver(self) -> str:
        return "home-assistant" if self.name == "default" else f"home-assistant:{self.name}"

    @classmethod
    def from_env(cls) -> HomeAssistantConfig:
        url = os.environ.get("HOMELAB_HELPER_HASS_URL")
        token = secret_from_env("HOMELAB_HELPER_HASS_TOKEN")
        if not url or not token:
            raise HomeAssistantConfigError(
                "Home Assistant URL + token are required. Set HOMELAB_HELPER_HASS_URL and "
                "HOMELAB_HELPER_HASS_TOKEN (a long-lived access token)."
            )
        verify = os.environ.get("HOMELAB_HELPER_HASS_VERIFY_SSL", "true").lower() not in _FALSEY
        name = os.environ.get("HOMELAB_HELPER_HASS_NAME") or "default"
        return cls(url=url, token=token, verify_ssl=verify, name=name)

    @classmethod
    def all_from_env(cls) -> list[HomeAssistantConfig]:
        """Every configured Home Assistant instance, for labs with more than one hub.

        ``HOMELAB_HELPER_HASS_INSTANCES=main,cabin`` opts in; each name then reads
        ``HOMELAB_HELPER_HASS_<NAME>_URL`` / ``_TOKEN`` (and optional ``_VERIFY_SSL``).
        With the list unset this is the single unsuffixed instance.
        """
        raw = os.environ.get("HOMELAB_HELPER_HASS_INSTANCES") or ""
        names = [n.strip() for n in raw.split(",") if n.strip()]
        if not names:
            return [cls.from_env()]
        configs: list[HomeAssistantConfig] = []
        for name in names:
            prefix = f"HOMELAB_HELPER_HASS_{name.upper().replace('-', '_')}_"
            url = os.environ.get(prefix + "URL")
            token = secret_from_env(prefix + "TOKEN")
            if not url or not token:
                raise HomeAssistantConfigError(
                    f"Home Assistant instance {name!r} is listed in "
                    f"HOMELAB_HELPER_HASS_INSTANCES but needs {prefix}URL and {prefix}TOKEN."
                )
            verify = os.environ.get(prefix + "VERIFY_SSL", "true").lower() not in _FALSEY
            configs.append(cls(url=url, token=token, verify_ssl=verify, name=name))
        return configs


def parse_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape ``GET /api/config`` into the stable fields the harness keeps."""
    components = raw.get("components") or []
    return {
        "version": raw.get("version"),
        "location_name": raw.get("location_name"),
        "time_zone": raw.get("time_zone"),
        "state": raw.get("state"),
        "internal_url": raw.get("internal_url"),
        "external_url": raw.get("external_url"),
        "components": sorted(str(c) for c in components),
    }


def parse_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one ``GET /api/states`` row; the domain is the entity id's prefix."""
    entity_id = str(raw.get("entity_id") or "")
    attributes = raw.get("attributes") or {}
    shaped = {
        "entity_id": entity_id,
        "domain": entity_id.split(".", 1)[0] if "." in entity_id else None,
        "state": raw.get("state"),
        "name": attributes.get("friendly_name"),
        "last_changed": raw.get("last_changed"),
    }
    if shaped["domain"] == "update":
        # Version currency (Phase 8.1) needs what is installed and what is offered.
        for key in ("installed_version", "latest_version", "title"):
            shaped[key] = attributes.get(key)
    elif shaped["domain"] == "device_tracker":
        ip = attributes.get("ip") or attributes.get("ip_address")
        mac = attributes.get("mac")
        host_name = attributes.get("host_name") or attributes.get("hostname")
        if ip:
            shaped["ip"] = str(ip).strip()
        if mac:
            shaped["mac"] = str(mac).strip().lower()
        if host_name:
            shaped["host_name"] = str(host_name).strip()
        if attributes.get("source_type"):
            shaped["source_type"] = str(attributes["source_type"])
    return shaped


def extract_device_trackers(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalized ``device_tracker.*`` entries carrying ``ip``, ``mac``, or ``host_name``."""
    trackers: list[dict[str, Any]] = []
    for raw in states:
        row = raw if "domain" in raw else parse_state(raw)
        if row.get("domain") != "device_tracker":
            continue
        raw_attrs = raw.get("attributes")
        attrs: dict[str, Any] = raw_attrs if isinstance(raw_attrs, dict) else {}
        ip = row.get("ip") or attrs.get("ip") or attrs.get("ip_address")
        mac = row.get("mac") or attrs.get("mac")
        host_name = (
            row.get("host_name")
            or attrs.get("host_name")
            or attrs.get("hostname")
            or row.get("name")
        )
        if not (ip or mac or host_name):
            continue
        trackers.append(
            {
                "entity_id": row.get("entity_id"),
                "state": row.get("state"),
                "ip": str(ip).strip() if ip else None,
                "mac": str(mac).strip().lower() if mac else None,
                "host_name": str(host_name).strip() if host_name else None,
                "source_type": row.get("source_type") or attrs.get("source_type"),
            }
        )
    return sorted(trackers, key=lambda t: str(t.get("entity_id") or ""))


def summarize_states(states: list[dict[str, Any]]) -> dict[str, int]:
    """Entity counts by domain, sorted by domain name."""
    counts = Counter(s["domain"] for s in states if s.get("domain"))
    return dict(sorted(counts.items()))


def integrations_of_interest(components: list[str]) -> list[str]:
    """The loaded components that name infrastructure the harness models."""
    return sorted(c for c in components if c in INTEGRATIONS_OF_INTEREST)


class HomeAssistantAdapter:
    """Read-only async client for a Home Assistant instance."""

    name = "home-assistant"

    def __init__(
        self,
        config: HomeAssistantConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if config is None and client is None:
            raise HomeAssistantConfigError(
                "HomeAssistantAdapter needs a config or an injected client"
            )
        self.config = config or HomeAssistantConfig(url="http://injected", token="x")
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_env(cls) -> HomeAssistantAdapter:
        return cls(HomeAssistantConfig.from_env())

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self.config.token}",
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

    async def __aenter__(self) -> HomeAssistantAdapter:
        _ = self.client
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str) -> Any:
        response = await self.client.request(method, path)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            detail = response.text.strip()[:300] or response.reason_phrase
            raise HomeAssistantAPIError(response.status_code, detail, method=method, path=path)
        if not response.content:
            return None
        return response.json()

    # ------------------------------------------------------------------ reads

    async def get_config(self) -> dict[str, Any]:
        raw = await self._request("GET", "/api/config") or {}
        return parse_config(raw if isinstance(raw, dict) else {})

    async def list_states(self) -> list[dict[str, Any]]:
        rows = await self._request("GET", "/api/states") or []
        return [parse_state(r) for r in rows if isinstance(r, dict)]

    async def list_service_domains(self) -> list[str]:
        """Service domains the hub exposes (``light``, ``switch``, …), sorted."""
        rows = await self._request("GET", "/api/services") or []
        return sorted(str(r.get("domain")) for r in rows if isinstance(r, dict) and r.get("domain"))

    async def health_check(self) -> tuple[bool, str | None]:
        """Quick reachability/auth probe. Returns ``(ok, error_message)``."""
        try:
            await self._request("GET", "/api/")
        except (HomeAssistantAPIError, httpx.HTTPError) as exc:
            return False, str(exc)
        return True, None


__all__ = [
    "INTEGRATIONS_OF_INTEREST",
    "HomeAssistantAPIError",
    "HomeAssistantAdapter",
    "HomeAssistantConfig",
    "HomeAssistantConfigError",
    "extract_device_trackers",
    "integrations_of_interest",
    "parse_config",
    "parse_state",
    "summarize_states",
]
