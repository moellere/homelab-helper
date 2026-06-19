"""``host.memory`` probe — RAM totals from ``/proc/meminfo`` + per-DIMM layout.

Two layers:

- Always: RAM/swap/hugepage totals from ``/proc/meminfo`` (works as any user).
- Best-effort: per-DIMM inventory (slot, size, serial, vendor, part number)
  from ``dmidecode -t memory``, which needs root — the probe prefixes
  ``sudo -n`` when the SSH user isn't root and silently skips the DIMM layer if
  dmidecode is unavailable or unauthorized. The emitted ``host.memory.dimms``
  list is exactly the shape the reconciler's DIMM lineage consumes, so a
  successful run populates ``PhysicalPart``/``Placement`` rows per DIMM.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult

_MEMINFO_UNIT_TOKEN_COUNT = 2  # value + unit
_SIZE_UNITS = {"kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}


def _clean_field(value: str | None) -> str | None:
    """Strip dmidecode placeholders to None."""
    if value is None:
        return None
    v = value.strip()
    if not v or v.lower() in ("not specified", "unknown", "none", "no module installed"):
        return None
    return v


def _parse_dimm_size(value: str | None) -> int | None:
    """``"16 GB"`` -> bytes. dmidecode reports binary multiples despite GB/MB."""
    if not value:
        return None
    parts = value.split()
    if len(parts) < _MEMINFO_UNIT_TOKEN_COUNT or not parts[0].isdigit():
        return None
    mult = _SIZE_UNITS.get(parts[1].lower())
    return int(parts[0]) * mult if mult else None


def _parse_speed_mts(value: str | None) -> int | None:
    """``"3200 MT/s"`` -> 3200."""
    if not value:
        return None
    first = value.split()[0] if value.split() else ""
    return int(first) if first.isdigit() else None


def parse_dmidecode_memory(content: str) -> list[dict[str, Any]]:
    """Parse ``dmidecode -t memory`` into the reconciler's DIMM dict shape.

    Each ``Memory Device`` block becomes a dict ``{slot, serial, size_bytes,
    speed_mts, manufacturer, part_number, type}``. Empty slots (``Size: No
    Module Installed``) are dropped — only populated DIMMs are parts.
    """
    blocks: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    in_device = False
    for raw in content.splitlines():
        if not raw.startswith((" ", "\t")):  # non-indented = section header
            if cur is not None:
                blocks.append(cur)
                cur = None
            in_device = raw.strip() == "Memory Device"
            if in_device:
                cur = {}
            continue
        if cur is None:
            continue
        key, sep, val = raw.strip().partition(":")
        if sep:
            cur[key.strip()] = val.strip()
    if cur is not None:
        blocks.append(cur)

    dimms: list[dict[str, Any]] = []
    for b in blocks:
        size = _parse_dimm_size(b.get("Size"))
        if size is None:  # empty slot or unreadable size
            continue
        dimms.append(
            {
                "slot": _clean_field(b.get("Locator")),
                "serial": _clean_field(b.get("Serial Number")),
                "size_bytes": size,
                "speed_mts": _parse_speed_mts(b.get("Speed")),
                "manufacturer": _clean_field(b.get("Manufacturer")),
                "part_number": _clean_field(b.get("Part Number")),
                "type": _clean_field(b.get("Type")),
            }
        )
    return dimms


def parse_meminfo(content: str) -> dict[str, int]:
    """Parse /proc/meminfo. Returns ``{key: bytes}``.

    /proc/meminfo lines look like ``MemTotal:       16363716 kB``. Sizes are
    reported in kibibytes when a unit is given (always ``kB`` in current
    kernels, which actually means KiB despite the lower-case b). Hugepage
    counts are unitless integers.
    """
    out: dict[str, int] = {}
    for line in content.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        rest = rest.strip()
        if not rest:
            continue
        tokens = rest.split()
        try:
            value = int(tokens[0])
        except ValueError:
            continue
        if len(tokens) >= _MEMINFO_UNIT_TOKEN_COUNT and tokens[1].lower() == "kb":
            value *= 1024  # KiB → bytes
        out[key.strip()] = value
    return out


class HostMemoryOutput(BaseModel):
    mem_total_bytes: int | None = None
    mem_available_bytes: int | None = None
    mem_free_bytes: int | None = None
    swap_total_bytes: int | None = None
    swap_free_bytes: int | None = None
    hugepages_total: int | None = None
    hugepages_free: int | None = None
    hugepagesize_bytes: int | None = None


class HostMemoryProbe(Probe):
    name: ClassVar[str] = "host.memory"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = [
        "host.memory.mem_total_bytes",
        "host.memory.mem_available_bytes",
        "host.memory.mem_free_bytes",
        "host.memory.swap_total_bytes",
        "host.memory.swap_free_bytes",
        "host.memory.hugepages_total",
        "host.memory.hugepages_free",
        "host.memory.hugepagesize_bytes",
        "host.memory.dimms",
        "host.memory.dimm_count",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = HostMemoryOutput
    description: ClassVar[str | None] = (
        "RAM/swap totals from /proc/meminfo plus best-effort per-DIMM layout "
        "from dmidecode (root): slot, size, serial, vendor, part number."
    )

    async def run(self, ctx: ProbeContext) -> ProbeResult:  # noqa: PLR0911
        target = ctx.target
        if target.kind != "host":
            return ProbeResult(success=False, error=f"unsupported target kind: {target.kind!r}")
        if not (target.hostname or target.primary_ip):
            return ProbeResult(success=False, error="target has neither hostname nor primary_ip")
        if not target.ssh_user:
            return ProbeResult(success=False, error="ssh_user is required on the target")
        if not ctx.adapters.has("kernel-ssh"):
            return ProbeResult(success=False, error="kernel-ssh adapter is not registered")

        adapter = ctx.adapters.get("kernel-ssh")
        connect_host = target.primary_ip or target.hostname
        assert connect_host
        sudo = "" if target.ssh_user == "root" else "sudo -n "

        try:
            async with adapter.session(
                connect_host,
                user=target.ssh_user,
                key_path=target.ssh_key_path,
                password=target.ssh_password,
                port=target.ssh_port,
            ) as ssh:
                res = await ssh.run("cat /proc/meminfo")
                # Best-effort DIMM layout — root-only; skip cleanly on failure.
                dmi = await ssh.run(f"{sudo}dmidecode -t memory")
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")

        if not res.ok:
            return ProbeResult(
                success=False, error=f"could not read /proc/meminfo (exit {res.exit_code})"
            )

        dimms = parse_dmidecode_memory(dmi.stdout) if dmi.ok else []
        info = parse_meminfo(res.stdout)
        structured = HostMemoryOutput(
            mem_total_bytes=info.get("MemTotal"),
            mem_available_bytes=info.get("MemAvailable"),
            mem_free_bytes=info.get("MemFree"),
            swap_total_bytes=info.get("SwapTotal"),
            swap_free_bytes=info.get("SwapFree"),
            hugepages_total=info.get("HugePages_Total"),
            hugepages_free=info.get("HugePages_Free"),
            hugepagesize_bytes=info.get("Hugepagesize"),
        )

        target_id = target.host_id or target.hostname or "unknown"
        observations: list[ObservationData] = []
        for key, value in (
            ("host.memory.mem_total_bytes", structured.mem_total_bytes),
            ("host.memory.mem_available_bytes", structured.mem_available_bytes),
            ("host.memory.mem_free_bytes", structured.mem_free_bytes),
            ("host.memory.swap_total_bytes", structured.swap_total_bytes),
            ("host.memory.swap_free_bytes", structured.swap_free_bytes),
            ("host.memory.hugepages_total", structured.hugepages_total),
            ("host.memory.hugepages_free", structured.hugepages_free),
            ("host.memory.hugepagesize_bytes", structured.hugepagesize_bytes),
        ):
            if value is None:
                continue
            observations.append(
                ObservationData(
                    key=key,
                    value=value,
                    target_type=IntentTargetType.HOST,
                    target_id=target_id,
                )
            )

        if dimms:
            dimm_obs: tuple[tuple[str, Any], ...] = (
                ("host.memory.dimms", dimms),
                ("host.memory.dimm_count", len(dimms)),
            )
            for dkey, dval in dimm_obs:
                observations.append(
                    ObservationData(
                        key=dkey,
                        value=dval,
                        target_type=IntentTargetType.HOST,
                        target_id=target_id,
                    )
                )

        return ProbeResult(
            observations=observations,
            success=True,
            raw_payload=structured.model_dump(),
        )


__all__ = [
    "HostMemoryOutput",
    "HostMemoryProbe",
    "parse_dmidecode_memory",
    "parse_meminfo",
]
