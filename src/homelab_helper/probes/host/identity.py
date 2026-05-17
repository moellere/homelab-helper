"""``host.identity`` probe — the simplest first-party probe.

Reads basic identity facts every Linux host can answer cheaply:

- ``hostname`` (from ``hostname`` command)
- ``kernel`` (from ``uname -r``)
- ``machine_id`` (from ``/etc/machine-id``)
- ``os_id`` and ``os_pretty_name`` (from ``/etc/os-release``)
- ``boot_time_unix`` (derived from ``/proc/uptime`` + current time)

Emits one observation per fact. All facts are optional except ``hostname`` —
if hostname can't be read, the probe fails entirely.
"""

from __future__ import annotations

import time
from typing import ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult

_QUOTED_VALUE_MIN_LEN = 2  # need at least quote+quote to be "quoted"


def parse_os_release(content: str) -> dict[str, str]:
    """Parse ``/etc/os-release`` content into a dict.

    Format is shell-style ``KEY=VALUE`` lines, possibly with double or single
    quoted values, possibly with comments. We tolerate junk lines silently.
    """
    out: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= _QUOTED_VALUE_MIN_LEN and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def parse_uptime(content: str) -> float | None:
    """``/proc/uptime`` returns ``<uptime_seconds> <idle_seconds>``.

    Returns the uptime as a float, or ``None`` if parsing fails.
    """
    try:
        first = content.strip().split()[0]
        return float(first)
    except (IndexError, ValueError):
        return None


class HostIdentityOutput(BaseModel):
    """Structured output of the host.identity probe."""

    hostname: str
    kernel: str | None = None
    machine_id: str | None = None
    os_id: str | None = None
    os_pretty_name: str | None = None
    boot_time_unix: int | None = None


class HostIdentityProbe(Probe):
    name: ClassVar[str] = "host.identity"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = [
        "host.identity.hostname",
        "host.identity.kernel",
        "host.identity.machine_id",
        "host.identity.os_id",
        "host.identity.os_pretty_name",
        "host.identity.boot_time_unix",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = HostIdentityOutput
    description: ClassVar[str | None] = (
        "Basic identity facts of a Linux host: hostname, kernel, OS, machine-id, boot time."
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
        assert connect_host  # narrowed by the check above

        try:
            async with adapter.session(
                connect_host,
                user=target.ssh_user,
                key_path=target.ssh_key_path,
                password=target.ssh_password,
                port=target.ssh_port,
            ) as ssh:
                hostname_res = await ssh.run("hostname")
                if not hostname_res.ok or not hostname_res.stdout.strip():
                    return ProbeResult(
                        success=False,
                        error=f"could not read hostname (exit {hostname_res.exit_code})",
                    )
                hostname = hostname_res.stdout.strip()
                kernel_res = await ssh.run("uname -r")
                machine_id_res = await ssh.run("cat /etc/machine-id 2>/dev/null || true")
                os_release_res = await ssh.run("cat /etc/os-release 2>/dev/null || true")
                uptime_res = await ssh.run("cat /proc/uptime 2>/dev/null || true")
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")

        kernel = kernel_res.stdout.strip() if kernel_res.ok else None
        machine_id = (
            machine_id_res.stdout.strip()
            if machine_id_res.ok and machine_id_res.stdout.strip()
            else None
        )

        os_data = parse_os_release(os_release_res.stdout) if os_release_res.ok else {}
        os_id = os_data.get("ID") or None
        os_pretty_name = os_data.get("PRETTY_NAME") or os_data.get("NAME") or None

        uptime_s = parse_uptime(uptime_res.stdout) if uptime_res.ok else None
        boot_time_unix = int(time.time() - uptime_s) if uptime_s is not None else None

        structured = HostIdentityOutput(
            hostname=hostname,
            kernel=kernel,
            machine_id=machine_id,
            os_id=os_id,
            os_pretty_name=os_pretty_name,
            boot_time_unix=boot_time_unix,
        )

        target_id = target.host_id or hostname
        observations: list[ObservationData] = []
        for key, value in (
            ("host.identity.hostname", structured.hostname),
            ("host.identity.kernel", structured.kernel),
            ("host.identity.machine_id", structured.machine_id),
            ("host.identity.os_id", structured.os_id),
            ("host.identity.os_pretty_name", structured.os_pretty_name),
            ("host.identity.boot_time_unix", structured.boot_time_unix),
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

        return ProbeResult(
            observations=observations,
            success=True,
            raw_payload=structured.model_dump(),
        )


__all__ = ["HostIdentityOutput", "HostIdentityProbe", "parse_os_release", "parse_uptime"]
