"""NetBox adapter — read+write subset for the harness's Slice 1 needs.

This adapter is the bridge between the harness DB and the operator's NetBox
instance. Per the schema doc's NetBox-sync invariants, the harness owns a
specific set of *custom fields* on NetBox objects (capabilities, discovery
state, power policy, arch). NetBox owns the canonical inventory facts
(site, role, primary IP, status); the adapter never overwrites those.

Scope landed today:

- Device CRUD: list / get-by-name / update.
- Custom-field bootstrap: idempotent ensure-exists for the ~10 Device CFs
  the harness needs. Run once per NetBox instance before sync.
- ``sync_host`` — push a harness ``Host`` row's CF values to its NetBox
  Device. Matches Device by hostname. **Won't create Devices** — operator
  owns Device creation; missing Device returns ``Synced(found=False)``.

Deferred to follow-up slices:

- Interfaces / IPs / VLANs / Prefixes / Clusters / VMs / Services / InventoryItems
- Reconciler-driven write path (placement mirroring)
- ``NETBOX_DIVERGENCE`` finding when hand-edits collide with a planned write

Configuration via two env vars, both required for live use:

- ``HOMELAB_HELPER_NETBOX_URL``    — e.g. ``https://netbox.example.com``
- ``HOMELAB_HELPER_NETBOX_TOKEN``  — API token

Tests inject an ``httpx.AsyncClient`` with ``MockTransport`` so no live
NetBox is needed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx

_HTTP_ERROR_THRESHOLD = 400

if TYPE_CHECKING:
    from homelab_helper.db.models import Host


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NetBoxConfig:
    url: str
    token: str
    verify_ssl: bool = True
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> NetBoxConfig:
        url = os.environ.get("HOMELAB_HELPER_NETBOX_URL")
        token = os.environ.get("HOMELAB_HELPER_NETBOX_TOKEN")
        if not url or not token:
            raise NetBoxConfigError(
                "NetBox URL and token are required. Set HOMELAB_HELPER_NETBOX_URL "
                "and HOMELAB_HELPER_NETBOX_TOKEN."
            )
        verify = os.environ.get("HOMELAB_HELPER_NETBOX_VERIFY_SSL", "true").lower() not in {
            "0",
            "false",
            "no",
        }
        return cls(url=url, token=token, verify_ssl=verify)


class NetBoxConfigError(RuntimeError):
    """Missing required NetBox configuration."""


class NetBoxAPIError(RuntimeError):
    """A NetBox API call returned a non-2xx response."""

    def __init__(self, status_code: int, detail: str, *, method: str, path: str) -> None:
        super().__init__(f"NetBox API {method} {path} -> {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.method = method
        self.path = path


# ---------------------------------------------------------------------------
# Custom-field specs the harness needs on Device. Mirrors the walkthrough's
# "Custom fields to add on day one" list. Type names match NetBox's
# ``customfield_type`` enum.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomFieldSpec:
    name: str
    label: str
    description: str
    type: str  # "text" | "longtext" | "integer" | "decimal" | "boolean" | "date" | "datetime" | "json" | "select"
    choices: tuple[str, ...] = ()
    # NetBox object types the CF applies to (e.g. "dcim.device").
    object_types: tuple[str, ...] = ("dcim.device",)


_DEVICE_CFS: tuple[CustomFieldSpec, ...] = (
    CustomFieldSpec(
        name="cf_power_policy",
        label="Power policy",
        description="Operator-declared power policy.",
        type="select",
        choices=("always-on", "wol-on-demand", "manual"),
    ),
    CustomFieldSpec(
        name="cf_expected_power_state",
        label="Expected power state",
        description="Derived: what state the device should be in right now.",
        type="select",
        choices=("on", "off", "either"),
    ),
    CustomFieldSpec(
        name="cf_discovery_source",
        label="Discovery source",
        description="How this Device was first discovered.",
        type="text",
    ),
    CustomFieldSpec(
        name="cf_discovery_last_run",
        label="Discovery last run",
        description="UTC datetime the harness last ran discovery against this host.",
        type="datetime",
    ),
    CustomFieldSpec(
        name="cf_last_verified",
        label="Last verified",
        description="Date the operator last hand-confirmed this Device's facts.",
        type="date",
    ),
    CustomFieldSpec(
        name="cf_capabilities",
        label="Capabilities",
        description="JSON capability bag the harness owns (CPU model, mem totals, etc.).",
        type="json",
    ),
    CustomFieldSpec(
        name="cf_arch",
        label="Architecture",
        description="CPU architecture.",
        type="select",
        choices=("amd64", "arm64", "arm", "other"),
    ),
    CustomFieldSpec(
        name="cf_hypervisor_type",
        label="Hypervisor type",
        description="What kind of hypervisor (if any) runs on this Device.",
        type="select",
        choices=(
            "proxmox",
            "esxi",
            "kvm-host",
            "docker-host",
            "bare-metal",
            "talos",
            "none",
        ),
    ),
    CustomFieldSpec(
        name="cf_power_draw_idle_watts",
        label="Idle power draw (W)",
        description="Idle wattage at the wall.",
        type="decimal",
    ),
    CustomFieldSpec(
        name="cf_power_draw_max_watts",
        label="Max power draw (W)",
        description="Peak wattage at the wall.",
        type="decimal",
    ),
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class BootstrapResult:
    created: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.already_present) + len(self.failed)


@dataclass
class SyncHostResult:
    hostname: str
    found: bool
    device_id: int | None = None
    patch: dict[str, Any] = field(default_factory=dict)
    skipped_reason: str | None = None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class NetBoxAdapter:
    """Thin async wrapper over NetBox's REST API for the harness's CF surface."""

    name: str = "netbox"

    def __init__(
        self,
        config: NetBoxConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = client
        self._owns_client = client is None

    @classmethod
    def from_env(cls) -> NetBoxAdapter:
        return cls(NetBoxConfig.from_env())

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.url.rstrip("/"),
            headers={
                "Authorization": f"Token {self.config.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
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

    async def __aenter__(self) -> NetBoxAdapter:
        # Ensure client exists.
        _ = self.client
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ HTTP

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        response = await self.client.request(method, path, params=params, json=json)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            detail = _extract_detail(response)
            raise NetBoxAPIError(response.status_code, detail, method=method, path=path)
        if not response.content:
            return None
        return cast("Any", response.json())

    async def _paginate(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        url: str | None = path
        first_params = dict(params or {})
        while url is not None:
            payload = await self._request("GET", url, params=first_params)
            first_params = {}  # NetBox echoes the cursor in `next`
            results.extend(payload.get("results", []))
            next_url = payload.get("next")
            if next_url is None:
                break
            # NetBox returns an absolute URL in ``next``; strip the base prefix.
            url = _strip_base(next_url, self.config.url)
        return results

    # ------------------------------------------------------------------ health

    async def health_check(self) -> dict[str, Any]:
        """Hit ``/api/status/`` — returns NetBox version + plugin info on success."""
        return cast("dict[str, Any]", await self._request("GET", "/api/status/"))

    # ------------------------------------------------------------------ devices

    async def list_devices(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self._paginate("/api/dcim/devices/", params={"limit": limit})

    async def get_device_by_name(self, name: str) -> dict[str, Any] | None:
        payload = await self._request("GET", "/api/dcim/devices/", params={"name": name})
        rows = payload.get("results", [])
        if not rows:
            return None
        # NetBox's ``name`` filter is exact-match but case-insensitive; verify.
        for row in rows:
            if row.get("name") == name:
                return cast("dict[str, Any]", row)
        return cast("dict[str, Any]", rows[0])

    async def update_device(self, device_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._request("PATCH", f"/api/dcim/devices/{device_id}/", json=patch),
        )

    # ----------------------------------------------------------- custom fields

    async def list_custom_fields(self) -> list[dict[str, Any]]:
        return await self._paginate("/api/extras/custom-fields/", params={"limit": 100})

    async def create_custom_field(self, spec: CustomFieldSpec) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": spec.name,
            "label": spec.label,
            "description": spec.description,
            "type": spec.type,
            "object_types": list(spec.object_types),
            "required": False,
        }
        if spec.choices:
            payload["choice_set"] = None
            payload["choices"] = [{"value": c, "label": c} for c in spec.choices]
        return cast(
            "dict[str, Any]",
            await self._request("POST", "/api/extras/custom-fields/", json=payload),
        )

    async def bootstrap_custom_fields(
        self,
        *,
        specs: tuple[CustomFieldSpec, ...] = _DEVICE_CFS,
        dry_run: bool = False,
    ) -> BootstrapResult:
        """Idempotent. Reads existing CFs; creates only the missing ones."""
        result = BootstrapResult()
        existing = {cf.get("name") for cf in await self.list_custom_fields()}
        for spec in specs:
            if spec.name in existing:
                result.already_present.append(spec.name)
                continue
            if dry_run:
                result.created.append(spec.name)
                continue
            try:
                await self.create_custom_field(spec)
            except NetBoxAPIError as exc:
                result.failed.append((spec.name, exc.detail or str(exc)))
            else:
                result.created.append(spec.name)
        return result

    # ----------------------------------------------------------- host sync

    async def sync_host(self, host: Host, *, dry_run: bool = False) -> SyncHostResult:
        """Push a harness Host's harness-owned fields onto its NetBox Device.

        Won't create Devices — operator-owned. Returns ``found=False`` with a
        reason when no matching Device exists; the operator is expected to
        create the Device in NetBox first.
        """
        device = await self.get_device_by_name(host.hostname)
        if device is None:
            return SyncHostResult(
                hostname=host.hostname,
                found=False,
                skipped_reason=f"no NetBox Device named {host.hostname!r}",
            )

        patch = {"custom_fields": _build_host_cf_patch(host)}
        if not dry_run:
            await self.update_device(device["id"], patch)
        return SyncHostResult(
            hostname=host.hostname,
            found=True,
            device_id=device["id"],
            patch=patch,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_host_cf_patch(host: Host) -> dict[str, Any]:
    """Map Host columns onto the cf_* names this adapter knows."""
    patch: dict[str, Any] = {
        "cf_power_policy": host.power_policy.value,
        "cf_expected_power_state": host.expected_power_state.value,
        "cf_discovery_source": host.discovery_source.value,
        "cf_arch": host.arch.value,
        "cf_capabilities": host.capabilities or {},
    }
    if host.discovery_last_run is not None:
        patch["cf_discovery_last_run"] = host.discovery_last_run.isoformat()
    if host.last_verified is not None:
        patch["cf_last_verified"] = host.last_verified.date().isoformat()
    return patch


def _extract_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        if "detail" in payload:
            return str(payload["detail"])
        return ", ".join(f"{k}={v!r}" for k, v in payload.items())[:300]
    return str(payload)[:200]


def _strip_base(url: str, base: str) -> str:
    base = base.rstrip("/")
    if url.startswith(base):
        return url[len(base) :] or "/"
    return url


__all__ = [
    "BootstrapResult",
    "CustomFieldSpec",
    "NetBoxAPIError",
    "NetBoxAdapter",
    "NetBoxConfig",
    "NetBoxConfigError",
    "SyncHostResult",
]
