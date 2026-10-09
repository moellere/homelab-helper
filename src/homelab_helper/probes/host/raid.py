"""``host.raid`` probe — Linux software RAID (mdraid) composition and health.

``host.storage`` already sees ``md*`` nodes in the block-device tree, and the
reconciler deliberately keeps them out of part lineage (a logical device has no
WWN). This probe adds the array view: what each array is made of and whether it
is whole.

Sources:

- ``/proc/mdstat`` (no root) — every array's level, state, member devices and
  their roles (``(F)`` faulty, ``(S)`` spare, ``(W)`` write-mostly), the
  ``[n/m]`` slot count and any resync / recovery / reshape / check in flight.
- ``mdadm --detail /dev/mdN`` (root, best effort) — the array UUID. Skipped
  without error when sudo or mdadm is unavailable; composition and health come
  from ``/proc/mdstat`` alone.

A host with no md driver loaded (no ``/proc/mdstat``) reports zero arrays — an
observation in its own right, so a degraded-array finding resolves once the
array is gone. Only an SSH failure yields no observation at all.

Health is judged by the reconciler, not here: the probe reports ``degraded``
and ``sync_action`` and the reconciler turns them into ``storage-health``
findings (``engine/raid_health.py``).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult

_HEADER = re.compile(r"^(md[\w/]+)\s*:\s*(.*)$")
_MEMBER = re.compile(r"^([\w./-]+)\[(\d+)\]((?:\([A-Z]\))*)$")
_SLOTS = re.compile(r"\[(\d+)/(\d+)\]\s*\[([U_]+)\]")
_BLOCKS = re.compile(r"^\s*(\d+)\s+blocks")
_SYNC = re.compile(r"\b(recovery|resync|reshape|check|repair)\s*=\s*([\d.]+)%")
_DELAYED = re.compile(r"\b(resync|recovery)\s*=\s*(DELAYED|PENDING)")
_ROLE = {"F": "faulty", "S": "spare", "W": "write-mostly", "R": "replacement"}
_LEVELS = ("raid0", "raid1", "raid4", "raid5", "raid6", "raid10", "linear", "multipath")


def _member(token: str) -> dict[str, Any] | None:
    m = _MEMBER.match(token)
    if not m:
        return None
    flags = re.findall(r"\(([A-Z])\)", m.group(3))
    state = next((_ROLE[f] for f in flags if f in _ROLE), "active")
    return {"device": m.group(1), "slot": int(m.group(2)), "state": state}


def parse_mdstat(content: str) -> list[dict[str, Any]]:
    """Every array in ``/proc/mdstat``, in file order."""
    arrays: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in content.splitlines():
        header = _HEADER.match(line)
        if header and not line.startswith(("Personalities", "unused devices")):
            tokens = header.group(2).split()
            status = tokens[0] if tokens else "unknown"
            rest = tokens[1:]
            read_only = None
            if rest and rest[0] in ("(read-only)", "(auto-read-only)"):
                read_only = rest[0].strip("()")
                rest = rest[1:]
            level = rest[0] if rest and rest[0] in _LEVELS else None
            members = [m for m in (_member(t) for t in (rest[1:] if level else rest)) if m]
            current = {
                "name": header.group(1),
                "level": level,
                "state": status if read_only is None else f"{status} ({read_only})",
                "size_bytes": None,
                "raid_disks": None,
                "active_disks": None,
                "degraded": False,
                "sync_action": None,
                "sync_progress": None,
                "uuid": None,
                "members": sorted(members, key=lambda m: m["slot"]),
            }
            arrays.append(current)
            continue
        if current is None:
            continue
        blocks = _BLOCKS.match(line)
        if blocks and current["size_bytes"] is None:
            current["size_bytes"] = int(blocks.group(1)) * 1024
        slots = _SLOTS.search(line)
        if slots:
            current["raid_disks"] = int(slots.group(1))
            current["active_disks"] = int(slots.group(2))
            current["degraded"] = "_" in slots.group(3)
        sync = _SYNC.search(line)
        if sync:
            current["sync_action"] = sync.group(1)
            current["sync_progress"] = float(sync.group(2))
        delayed = _DELAYED.search(line)
        if delayed and current["sync_action"] is None:
            current["sync_action"] = delayed.group(1)
    return arrays


def parse_mdadm_uuid(content: str) -> str | None:
    """The ``UUID :`` line of ``mdadm --detail``."""
    m = re.search(r"^\s*UUID\s*:\s*([0-9a-fA-F:]+)\s*$", content, re.MULTILINE)
    return m.group(1).lower() if m else None


def array_summary(array: dict[str, Any]) -> dict[str, Any]:
    """The compact form projected onto ``Host.capabilities["raid"]``."""
    disks = (
        f"{array['active_disks']}/{array['raid_disks']}"
        if array.get("raid_disks") is not None
        else None
    )
    return {
        "name": array["name"],
        "level": array.get("level"),
        "state": array.get("state"),
        "disks": disks,
        "degraded": bool(array.get("degraded")),
        "sync_action": array.get("sync_action"),
        "members": [m["device"] for m in array.get("members") or []],
    }


class HostRaidOutput(BaseModel):
    arrays: list[dict[str, Any]]
    array_count: int
    summary: list[dict[str, Any]]


class HostRaidProbe(Probe):
    name: ClassVar[str] = "host.raid"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = [
        "host.raid.arrays",
        "host.raid.array_count",
        "host.raid.summary",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = HostRaidOutput
    description: ClassVar[str | None] = (
        "Linux software RAID (mdraid) from /proc/mdstat: each array's level, state, "
        "members and their roles, degraded slots and any rebuild in flight; UUID via "
        "mdadm --detail when sudo allows."
    )

    async def run(self, ctx: ProbeContext) -> ProbeResult:
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
                mdstat = await ssh.run("cat /proc/mdstat")
                arrays = parse_mdstat(mdstat.stdout) if mdstat.ok else []
                for array in arrays:
                    detail = await ssh.run(f"{sudo}mdadm --detail /dev/{array['name']}")
                    if detail.ok:
                        array["uuid"] = parse_mdadm_uuid(detail.stdout)
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")

        summary = [array_summary(a) for a in arrays]
        structured = HostRaidOutput(arrays=arrays, array_count=len(arrays), summary=summary)
        target_id = target.host_id or target.hostname or "unknown"
        observations = [
            ObservationData(
                key=key, value=value, target_type=IntentTargetType.HOST, target_id=target_id
            )
            for key, value in (
                ("host.raid.arrays", arrays),
                ("host.raid.array_count", len(arrays)),
                ("host.raid.summary", summary),
            )
        ]
        return ProbeResult(
            observations=observations, success=True, raw_payload=structured.model_dump()
        )


__all__ = [
    "HostRaidOutput",
    "HostRaidProbe",
    "array_summary",
    "parse_mdadm_uuid",
    "parse_mdstat",
]
