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

Also landed (Phase 3 virtualization slice):

- Cluster + VirtualMachine CRUD (list/get-by-name/create/update).
- ``sync_cluster_vms`` — upsert discovered (Proxmox-shaped) VMs into an existing
  NetBox cluster; create+update only, never reaps operator VMs.

Deferred to follow-up slices:

- Interfaces / IPs / VLANs / Prefixes / Services CRUD
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


@dataclass(frozen=True)
class PartPlacement:
    """Caller-supplied view of one current ``Placement`` joined with its part.

    The adapter never queries the harness DB; the CLI assembles these from
    ``Placement.to_date IS NULL`` rows. Fields that don't apply for a given
    part kind (e.g. ``capacity_bytes`` on a NIC) are simply ``None``.
    """

    slot: str
    part_kind: str  # "dimm" | "ssd" | "hdd" | "nvme" | "nic" | "other"
    serial: str | None = None
    wwid: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    capacity_bytes: int | None = None


@dataclass
class SyncInventoryResult:
    device_id: int
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.created) + len(self.updated) + len(self.deleted)


@dataclass
class SyncVMResult:
    cluster_name: str
    found: bool = True
    reason: str | None = None
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.created) + len(self.updated)


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

    # ----------------------------------------------------------- inventory items

    async def list_inventory_items(
        self, device_id: int, *, discovered_only: bool = True
    ) -> list[dict[str, Any]]:
        """Return all InventoryItems on ``device_id``.

        ``discovered_only`` filters to items the harness created (so a human-
        edited inventory item isn't accidentally diffed away by sync).
        """
        params: dict[str, Any] = {"device_id": device_id, "limit": 100}
        if discovered_only:
            params["discovered"] = "true"
        return await self._paginate("/api/dcim/inventory-items/", params=params)

    async def create_inventory_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._request("POST", "/api/dcim/inventory-items/", json=payload),
        )

    async def update_inventory_item(self, item_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._request("PATCH", f"/api/dcim/inventory-items/{item_id}/", json=patch),
        )

    async def delete_inventory_item(self, item_id: int) -> None:
        await self._request("DELETE", f"/api/dcim/inventory-items/{item_id}/")

    async def sync_inventory_items(
        self,
        device_id: int,
        placements: list[PartPlacement],
        *,
        dry_run: bool = False,
    ) -> SyncInventoryResult:
        """Diff ``placements`` against the Device's current discovered items.

        The slot label is the diff key (unique within a Device, stable across
        runs). Items missing from ``placements`` are deleted; new placements
        are created; matching slots whose fields differ are PATCHed.

        Only touches items with ``discovered=True`` — human-entered items are
        invisible to this sync, so an operator's manual InventoryItem doesn't
        get reaped by a probe run.
        """
        result = SyncInventoryResult(device_id=device_id)
        desired: dict[str, dict[str, Any]] = {}
        for p in placements:
            desired[p.slot] = _build_inventory_payload(p, device_id)

        existing_rows = await self.list_inventory_items(device_id, discovered_only=True)
        existing: dict[str, dict[str, Any]] = {
            row["name"]: row for row in existing_rows if isinstance(row.get("name"), str)
        }

        # Create + update pass.
        for slot, payload in desired.items():
            existing_row = existing.get(slot)
            if existing_row is None:
                if not dry_run:
                    await self.create_inventory_item(payload)
                result.created.append(slot)
                continue
            if _inventory_matches(existing_row, payload):
                result.unchanged.append(slot)
                continue
            if not dry_run:
                await self.update_inventory_item(existing_row["id"], payload)
            result.updated.append(slot)

        # Delete pass — anything left in `existing` that the harness no longer
        # claims gets removed.
        for slot, row in existing.items():
            if slot in desired:
                continue
            if not dry_run:
                await self.delete_inventory_item(row["id"])
            result.deleted.append(slot)

        return result

    # -------------------------------------------------- virtualization CRUD

    async def list_clusters(self, *, name: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 100}
        if name is not None:
            params["name"] = name
        return await self._paginate("/api/virtualization/clusters/", params=params)

    async def get_cluster_by_name(self, name: str) -> dict[str, Any] | None:
        rows = await self.list_clusters(name=name)
        return next((r for r in rows if r.get("name") == name), None)

    async def create_cluster(self, payload: dict[str, Any]) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._request("POST", "/api/virtualization/clusters/", json=payload),
        )

    async def update_cluster(self, cluster_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._request("PATCH", f"/api/virtualization/clusters/{cluster_id}/", json=patch),
        )

    async def list_virtual_machines(self, *, cluster_id: int) -> list[dict[str, Any]]:
        return await self._paginate(
            "/api/virtualization/virtual-machines/",
            params={"cluster_id": cluster_id, "limit": 100},
        )

    async def create_virtual_machine(self, payload: dict[str, Any]) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._request("POST", "/api/virtualization/virtual-machines/", json=payload),
        )

    async def update_virtual_machine(self, vm_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._request(
                "PATCH", f"/api/virtualization/virtual-machines/{vm_id}/", json=patch
            ),
        )

    async def sync_cluster_vms(
        self,
        cluster_name: str,
        vms: list[dict[str, Any]],
        *,
        dry_run: bool = False,
    ) -> SyncVMResult:
        """Upsert discovered VMs/LXCs into an existing NetBox cluster.

        NetBox owns cluster topology, so the cluster must already exist — like
        ``sync_host`` won't create Devices, this won't create Clusters; a missing
        cluster returns ``found=False`` with a clear reason. VMs are matched by
        name within the cluster. **Create + update only** — this never deletes,
        so an operator-entered VM is never reaped (a ``discovered`` marker for
        safe reaping is a follow-up).
        """
        result = SyncVMResult(cluster_name=cluster_name)
        cluster = await self.get_cluster_by_name(cluster_name)
        if cluster is None:
            result.found = False
            result.reason = f"cluster {cluster_name!r} not found in NetBox — create it, then re-run"
            return result
        cluster_id = cluster["id"]

        existing_rows = await self.list_virtual_machines(cluster_id=cluster_id)
        existing = {r["name"]: r for r in existing_rows if isinstance(r.get("name"), str)}

        for vm in vms:
            name = vm.get("name")
            if not isinstance(name, str) or not name:
                continue
            payload = _vm_to_netbox_payload(vm, cluster_id)
            row = existing.get(name)
            if row is None:
                if not dry_run:
                    await self.create_virtual_machine(payload)
                result.created.append(name)
            elif _vm_matches(row, payload):
                result.unchanged.append(name)
            else:
                if not dry_run:
                    await self.update_virtual_machine(row["id"], payload)
                result.updated.append(name)
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


# Compared fields when deciding whether an existing InventoryItem matches what
# we would send. NetBox echoes some fields as nested objects (manufacturer is
# {"id": N, "name": "X"} on GET, int on POST), so the comparator normalizes.
_INVENTORY_COMPARED_FIELDS: tuple[str, ...] = (
    "name",
    "part_id",
    "serial",
    "description",
    "discovered",
)


def _build_inventory_payload(p: PartPlacement, device_id: int) -> dict[str, Any]:
    """Build the InventoryItem JSON body for one placement.

    Field choices:

    - ``name`` is the slot label (``DIMM_A1`` / ``/dev/nvme0n1`` / ``enp1s0``).
      Unique within Device; serves as the diff key.
    - ``serial`` is the harness's identity-of-record for the part.
    - ``part_id`` carries manufacturer + model as a single human-readable
      string, since we don't bind to NetBox's Manufacturer FK in this slice
      (that needs a Manufacturer lookup/create dance — follow-up).
    - ``description`` carries the kind + capacity so the inventory tab in
      NetBox is at-a-glance useful.
    - ``discovered: True`` marks this as harness-owned so future sync runs
      won't touch human-entered items.
    """
    parts: list[str] = []
    if p.manufacturer:
        parts.append(p.manufacturer)
    if p.model:
        parts.append(p.model)
    part_id = " ".join(parts) if parts else ""
    # NetBox caps part_id at 50 chars; truncate cleanly so the diff is stable.
    if len(part_id) > _NETBOX_PART_ID_MAX:
        part_id = part_id[: _NETBOX_PART_ID_MAX - 1] + "…"

    serial = (p.serial or p.wwid or "") or ""
    if len(serial) > _NETBOX_SERIAL_MAX:
        serial = serial[: _NETBOX_SERIAL_MAX - 1] + "…"

    description_bits: list[str] = [p.part_kind.upper()]
    if p.capacity_bytes is not None:
        description_bits.append(_format_bytes(p.capacity_bytes))
    description = " ".join(description_bits)

    return {
        "device": device_id,
        "name": p.slot,
        "part_id": part_id,
        "serial": serial,
        "description": description,
        "discovered": True,
    }


def _inventory_matches(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    """True when every compared field already matches what we'd PATCH to."""
    for key in _INVENTORY_COMPARED_FIELDS:
        if (existing.get(key) or "") != (desired.get(key) or ""):
            return False
    return True


_VM_COMPARED_FIELDS = ("status", "vcpus", "memory")
_BYTES_PER_MB = 1024 * 1024


def _vm_to_netbox_payload(vm: dict[str, Any], cluster_id: int) -> dict[str, Any]:
    """Map a discovered (Proxmox-shaped) VM dict onto a NetBox VM payload.

    Proxmox memory is bytes → NetBox ``memory`` is MB; running → ``active``,
    everything else → ``offline``. ``vcpus`` is a float in NetBox's schema.
    """
    status = "active" if vm.get("status") == "running" else "offline"
    payload: dict[str, Any] = {
        "name": vm.get("name"),
        "status": status,
        "cluster": cluster_id,
    }
    maxcpu = vm.get("maxcpu")
    if isinstance(maxcpu, (int, float)) and maxcpu:
        payload["vcpus"] = float(maxcpu)
    maxmem = vm.get("maxmem_bytes")
    if isinstance(maxmem, int) and maxmem:
        payload["memory"] = maxmem // _BYTES_PER_MB
    return payload


def _vm_matches(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    """True when the compared VM fields already match (status may be a dict in NetBox)."""
    for key in _VM_COMPARED_FIELDS:
        want = desired.get(key)
        have = existing.get(key)
        if key == "status" and isinstance(have, dict):
            have = have.get("value")
        if key == "vcpus":
            want = float(want) if want is not None else None
            have = float(have) if have is not None else None
        if (have if have is not None else None) != (want if want is not None else None):
            return False
    return True


def _format_bytes(n: int) -> str:
    for boundary, unit in ((1024**4, "TB"), (1024**3, "GB"), (1024**2, "MB"), (1024, "KB")):
        if n >= boundary:
            return f"{n / boundary:.0f} {unit}"
    return f"{n} B"


_NETBOX_PART_ID_MAX = 50
_NETBOX_SERIAL_MAX = 50


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
    "PartPlacement",
    "SyncHostResult",
    "SyncInventoryResult",
    "SyncVMResult",
]
