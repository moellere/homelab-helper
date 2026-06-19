"""``talos.host`` probe — full node inventory from the Talos machine API.

One probe rather than five: Talos serves every fact from a single API surface,
so splitting identity/cpu/memory/storage/network into separate round-trips
would just multiply ``talosctl`` invocations. The probe pulls a handful of
COSI resources plus two ``/proc`` reads and projects them onto the **canonical
``host.*`` observation keys** the SSH probes already emit — that's what lets
the reconciler build part lineage for Talos nodes with no reconciler change.

Sources:

- ``nodename``           → ``host.identity.hostname``
- ``version`` (server)   → ``host.identity.os_*``, ``host.cpu.architecture``
- ``systeminformation``  → cpu vendor/model, ``host.identity.machine_id``
- ``/proc/cpuinfo``      → ``host.cpu.cores`` / ``threads`` / flags
- ``/proc/meminfo``      → ``host.memory.mem_total_bytes``
- ``disks``              → ``host.storage.devices`` (WWID + symlink serial)
- ``links`` + ``addresses`` → ``host.network.interfaces`` (physical only)

Physical-NIC filtering is *positive* here, unlike the SSH path's name-prefix
heuristic: Talos tags every link with ``kind`` (empty == real device) and a
``busPath``, so virtual constructs (bridge/bond/vxlan/dummy/…) are dropped at
the source and never reach the reconciler's fallback heuristic.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from homelab_helper.adapters.talos import TalosError
from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult

# CPU feature flags worth surfacing for capability assertions, across x86 + ARM
# spellings. Mirrors the spirit of the SSH cpu probe's "interesting" subset.
INTERESTING_CPU_FLAGS: frozenset[str] = frozenset(
    {
        # x86
        "aes",
        "avx",
        "avx2",
        "avx512f",
        "sse4_1",
        "sse4_2",
        "popcnt",
        "pclmulqdq",
        "sha_ni",
        "vmx",
        "svm",
        # ARM
        "asimd",
        "crc32",
        "pmull",
        "sha1",
        "sha2",
        "sha512",
        "sve",
    }
)

# by-id symlink bus prefixes, in order of identity preference.
_BY_ID_PREFIXES: tuple[str, ...] = ("wwn-", "nvme-", "ata-", "scsi-", "usb-")

# talosctl reports UINT32_MAX as the link speed when it can't read one.
_SPEED_UNKNOWN: int = 4_294_967_295


def parse_cpuinfo(content: str) -> dict[str, Any]:
    """Pull core count and feature flags out of ``/proc/cpuinfo``.

    Works for both x86 (``flags:``) and ARM (``Features:``) layouts. Core count
    is the number of ``processor`` records.
    """
    cores = 0
    flags: list[str] = []
    for raw in content.splitlines():
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip().lower()
        if key == "processor":
            cores += 1
        elif key in ("flags", "features") and not flags:
            flags = value.split()
    return {"cores": cores, "flags": flags}


def parse_meminfo_total_bytes(content: str) -> int | None:
    """Return ``MemTotal`` from ``/proc/meminfo`` in bytes (the kB value times 1024)."""
    for raw in content.splitlines():
        if raw.startswith("MemTotal:"):
            fields = raw.split()
            kb = fields[1] if len(fields) > 1 else ""
            if kb.isdigit():
                return int(kb) * 1024
    return None


def _disk_serial_from_symlinks(symlinks: list[str]) -> str | None:
    """Best-effort unique serial from ``/dev/disk/by-id`` symlinks.

    USB/SATA enclosures frequently forge the WWID (e.g. the Pi SSDs all report
    ``naa.5000000000000099``) while the by-id symlink still carries the real
    per-drive serial. Format is ``<bus>-<MODEL>_<SERIAL>`` with an optional
    ``-0:0`` USB suffix; take the trailing ``_``-delimited token.
    """
    for prefix in _BY_ID_PREFIXES:
        for link in symlinks:
            base = link.rsplit("/", 1)[-1]
            if not base.startswith(prefix) or ".part" in base or "-part" in base:
                continue
            token = base[len(prefix) :].split("-", 1)[0]  # drop "-0:0" USB tail
            return token.rsplit("_", 1)[-1] if "_" in token else token or None
    return None


def _disk_doc_to_device(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Talos ``disks`` resource doc to a canonical storage-device dict.

    Returns ``None`` for non-disks (loop/ram/cdrom/virtual) so they never reach
    the storage lineage. The output dict matches the keys
    ``Reconciler._reconcile_storage_lineage`` reads.
    """
    name = doc.get("metadata", {}).get("id")
    spec = doc.get("spec") or {}
    if not isinstance(name, str) or not name:
        return None
    if name.startswith(("loop", "ram", "zram", "md")) or spec.get("cdrom"):
        return None
    if spec.get("bus_path") in (None, "", "/virtual"):
        return None
    symlinks = spec.get("symlinks")
    serial = _disk_serial_from_symlinks(symlinks) if isinstance(symlinks, list) else None
    return {
        "name": name,
        "type": "disk",
        "size_bytes": spec.get("size"),
        "model": (spec.get("model") or "").strip() or None,
        "serial": serial,
        "wwn": (spec.get("wwid") or "").strip() or None,
        "vendor": None,
        "rotational": None,  # Talos disks resource doesn't expose rota
        "transport": (spec.get("transport") or "").strip() or None,
        "readonly": spec.get("readonly"),
    }


def _link_is_physical(spec: dict[str, Any]) -> bool:
    """Positive physical-NIC test: empty ``kind``, ether type, real bus path."""
    if spec.get("type") != "ether":
        return False
    if spec.get("kind"):  # bond/bridge/vxlan/dummy/… all set a non-empty kind
        return False
    bus = spec.get("busPath")
    return isinstance(bus, str) and bus not in ("", "N/A")


def _build_interfaces(
    links: list[dict[str, Any]], addresses: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Canonical ``host.network.interfaces`` list from links + addresses."""
    addrs_by_link: dict[str, list[dict[str, Any]]] = {}
    for doc in addresses:
        spec = doc.get("spec") or {}
        link_name = spec.get("linkName")
        cidr = spec.get("address")
        if not isinstance(link_name, str) or not isinstance(cidr, str) or "/" not in cidr:
            continue
        ip, _, prefix = cidr.partition("/")
        family = "inet6" if spec.get("family") in ("inet6", "AF_INET6") else "inet"
        addrs_by_link.setdefault(link_name, []).append(
            {"family": family, "ip": ip, "prefix": int(prefix) if prefix.isdigit() else None}
        )

    interfaces: list[dict[str, Any]] = []
    for doc in links:
        name = doc.get("metadata", {}).get("id")
        spec = doc.get("spec") or {}
        if not isinstance(name, str) or not _link_is_physical(spec):
            continue
        mac = spec.get("hardwareAddr") or spec.get("permanentAddr") or None
        speed = spec.get("speedMbit")
        interfaces.append(
            {
                "name": name,
                "mac": mac,
                "mtu": spec.get("mtu"),
                "operstate": (spec.get("operationalState") or "").upper() or None,
                "link_type": "ether",
                "master": None,
                # 4294967295 (UINT32_MAX) is talosctl's "unknown speed" sentinel.
                "speed_mbps": speed if isinstance(speed, int) and speed < _SPEED_UNKNOWN else None,
                "addresses": addrs_by_link.get(name, []),
            }
        )
    return interfaces


class TalosHostOutput(BaseModel):
    """Structured output of the talos.host probe (for schema validation)."""

    hostname: str
    arch: str | None = None
    talos_version: str | None = None
    cpu_cores: int | None = None
    mem_total_bytes: int | None = None
    disk_count: int = 0
    interface_count: int = 0


class TalosHostProbe(Probe):
    name: ClassVar[str] = "talos.host"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.API
    target_kinds: ClassVar[list[str]] = ["talos"]
    produces_keys: ClassVar[list[str]] = [
        "host.identity.hostname",
        "host.identity.machine_id",
        "host.identity.os_id",
        "host.identity.os_pretty_name",
        "host.cpu.architecture",
        "host.cpu.model",
        "host.cpu.vendor",
        "host.cpu.cores",
        "host.cpu.threads",
        "host.cpu.flags",
        "host.cpu.interesting_flags",
        "host.memory.mem_total_bytes",
        "host.storage.devices",
        "host.storage.disk_count",
        "host.network.interfaces",
        "host.network.interface_count",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = TalosHostOutput
    description: ClassVar[str | None] = (
        "Inventory a Talos Linux node via the machine API (talosctl): identity, "
        "CPU, memory, storage, and physical NICs — emitted on the canonical host.* keys."
    )

    async def run(self, ctx: ProbeContext) -> ProbeResult:  # noqa: PLR0911
        target = ctx.target
        if target.kind != "talos":
            return ProbeResult(success=False, error=f"unsupported target kind: {target.kind!r}")
        node = target.primary_ip or target.hostname
        if not node:
            return ProbeResult(success=False, error="target has neither hostname nor primary_ip")
        if not ctx.adapters.has("talos"):
            return ProbeResult(success=False, error="talos adapter is not registered")

        adapter = ctx.adapters.get("talos")
        try:
            nodename = await adapter.get_resources(node, "nodename")
            sysinfo = await adapter.get_resources(node, "systeminformation")
            disks = await adapter.get_resources(node, "disks")
            links = await adapter.get_resources(node, "links")
            addresses = await adapter.get_resources(node, "addresses")
            ver = await adapter.version(node)
            cpuinfo_raw = await adapter.read_file(node, "/proc/cpuinfo")
            meminfo_raw = await adapter.read_file(node, "/proc/meminfo")
        except TalosError as exc:
            return ProbeResult(success=False, error=f"talos API failure: {exc}")
        except Exception as exc:  # runner/parse surprises become a probe failure, not a crash
            return ProbeResult(success=False, error=f"talos probe error: {exc}")

        def _first_spec(docs: list[dict[str, Any]]) -> dict[str, Any]:
            return (docs[0].get("spec") or {}) if docs else {}

        nodename_spec = _first_spec(nodename)
        sysinfo_spec = _first_spec(sysinfo)
        hostname = nodename_spec.get("nodename") or target.hostname
        if not hostname:
            return ProbeResult(success=False, error="could not read nodename from Talos")

        cpu = parse_cpuinfo(cpuinfo_raw)
        flags = cpu["flags"]
        interesting = [f for f in flags if f in INTERESTING_CPU_FLAGS]
        mem_total = parse_meminfo_total_bytes(meminfo_raw)
        devices = [d for d in (_disk_doc_to_device(doc) for doc in disks) if d is not None]
        interfaces = _build_interfaces(links, addresses)

        tag = ver.get("tag")
        structured = TalosHostOutput(
            hostname=hostname,
            arch=ver.get("arch"),
            talos_version=tag,
            cpu_cores=cpu["cores"] or None,
            mem_total_bytes=mem_total,
            disk_count=len(devices),
            interface_count=len(interfaces),
        )

        target_id = target.host_id or hostname
        pairs: list[tuple[str, Any]] = [
            ("host.identity.hostname", hostname),
            ("host.identity.machine_id", sysinfo_spec.get("uuid") or None),
            ("host.identity.os_id", "talos"),
            ("host.identity.os_pretty_name", f"Talos Linux {tag}" if tag else "Talos Linux"),
            ("host.cpu.architecture", ver.get("arch")),
            ("host.cpu.model", (sysinfo_spec.get("productName") or "").strip() or None),
            ("host.cpu.vendor", (sysinfo_spec.get("manufacturer") or "").strip() or None),
            ("host.cpu.cores", cpu["cores"] or None),
            ("host.cpu.threads", cpu["cores"] or None),
            ("host.cpu.flags", flags or None),
            ("host.cpu.interesting_flags", interesting or None),
            ("host.memory.mem_total_bytes", mem_total),
            ("host.storage.devices", devices),
            ("host.storage.disk_count", len(devices)),
            ("host.network.interfaces", interfaces),
            ("host.network.interface_count", len(interfaces)),
        ]
        observations = [
            ObservationData(
                key=key, value=value, target_type=IntentTargetType.HOST, target_id=target_id
            )
            for key, value in pairs
            if value is not None
        ]

        return ProbeResult(
            observations=observations, success=True, raw_payload=structured.model_dump()
        )


__all__ = [
    "INTERESTING_CPU_FLAGS",
    "TalosHostOutput",
    "TalosHostProbe",
    "parse_cpuinfo",
    "parse_meminfo_total_bytes",
]
