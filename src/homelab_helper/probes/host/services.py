"""``host.services`` probe — systemd services that are running or failed.

Uses ``systemctl list-units`` with ``--plain --no-legend --no-pager`` for
clean, scriptable output. Captures both the live "running" set and the
"failed" set; the latter is what generates findings down the road.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult


class ServiceUnit(BaseModel):
    name: str  # e.g. "ssh.service"
    load: str  # "loaded"
    active: str  # "active"
    sub: str  # "running" / "failed" / "exited" / ...
    description: str | None = None


def parse_systemctl_units(content: str) -> list[ServiceUnit]:
    """Parse ``systemctl list-units --plain --no-legend --no-pager`` output.

    Each non-empty line is ``UNIT LOAD ACTIVE SUB DESCRIPTION``. Description
    may contain spaces (we treat everything after column 4 as one field).
    """
    units: list[ServiceUnit] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(maxsplit=4)
        if len(parts) < _MIN_COLUMNS_BEFORE_DESCRIPTION:
            continue
        name, load, active, sub = parts[:4]
        description = parts[4] if len(parts) == _FULL_COLUMN_COUNT else None
        if not name.endswith(".service"):
            continue
        units.append(
            ServiceUnit(name=name, load=load, active=active, sub=sub, description=description)
        )
    return units


_MIN_COLUMNS_BEFORE_DESCRIPTION = 4
_FULL_COLUMN_COUNT = 5


class HostServicesOutput(BaseModel):
    running_services: list[ServiceUnit]
    failed_services: list[ServiceUnit]
    running_count: int
    failed_count: int


class HostServicesProbe(Probe):
    name: ClassVar[str] = "host.services"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = [
        "host.services.running",
        "host.services.failed",
        "host.services.running_count",
        "host.services.failed_count",
        "host.services.failed_names",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = HostServicesOutput
    description: ClassVar[str | None] = (
        "Running and failed systemd services. Failed-count is the primary signal for the "
        "reconciler; running-list is debugging aid."
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

        base = "systemctl list-units --type=service --no-pager --plain --no-legend"
        try:
            async with adapter.session(
                connect_host,
                user=target.ssh_user,
                key_path=target.ssh_key_path,
                password=target.ssh_password,
                port=target.ssh_port,
            ) as ssh:
                running_res = await ssh.run(f"{base} --state=running")
                failed_res = await ssh.run(f"{base} --state=failed")
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")

        if not running_res.ok:
            return ProbeResult(
                success=False,
                error=f"systemctl running query failed (exit {running_res.exit_code})",
            )

        running = parse_systemctl_units(running_res.stdout)
        failed = parse_systemctl_units(failed_res.stdout) if failed_res.ok else []

        structured = HostServicesOutput(
            running_services=running,
            failed_services=failed,
            running_count=len(running),
            failed_count=len(failed),
        )

        target_id = target.host_id or target.hostname or "unknown"
        observations: list[ObservationData] = [
            ObservationData(
                key="host.services.running_count",
                value=len(running),
                target_type=IntentTargetType.HOST,
                target_id=target_id,
            ),
            ObservationData(
                key="host.services.failed_count",
                value=len(failed),
                target_type=IntentTargetType.HOST,
                target_id=target_id,
            ),
            ObservationData(
                key="host.services.running",
                value=[u.model_dump() for u in running],
                target_type=IntentTargetType.HOST,
                target_id=target_id,
            ),
            ObservationData(
                key="host.services.failed",
                value=[u.model_dump() for u in failed],
                target_type=IntentTargetType.HOST,
                target_id=target_id,
            ),
            ObservationData(
                key="host.services.failed_names",
                value=[u.name for u in failed],
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
    "HostServicesOutput",
    "HostServicesProbe",
    "ServiceUnit",
    "parse_systemctl_units",
]
