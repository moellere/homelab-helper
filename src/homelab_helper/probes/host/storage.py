"""``host.storage`` probe — block-device inventory from ``lsblk -J``.

Captures the disk/partition/lvm/md tree, vendor + model + serial + wwn for
identity, plus rotational flag and mountpoints. SMART data is deferred to a
separate probe (needs sudo and per-device dispatch).
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult

# Columns to ask lsblk for. We request more than we currently use so we can
# extend the parser without re-running discovery.
LSBLK_COLUMNS: tuple[str, ...] = (
    "NAME",
    "KNAME",
    "PATH",
    "TYPE",
    "SIZE",
    "FSTYPE",
    "FSAVAIL",
    "MOUNTPOINT",
    "MOUNTPOINTS",
    "MODEL",
    "SERIAL",
    "WWN",
    "VENDOR",
    "REV",
    "ROTA",
    "RM",
    "RO",
    "STATE",
    "TRAN",
    "HCTL",
    "PHY-SEC",
    "LOG-SEC",
    "PKNAME",
)


class BlockDevice(BaseModel):
    """One node in the lsblk tree (disk, partition, lvm, raid, etc.)."""

    name: str
    type: str | None = None  # disk / part / lvm / raid / loop / rom / ...
    size_bytes: int | None = None
    fstype: str | None = None
    mountpoint: str | None = None
    model: str | None = None
    serial: str | None = None
    wwn: str | None = None
    vendor: str | None = None
    rotational: bool | None = None
    removable: bool | None = None
    readonly: bool | None = None
    transport: str | None = None  # sata / nvme / usb / virtio / ...
    parent: str | None = None
    children: list[BlockDevice] = []


def _coerce_int(value: Any) -> int | None:
    """lsblk -b returns SIZE as int; -O without -b returns string ('1.5T')."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _coerce_bool(value: Any) -> bool | None:
    """lsblk emits booleans as either 0/1 ints, '0'/'1' strings, or booleans."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value not in {"0", "false", "False", ""}
    return None


def _node_to_device(node: dict[str, Any], parent: str | None = None) -> BlockDevice:
    mp = node.get("mountpoint")
    if not mp:
        mps = node.get("mountpoints")
        if isinstance(mps, list):
            non_empty = [m for m in mps if m]
            mp = non_empty[0] if non_empty else None
    dev = BlockDevice(
        name=node.get("name", ""),
        type=node.get("type"),
        size_bytes=_coerce_int(node.get("size")),
        fstype=node.get("fstype"),
        mountpoint=mp,
        model=(node.get("model") or "").strip() or None,
        serial=(node.get("serial") or "").strip() or None,
        wwn=(node.get("wwn") or "").strip() or None,
        vendor=(node.get("vendor") or "").strip() or None,
        rotational=_coerce_bool(node.get("rota")),
        removable=_coerce_bool(node.get("rm")),
        readonly=_coerce_bool(node.get("ro")),
        transport=(node.get("tran") or "").strip() or None,
        parent=parent,
        children=[],
    )
    for child in node.get("children", []) or []:
        dev.children.append(_node_to_device(child, parent=dev.name))
    return dev


def parse_lsblk_json(content: str) -> list[BlockDevice]:
    """Top-level disks (plus their child partitions/lvm/raid) from lsblk JSON."""
    devices: list[BlockDevice] = []
    with contextlib.suppress(json.JSONDecodeError):
        payload = json.loads(content)
        blocks = payload.get("blockdevices", []) if isinstance(payload, dict) else []
        for node in blocks:
            devices.append(_node_to_device(node))
    return devices


class HostStorageOutput(BaseModel):
    devices: list[BlockDevice]
    disk_count: int  # top-level type=disk count


class HostStorageProbe(Probe):
    name: ClassVar[str] = "host.storage"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = [
        "host.storage.devices",
        "host.storage.disk_count",
        "host.storage.disk_names",
        "host.storage.total_disk_bytes",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = HostStorageOutput
    description: ClassVar[str | None] = (
        "Block-device tree from lsblk: disks, partitions, LVM, RAID, fstypes, "
        "mountpoints, model/serial/wwn identity, transport, rotational flag."
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

        # -b: byte sizes (no humanized "1.5T"); -J: JSON; -o: explicit columns
        cmd = f"lsblk -b -J -o {','.join(LSBLK_COLUMNS)}"
        try:
            async with adapter.session(
                connect_host,
                user=target.ssh_user,
                key_path=target.ssh_key_path,
                password=target.ssh_password,
                port=target.ssh_port,
            ) as ssh:
                res = await ssh.run(cmd)
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")

        if not res.ok:
            return ProbeResult(
                success=False,
                error=f"lsblk failed (exit {res.exit_code}): {res.stderr.strip()[:200]}",
            )

        devices = parse_lsblk_json(res.stdout)
        disks = [d for d in devices if d.type == "disk"]
        total_disk_bytes = sum(d.size_bytes or 0 for d in disks)
        structured = HostStorageOutput(devices=devices, disk_count=len(disks))

        target_id = target.host_id or target.hostname or "unknown"
        observations: list[ObservationData] = [
            ObservationData(
                key="host.storage.disk_count",
                value=len(disks),
                target_type=IntentTargetType.HOST,
                target_id=target_id,
            ),
            ObservationData(
                key="host.storage.disk_names",
                value=[d.name for d in disks],
                target_type=IntentTargetType.HOST,
                target_id=target_id,
            ),
            ObservationData(
                key="host.storage.total_disk_bytes",
                value=total_disk_bytes,
                target_type=IntentTargetType.HOST,
                target_id=target_id,
            ),
            ObservationData(
                key="host.storage.devices",
                value=[d.model_dump() for d in devices],
                target_type=IntentTargetType.HOST,
                target_id=target_id,
            ),
        ]

        return ProbeResult(
            observations=observations,
            success=True,
            raw_payload=structured.model_dump(),
        )


__all__ = [
    "LSBLK_COLUMNS",
    "BlockDevice",
    "HostStorageOutput",
    "HostStorageProbe",
    "parse_lsblk_json",
]
