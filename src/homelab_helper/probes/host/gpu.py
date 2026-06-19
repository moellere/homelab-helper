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


def select_gpus(pci_devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter parsed lspci devices down to display controllers (class 03xx)."""
    gpus: list[dict[str, Any]] = []
    for d in pci_devices:
        code = d.get("class_code")
        if isinstance(code, str) and code.startswith(_GPU_CLASS_PREFIX):
            gpus.append(
                {
                    "slot": d.get("slot"),
                    "class_name": d.get("class_name"),
                    "vendor": d.get("vendor"),
                    "vendor_id": d.get("vendor_id"),
                    "device": d.get("device"),
                    "device_id": d.get("device_id"),
                }
            )
    return gpus


class HostGpuOutput(BaseModel):
    devices: list[dict[str, Any]]
    count: int
    vendors: list[str]


class HostGpuProbe(Probe):
    name: ClassVar[str] = "host.gpu"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = [
        "host.gpu.devices",
        "host.gpu.count",
        "host.gpu.vendors",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = HostGpuOutput
    description: ClassVar[str | None] = (
        "GPU/display-controller detection from PCI class 03xx via lspci."
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
                res = await ssh.run("lspci -vmmnn")
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")

        gpus = select_gpus(parse_lspci(res.stdout))
        if not gpus:
            # No PCI bus, no lspci, or genuinely headless — nothing to report.
            return ProbeResult(success=True, observations=[])

        vendors = sorted({g["vendor"] for g in gpus if g.get("vendor")})
        target_id = target.host_id or target.hostname or "unknown"
        observations = [
            ObservationData(
                key=key, value=value, target_type=IntentTargetType.HOST, target_id=target_id
            )
            for key, value in (
                ("host.gpu.devices", gpus),
                ("host.gpu.count", len(gpus)),
                ("host.gpu.vendors", vendors),
            )
        ]
        return ProbeResult(
            observations=observations,
            success=True,
            raw_payload=HostGpuOutput(devices=gpus, count=len(gpus), vendors=vendors).model_dump(),
        )


__all__ = ["HostGpuOutput", "HostGpuProbe", "select_gpus"]
