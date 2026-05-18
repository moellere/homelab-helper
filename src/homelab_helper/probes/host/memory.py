"""``host.memory`` probe — RAM totals + hugepages from ``/proc/meminfo``.

This probe is RAM-totals only; DIMM-level inventory (slots, vendors, sizes per
DIMM) needs ``dmidecode`` which requires sudo. That's a separate probe coming
later in this slice's progression.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult

_MEMINFO_UNIT_TOKEN_COUNT = 2  # value + unit


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
    ]
    output_schema: ClassVar[type[BaseModel] | None] = HostMemoryOutput
    description: ClassVar[str | None] = (
        "RAM and swap totals from /proc/meminfo. DIMM-level layout is a separate (sudo-only) probe."
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
                res = await ssh.run("cat /proc/meminfo")
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")

        if not res.ok:
            return ProbeResult(
                success=False, error=f"could not read /proc/meminfo (exit {res.exit_code})"
            )

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

        return ProbeResult(
            observations=observations,
            success=True,
            raw_payload=structured.model_dump(),
        )


__all__ = ["HostMemoryOutput", "HostMemoryProbe", "parse_meminfo"]
