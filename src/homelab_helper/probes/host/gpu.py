"""``host.gpu`` probe — GPU detection from the PCI bus.

Derives GPUs from PCI class ``03xx`` (VGA / XGA / 3D / Display controllers)
via the same ``lspci -vmmnn`` parse the ``host.pci`` probe uses. Capability
detail (VRAM, driver) is deferred — this answers "what display/compute
adapters are present and from whom", which is what the planner needs for
placement (e.g. don't schedule transcoding on a host with no GPU).
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult
from homelab_helper.probes.host.pci import parse_lspci

# PCI base class 0x03 = display controllers (VGA 0300, XGA 0301, 3D 0302, other 0380).
_GPU_CLASS_PREFIX = "03"
_MIB = 1024 * 1024
_SYSFS_PARTS = 3
_NVIDIA_CSV_PARTS = 4


def _norm_slot(slot: str) -> str:
    """Normalize ``0000:01:00.0`` or ``00000000:01:00.0`` to ``01:00.0``."""
    parts = slot.strip().lower().split(":")
    if len(parts) >= _SYSFS_PARTS:
        return ":".join(parts[-2:])
    return slot.strip().lower()


def parse_gpu_telemetry(
    sysfs_output: str = "", nvidia_output: str = ""
) -> dict[str, dict[str, Any]]:
    """Parse sysfs DRM / ``nvidia-smi`` output into ``{slot: {"driver", "vram_mb", "busy"}}``."""
    out: dict[str, dict[str, Any]] = {}
    for raw in sysfs_output.splitlines():
        line = raw.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < _SYSFS_PARTS or not parts[0]:
            continue
        slot = _norm_slot(parts[0])
        entry = out.setdefault(slot, {})
        if parts[1]:
            entry["driver"] = parts[1]
        if parts[2].isdigit() and int(parts[2]) > 0:
            entry["vram_mb"] = int(parts[2]) // _MIB
        if len(parts) > _SYSFS_PARTS and parts[3] in ("1", "busy", "true"):
            entry["busy"] = True

    for raw in nvidia_output.splitlines():
        line = raw.strip()
        if not line or "," not in line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < _NVIDIA_CSV_PARTS or not parts[0]:
            continue
        slot = _norm_slot(parts[0])
        entry = out.setdefault(slot, {})
        entry.setdefault("driver", "nvidia")
        if parts[1].isdigit() and int(parts[1]) > 0:
            entry["vram_mb"] = int(parts[1])
        used_mb = int(parts[2]) if parts[2].isdigit() else 0
        util_pct = int(parts[3]) if parts[3].isdigit() else 0
        if util_pct > 0 or used_mb >= 256:  # noqa: PLR2004
            entry["busy"] = True
        else:
            entry.setdefault("busy", False)
    return out


def select_gpus(
    pci_devices: list[dict[str, Any]],
    telemetry: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Filter parsed lspci devices down to display controllers (class 03xx)."""
    extra_by_slot = {_norm_slot(k): v for k, v in (telemetry or {}).items()}
    gpus: list[dict[str, Any]] = []
    for d in pci_devices:
        code = d.get("class_code")
        if isinstance(code, str) and code.startswith(_GPU_CLASS_PREFIX):
            slot = str(d.get("slot") or "")
            item: dict[str, Any] = {
                "slot": d.get("slot"),
                "class_name": d.get("class_name"),
                "vendor": d.get("vendor"),
                "vendor_id": d.get("vendor_id"),
                "device": d.get("device"),
                "device_id": d.get("device_id"),
            }
            t = extra_by_slot.get(_norm_slot(slot), {})
            driver = d.get("driver") or t.get("driver")
            if driver:
                item["driver"] = driver
            if t.get("vram_mb") is not None:
                item["vram_mb"] = t["vram_mb"]
            if t.get("busy") is not None:
                item["busy"] = bool(t["busy"])
            gpus.append(item)
    return gpus


class HostGpuOutput(BaseModel):
    devices: list[dict[str, Any]]
    count: int
    vendors: list[str]
    drivers: list[str] = []
    vram_mb: int | None = None
    busy: bool = False


class HostGpuProbe(Probe):
    name: ClassVar[str] = "host.gpu"
    version: ClassVar[str] = "0.2.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = [
        "host.gpu.devices",
        "host.gpu.count",
        "host.gpu.vendors",
        "host.gpu.drivers",
        "host.gpu.vram_mb",
        "host.gpu.busy",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = HostGpuOutput
    description: ClassVar[str | None] = (
        "GPU/display-controller detection from PCI class 03xx via lspci, "
        "enriched with bound driver, VRAM size, and active compute status."
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

        try:
            async with adapter.session(
                connect_host,
                user=target.ssh_user,
                key_path=target.ssh_key_path,
                password=target.ssh_password,
                port=target.ssh_port,
            ) as ssh:
                res = await ssh.run("lspci -vmmnn -k")
                nv_res = await ssh.run(
                    "nvidia-smi --query-gpu=pci.bus_id,memory.total,memory.used,utilization.gpu "
                    "--format=csv,noheader,nounits 2>/dev/null || true"
                )
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")

        telemetry = parse_gpu_telemetry(nvidia_output=nv_res.stdout if nv_res.ok else "")
        gpus = select_gpus(parse_lspci(res.stdout), telemetry)
        if not gpus:
            # No PCI bus, no lspci, or genuinely headless — nothing to report.
            return ProbeResult(success=True, observations=[])

        vendors = sorted({g["vendor"] for g in gpus if g.get("vendor")})
        drivers = sorted({str(g["driver"]) for g in gpus if g.get("driver")})
        vram_values = [int(g["vram_mb"]) for g in gpus if isinstance(g.get("vram_mb"), int)]
        vram_mb = sum(vram_values) if vram_values else None
        busy = any(bool(g.get("busy")) for g in gpus)
        target_id = target.host_id or target.hostname or "unknown"
        pairs: list[tuple[str, Any]] = [
            ("host.gpu.devices", gpus),
            ("host.gpu.count", len(gpus)),
            ("host.gpu.vendors", vendors),
        ]
        if drivers:
            pairs.append(("host.gpu.drivers", drivers))
        if vram_mb is not None:
            pairs.append(("host.gpu.vram_mb", vram_mb))
        if any("busy" in g for g in gpus):
            pairs.append(("host.gpu.busy", busy))
        observations = [
            ObservationData(
                key=key, value=value, target_type=IntentTargetType.HOST, target_id=target_id
            )
            for key, value in pairs
        ]
        return ProbeResult(
            observations=observations,
            success=True,
            raw_payload=HostGpuOutput(
                devices=gpus,
                count=len(gpus),
                vendors=vendors,
                drivers=drivers,
                vram_mb=vram_mb,
                busy=busy,
            ).model_dump(),
        )


__all__ = ["HostGpuOutput", "HostGpuProbe", "parse_gpu_telemetry", "select_gpus"]
