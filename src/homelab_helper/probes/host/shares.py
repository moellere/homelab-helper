"""``host.shares`` probe — what a Linux host exports over NFS and SMB.

The host-level counterpart of the OpenMediaVault adapter's export reads: any
Linux NAS (or a box that just exports a directory) reports its shares, and the
reconciler runs the same stray-export check OMV gets (``engine/stray_export``)
— an export whose path is gone, or whose declared filesystem is not mounted.

Sources:

- NFS: ``exportfs -v`` (root, best effort — it is the effective export table,
  ``/etc/exports.d`` included), falling back to ``/etc/exports``.
- SMB: ``testparm -s`` (the parsed, defaults-resolved ``smb.conf``). Absent
  samba means no SMB shares, not an error.
- Backing: ``/etc/fstab`` (which mountpoint *should* hold each path),
  ``findmnt -J`` (what *is* mounted) and ``test -d`` per export path.

Emits the exports plus the two lists the stray detector needs, already in its
vocabulary: one "folder" per existing export path, whose ``device`` is the
fstab mountpoint that path lives under (``None`` when only ``/`` holds it — a
root-filesystem path cannot be judged), and the mounted filesystems.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult

_CLIENT = re.compile(r"^([^\s(]+)(?:\(([^)]*)\))?$")
_PSEUDO_FS = {"swap", "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "none"}
_SKIP_SMB = {"global", "printers", "print$"}


def _exports_entries(text: str) -> list[dict[str, Any]]:
    """Shared by ``/etc/exports`` and ``exportfs -v``: path then client(opts) tokens."""
    entries: list[dict[str, Any]] = []
    logical: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if logical and logical[-1].endswith("\\"):
            logical[-1] = logical[-1][:-1] + " " + line.strip()  # /etc/exports continuation
        elif raw[:1].isspace() and logical:
            logical[-1] += " " + line.strip()  # exportfs -v wraps long paths
        else:
            logical.append(line.strip())
    for line in logical:
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if not tokens or not tokens[0].startswith("/"):
            continue
        path, clients = tokens[0], tokens[1:]
        for token in clients or ["*"]:
            m = _CLIENT.match(token)
            if not m:
                continue
            entries.append(
                {"protocol": "nfs", "path": path, "client": m.group(1), "options": m.group(2) or ""}
            )
    return entries


def parse_nfs_exports(text: str) -> list[dict[str, Any]]:
    """NFS exports from ``exportfs -v`` or ``/etc/exports``: one row per path and client."""
    return _exports_entries(text)


def parse_testparm(text: str) -> list[dict[str, Any]]:
    """SMB shares with a path from ``testparm -s``; printers and ``[global]`` are skipped."""
    shares: list[dict[str, Any]] = []
    section: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        header = re.match(r"^\[(.+)\]$", line)
        if header:
            name = header.group(1).strip()
            section = None if name.lower() in _SKIP_SMB else {"name": name, "params": {}}
            if section is not None:
                shares.append(section)
            continue
        if section is not None and "=" in line:
            key, value = (p.strip() for p in line.split("=", 1))
            section["params"][key.lower()] = value
    out = []
    for s in shares:
        params = s["params"]
        if params.get("printable", "no").lower() in ("yes", "true", "1"):
            continue
        if not params.get("path"):
            continue  # [homes] and friends resolve per user; nothing fixed to judge
        out.append(
            {
                "protocol": "smb",
                "name": s["name"],
                "path": params["path"],
                "read_only": params.get("read only", "yes").lower() in ("yes", "true", "1"),
            }
        )
    return out


def parse_fstab_mountpoints(text: str) -> list[str]:
    """Real-filesystem and bind mountpoints declared in ``/etc/fstab``."""
    points: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        fields = line.split()
        if len(fields) < 3:  # noqa: PLR2004 - device mountpoint fstype
            continue
        mountpoint, fstype = fields[1], fields[2]
        options = fields[3].split(",") if len(fields) > 3 else []  # noqa: PLR2004
        # A bind mount is fstype "none" but a real promise: OpenMediaVault serves
        # NFS from /export/<name> bind-mounted off the data disk, and a missing
        # disk leaves an empty directory behind a nofail bind.
        if (fstype in _PSEUDO_FS and "bind" not in options) or not mountpoint.startswith("/"):
            continue
        points.append(mountpoint.replace("\\040", " "))
    return points


def parse_findmnt(text: str) -> list[dict[str, Any]]:
    """Mounted filesystems from ``findmnt -J -l -o SOURCE,TARGET,FSTYPE``."""
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return []
    out = []
    for fs in payload.get("filesystems") or []:
        if fs.get("fstype") in _PSEUDO_FS:
            continue
        target = fs.get("target")
        if target:
            out.append({"device": target, "mountpoint": target, "source": fs.get("source")})
    return out


def backing_mountpoint(path: str, mountpoints: list[str]) -> str | None:
    """The longest fstab mountpoint (other than ``/``) that contains ``path``."""
    best: str | None = None
    for mp in mountpoints:
        if mp == "/":
            continue
        if (path == mp or path.startswith(mp.rstrip("/") + "/")) and (
            best is None or len(mp) > len(best)
        ):
            best = mp
    return best


def stray_inputs(
    exports: list[dict[str, Any]],
    existing_paths: set[str],
    fstab_mountpoints: list[str],
    mounted: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(filesystems, folders)`` in ``engine/stray_export``'s vocabulary.

    One folder per export path that exists, whose ``device`` is the fstab
    mountpoint it should live on; a path that is gone has no folder, which the
    detector reports as an export with nothing behind it.
    """
    folders = [
        {"name": p, "uuid": p, "device": backing_mountpoint(p, fstab_mountpoints)}
        for p in sorted({e["path"] for e in exports} & existing_paths)
    ]
    return mounted, folders


class HostSharesOutput(BaseModel):
    exports: list[dict[str, Any]]
    export_count: int
    folders: list[dict[str, Any]]
    filesystems: list[dict[str, Any]]


class HostSharesProbe(Probe):
    name: ClassVar[str] = "host.shares"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = [
        "host.shares.exports",
        "host.shares.export_count",
        "host.shares.folders",
        "host.shares.filesystems",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = HostSharesOutput
    description: ClassVar[str | None] = (
        "NFS (exportfs -v or /etc/exports) and SMB (testparm -s) shares, with each "
        "path's declared and mounted backing so stray exports can be detected."
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
                effective = await ssh.run(f"{sudo}exportfs -v")
                if effective.ok and effective.stdout.strip():
                    nfs = parse_nfs_exports(effective.stdout)
                else:
                    declared = await ssh.run("cat /etc/exports")
                    nfs = parse_nfs_exports(declared.stdout) if declared.ok else []
                smb_res = await ssh.run("testparm -s 2>/dev/null")
                smb = parse_testparm(smb_res.stdout) if smb_res.ok else []
                exports = nfs + smb
                fstab = await ssh.run("cat /etc/fstab")
                mountpoints = parse_fstab_mountpoints(fstab.stdout) if fstab.ok else []
                mnt = await ssh.run("findmnt -J -l -o SOURCE,TARGET,FSTYPE")
                mounted = parse_findmnt(mnt.stdout) if mnt.ok else []
                existing: set[str] = set()
                for path in sorted({e["path"] for e in exports}):
                    check = await ssh.run(f"test -d {shlex.quote(path)}")
                    if check.ok:
                        existing.add(path)
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")

        filesystems, folders = stray_inputs(exports, existing, mountpoints, mounted)
        structured = HostSharesOutput(
            exports=exports, export_count=len(exports), folders=folders, filesystems=filesystems
        )
        target_id = target.host_id or target.hostname or "unknown"
        observations = [
            ObservationData(
                key=key, value=value, target_type=IntentTargetType.HOST, target_id=target_id
            )
            for key, value in (
                ("host.shares.exports", exports),
                ("host.shares.export_count", len(exports)),
                ("host.shares.folders", folders),
                ("host.shares.filesystems", filesystems),
            )
        ]
        return ProbeResult(
            observations=observations, success=True, raw_payload=structured.model_dump()
        )


__all__ = [
    "HostSharesOutput",
    "HostSharesProbe",
    "backing_mountpoint",
    "parse_findmnt",
    "parse_fstab_mountpoints",
    "parse_nfs_exports",
    "parse_testparm",
    "stray_inputs",
]
