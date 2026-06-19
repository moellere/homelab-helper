"""``host.smart`` probe — drive health from ``smartctl`` (S.M.A.R.T.).

Complements ``host.storage`` (which enumerates block devices) with the health
layer: per-drive SMART overall status, power-on hours, temperature, and the
reallocated / pending-sector and CRC-error counters that predict failure. Needs
root — ``smartctl`` reads the device directly — so the probe runs ``sudo -n``
when the SSH user isn't already root.

Works on any Linux box with smartmontools, NAS or not. USB-attached drives
(common on homelab NAS builds) report ``type=scsi`` to ``--scan`` but usually
pass full ATA SMART through the bridge, so the per-device ``smartctl -a -j``
auto-detect is used rather than the scan's coarse type.

WWN is emitted in the same ``0x…`` form ``host.storage`` uses, so SMART rows
cross-reference cleanly to the ``PhysicalPart`` the storage lineage built.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult

# ATA SMART attribute IDs we surface (raw value).
_ATTR_REALLOCATED = 5
_ATTR_PENDING = 197
_ATTR_CRC = 199


def parse_smart_scan(content: str) -> list[str]:
    """Device names from ``smartctl --scan -j`` output."""
    names: list[str] = []
    with contextlib.suppress(json.JSONDecodeError):
        payload = json.loads(content)
        for dev in payload.get("devices", []) or []:
            name = dev.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def _format_wwn(wwn: Any) -> str | None:
    """smartctl's ``{naa, oui, id}`` -> ``0x<naa><oui:06x><id:09x>`` (lsblk form)."""
    if not isinstance(wwn, dict):
        return None
    try:
        return f"0x{wwn['naa']:x}{wwn['oui']:06x}{wwn['id']:09x}"
    except (KeyError, ValueError, TypeError):
        return None


def parse_smart_device(content: str) -> dict[str, Any] | None:
    """Summarize one ``smartctl -a -j /dev/X`` document into a flat health dict.

    Tolerant of the scsi/ata/nvme shape differences and of missing fields
    (a USB bridge may not expose every counter). Returns ``None`` only when the
    JSON has no device name at all.
    """
    try:
        d = json.loads(content)
    except json.JSONDecodeError:
        return None
    device = d.get("device") or {}
    name = device.get("name")
    if not isinstance(name, str) or not name:
        return None

    attrs: dict[int, Any] = {}
    for row in d.get("ata_smart_attributes", {}).get("table", []) or []:
        aid = row.get("id")
        if isinstance(aid, int):
            attrs[aid] = (row.get("raw") or {}).get("value")

    nvme = d.get("nvme_smart_health_information_log") or {}
    power_on = d.get("power_on_time") or {}
    temp = d.get("temperature") or {}
    status = d.get("smart_status") or {}

    return {
        "name": name,
        "protocol": device.get("protocol"),
        "model": d.get("model_name") or d.get("scsi_model_name"),
        "serial": d.get("serial_number"),
        "wwn": _format_wwn(d.get("wwn")),
        "health_passed": status.get("passed"),
        "power_on_hours": power_on.get("hours"),
        "temperature_c": temp.get("current"),
        "rotation_rate": d.get("rotation_rate"),
        "reallocated_sectors": attrs.get(_ATTR_REALLOCATED),
        "pending_sectors": attrs.get(_ATTR_PENDING),
        "crc_errors": attrs.get(_ATTR_CRC),
        # NVMe wear indicator (percent of rated endurance consumed).
        "percentage_used": nvme.get("percentage_used"),
    }


def _is_unhealthy(dev: dict[str, Any]) -> bool:
    """A drive is unhealthy if SMART failed or it has reallocated/pending sectors.

    CRC errors (cable/bridge faults) are captured but don't mark the *drive*
    unhealthy — they're a connection signal, not media failure.
    """
    if dev.get("health_passed") is False:
        return True
    for key in ("reallocated_sectors", "pending_sectors"):
        val = dev.get(key)
        if isinstance(val, int) and val > 0:
            return True
    return False


class HostSmartOutput(BaseModel):
    """Structured output of the host.smart probe."""

    devices: list[dict[str, Any]]
    device_count: int
    health_all_passed: bool
    unhealthy_devices: list[str]


class HostSmartProbe(Probe):
    name: ClassVar[str] = "host.smart"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.ROOT
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = [
        "host.smart.devices",
        "host.smart.device_count",
        "host.smart.health_all_passed",
        "host.smart.unhealthy_devices",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = HostSmartOutput
    description: ClassVar[str | None] = (
        "Per-drive S.M.A.R.T. health via smartctl: overall status, power-on hours, "
        "temperature, reallocated/pending sectors, CRC errors, NVMe wear. Needs root."
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
                scan = await ssh.run(f"{sudo}smartctl --scan -j")
                names = parse_smart_scan(scan.stdout)
                if not names:
                    # smartctl absent or no SMART-capable devices — nothing to
                    # report. Skip cleanly so a default fleet run doesn't fail on
                    # hosts without smartmontools or without elevated access.
                    return ProbeResult(success=True, observations=[])
                devices: list[dict[str, Any]] = []
                for name in names:
                    # smartctl uses a bitmask exit status (non-zero even on a
                    # readable healthy drive), so parse stdout regardless of code.
                    res = await ssh.run(f"{sudo}smartctl -a -j {name}")
                    dev = parse_smart_device(res.stdout)
                    if dev is not None:
                        devices.append(dev)
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")

        if not devices:
            return ProbeResult(success=True, observations=[])

        unhealthy = [d["name"] for d in devices if _is_unhealthy(d)]
        all_passed = all(d.get("health_passed") is not False for d in devices)
        structured = HostSmartOutput(
            devices=devices,
            device_count=len(devices),
            health_all_passed=all_passed,
            unhealthy_devices=unhealthy,
        )

        target_id = target.host_id or target.hostname or "unknown"
        observations = [
            ObservationData(
                key=key, value=value, target_type=IntentTargetType.HOST, target_id=target_id
            )
            for key, value in (
                ("host.smart.devices", [dict(d) for d in devices]),
                ("host.smart.device_count", len(devices)),
                ("host.smart.health_all_passed", all_passed),
                ("host.smart.unhealthy_devices", unhealthy),
            )
        ]
        return ProbeResult(
            observations=observations, success=True, raw_payload=structured.model_dump()
        )


__all__ = [
    "HostSmartOutput",
    "HostSmartProbe",
    "parse_smart_device",
    "parse_smart_scan",
]
