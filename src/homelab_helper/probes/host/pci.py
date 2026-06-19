"""``host.pci`` probe — PCI device enumeration via ``lspci -vmmnn``.

Machine-readable ``lspci`` (``-vmmnn``: blank-line-separated key/value blocks,
with numeric ``[code]`` suffixes) parsed into one dict per device. Runs as any
user — the class/vendor/device identity fields don't need root. The shared
:func:`parse_lspci` + :func:`split_code` are reused by the ``host.gpu`` probe.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult


def split_code(value: str) -> tuple[str, str | None]:
    """``"VGA compatible controller [0300]"`` -> ``("VGA compatible controller", "0300")``.

    The code is the *last* bracket group — device names can contain their own
    brackets (e.g. ``"Coffee Lake-U GT2 [UHD Graphics 620] [3ea9]"``).
    """
    v = value.strip()
    if v.endswith("]") and " [" in v:
        name, _, code = v.rpartition(" [")
        return name.strip(), code[:-1].strip() or None
    return v, None


def parse_lspci(content: str) -> list[dict[str, Any]]:
    """Parse ``lspci -vmmnn`` output into a list of device dicts."""
    devices: list[dict[str, Any]] = []
    block: dict[str, str] = {}

    def flush() -> None:
        if not block.get("Slot"):
            return
        class_name, class_code = split_code(block.get("Class", ""))
        vendor, vendor_id = split_code(block.get("Vendor", ""))
        device, device_id = split_code(block.get("Device", ""))
        devices.append(
            {
                "slot": block.get("Slot"),
                "class_name": class_name or None,
                "class_code": class_code,
                "vendor": vendor or None,
                "vendor_id": vendor_id,
                "device": device or None,
                "device_id": device_id,
                "rev": block.get("Rev"),
            }
        )

    for raw in content.splitlines():
        if not raw.strip():
            flush()
            block = {}
            continue
        key, sep, val = raw.partition(":")
        if sep:
            block[key.strip()] = val.strip()
    flush()
    return devices


class HostPciOutput(BaseModel):
    devices: list[dict[str, Any]]
    device_count: int


class HostPciProbe(Probe):
    name: ClassVar[str] = "host.pci"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = ["host.pci.devices", "host.pci.device_count"]
    output_schema: ClassVar[type[BaseModel] | None] = HostPciOutput
    description: ClassVar[str | None] = (
        "PCI device inventory via lspci: slot, class, vendor, device, and their numeric ids."
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

        devices = parse_lspci(res.stdout)
        if not devices:
            # lspci absent or no PCI bus (VM/container) — skip cleanly.
            return ProbeResult(success=True, observations=[])

        target_id = target.host_id or target.hostname or "unknown"
        observations = [
            ObservationData(
                key=key, value=value, target_type=IntentTargetType.HOST, target_id=target_id
            )
            for key, value in (
                ("host.pci.devices", devices),
                ("host.pci.device_count", len(devices)),
            )
        ]
        return ProbeResult(
            observations=observations,
            success=True,
            raw_payload=HostPciOutput(devices=devices, device_count=len(devices)).model_dump(),
        )


__all__ = ["HostPciOutput", "HostPciProbe", "parse_lspci", "split_code"]
