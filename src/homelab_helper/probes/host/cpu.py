"""``host.cpu`` probe — CPU model, topology, flags, and cache layout.

Primary source: ``lscpu -J`` (structured JSON from util-linux). Falls back
to ``/proc/cpuinfo`` for the few fields lscpu doesn't expose on older
distributions. Pure parse helpers are module-level for testability.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult

# Flag allowlist for the structured output. The full flag set lives in the
# observation value for the catch-all ``host.cpu.flags`` key; this list is for
# the typed Pydantic schema where presence of each flag is interesting.
INTERESTING_FLAGS: tuple[str, ...] = (
    "avx",
    "avx2",
    "avx512f",
    "vmx",
    "svm",
    "aes",
    "rdrand",
    "rdseed",
    "sha_ni",
    "sse4_2",
    "sse4_1",
    "pclmulqdq",
    "bmi1",
    "bmi2",
    "popcnt",
    "f16c",
    "fma",
    "fsgsbase",
)


def flatten_lscpu_json(payload: dict[str, Any]) -> dict[str, str]:
    """Convert ``lscpu -J`` tree into a flat ``{field: data}`` dict.

    Output shape (lscpu >= 2.34) is::

        {"lscpu": [{"field": "Architecture:", "data": "x86_64", "children": [...]}, ...]}

    We strip trailing colons from field names and recurse into ``children``.
    """
    out: dict[str, str] = {}

    def walk(items: list[dict[str, Any]]) -> None:
        for entry in items:
            field = entry.get("field", "").rstrip(":").strip()
            data = entry.get("data")
            if field and data is not None:
                out[field] = str(data)
            children = entry.get("children")
            if isinstance(children, list):
                walk(children)

    top = payload.get("lscpu")
    if isinstance(top, list):
        walk(top)
    return out


def parse_int_or_none(s: str | None) -> int | None:
    if s is None:
        return None
    try:
        return int(s.replace(",", "").split()[0])
    except (ValueError, IndexError):
        return None


def parse_freq_mhz(s: str | None) -> int | None:
    """lscpu typically reports MHz as a float string like ``2300.0000``."""
    if s is None:
        return None
    try:
        return int(float(s.split()[0]))
    except (ValueError, IndexError):
        return None


_CACHE_MIN_PARTS_WITH_SUFFIX = 2
_CACHE_MIN_PARTS_FOR_INSTANCES = 2


def parse_cache_bytes(s: str | None) -> int | None:  # noqa: PLR0912
    """Parse strings like '32K', '256 KiB (4 instances)', '12 MiB' into bytes.

    Returns the total reported size in bytes, summed across instances when
    lscpu reports them (e.g. '256 KiB (4 instances)' -> 4*256*1024).
    """
    if not s:
        return None
    parts = s.split()
    if not parts:
        return None
    raw = parts[0]
    multiplier = 1
    suffix = ""
    if len(parts) >= _CACHE_MIN_PARTS_WITH_SUFFIX:
        suffix = parts[1].lower()
    else:
        # bare 'NK' or 'NKiB' — strip suffix from raw
        for n in range(len(raw) - 1, -1, -1):
            if raw[n].isdigit() or raw[n] == ".":
                suffix = raw[n + 1 :].lower()
                raw = raw[: n + 1]
                break
    try:
        num = float(raw)
    except ValueError:
        return None
    if suffix.startswith("k"):
        multiplier = 1024 if "i" in suffix else 1000
    elif suffix.startswith("m"):
        multiplier = 1024 * 1024 if "i" in suffix else 1_000_000
    elif suffix.startswith("g"):
        multiplier = 1024**3 if "i" in suffix else 1_000_000_000
    # Sum across instances if reported.
    instances = 1
    joined = (
        " ".join(parts[_CACHE_MIN_PARTS_FOR_INSTANCES:])
        if len(parts) > _CACHE_MIN_PARTS_FOR_INSTANCES
        else ""
    )
    if "instance" in joined:
        for tok in joined.split():
            if tok.lstrip("(").isdigit():
                instances = int(tok.lstrip("("))
                break
    return int(num * multiplier * instances)


def parse_proc_cpuinfo(content: str) -> dict[str, str]:
    """Parse the first processor block from /proc/cpuinfo into a flat dict."""
    out: dict[str, str] = {}
    for line in content.splitlines():
        if not line.strip():
            # End of first processor block - we have enough.
            if out:
                break
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        out[key.strip()] = value.strip()
    return out


class HostCpuOutput(BaseModel):
    """Structured output of host.cpu."""

    model: str | None = None
    vendor: str | None = None
    architecture: str | None = None
    sockets: int | None = None
    cores: int | None = None
    threads: int | None = None
    threads_per_core: int | None = None
    base_freq_mhz: int | None = None
    max_freq_mhz: int | None = None
    l1d_bytes: int | None = None
    l1i_bytes: int | None = None
    l2_bytes: int | None = None
    l3_bytes: int | None = None
    interesting_flags: list[str] = []


def build_output(lscpu_flat: dict[str, str], cpuinfo_flat: dict[str, str]) -> HostCpuOutput:
    """Build the structured output dict from parsed sources.

    Prefers lscpu fields; falls back to /proc/cpuinfo where lscpu is missing.
    """
    model = lscpu_flat.get("Model name") or cpuinfo_flat.get("model name") or None
    vendor = lscpu_flat.get("Vendor ID") or cpuinfo_flat.get("vendor_id") or None
    arch = lscpu_flat.get("Architecture")
    sockets = parse_int_or_none(lscpu_flat.get("Socket(s)"))
    cores_per_socket = parse_int_or_none(lscpu_flat.get("Core(s) per socket"))
    threads_per_core = parse_int_or_none(lscpu_flat.get("Thread(s) per core"))
    cpus = parse_int_or_none(lscpu_flat.get("CPU(s)"))
    cores = cores_per_socket * sockets if (cores_per_socket and sockets) else None
    threads = cpus

    flag_blob = lscpu_flat.get("Flags") or cpuinfo_flat.get("flags") or ""
    all_flags = set(flag_blob.split())
    interesting = sorted(f for f in INTERESTING_FLAGS if f in all_flags)

    return HostCpuOutput(
        model=model,
        vendor=vendor,
        architecture=arch,
        sockets=sockets,
        cores=cores,
        threads=threads,
        threads_per_core=threads_per_core,
        base_freq_mhz=parse_freq_mhz(lscpu_flat.get("CPU MHz")),
        max_freq_mhz=parse_freq_mhz(lscpu_flat.get("CPU max MHz")),
        l1d_bytes=parse_cache_bytes(lscpu_flat.get("L1d cache")),
        l1i_bytes=parse_cache_bytes(lscpu_flat.get("L1i cache")),
        l2_bytes=parse_cache_bytes(lscpu_flat.get("L2 cache")),
        l3_bytes=parse_cache_bytes(lscpu_flat.get("L3 cache")),
        interesting_flags=interesting,
    )


class HostCpuProbe(Probe):
    name: ClassVar[str] = "host.cpu"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = [
        "host.cpu.model",
        "host.cpu.vendor",
        "host.cpu.architecture",
        "host.cpu.sockets",
        "host.cpu.cores",
        "host.cpu.threads",
        "host.cpu.threads_per_core",
        "host.cpu.base_freq_mhz",
        "host.cpu.max_freq_mhz",
        "host.cpu.l1d_bytes",
        "host.cpu.l1i_bytes",
        "host.cpu.l2_bytes",
        "host.cpu.l3_bytes",
        "host.cpu.flags",
        "host.cpu.interesting_flags",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = HostCpuOutput
    description: ClassVar[str | None] = (
        "CPU model, topology, frequencies, cache, and feature flags."
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
                lscpu_res = await ssh.run("lscpu -J 2>/dev/null || true")
                cpuinfo_res = await ssh.run("cat /proc/cpuinfo 2>/dev/null || true")
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")

        lscpu_flat: dict[str, str] = {}
        if lscpu_res.ok and lscpu_res.stdout.strip():
            with contextlib.suppress(json.JSONDecodeError):
                lscpu_flat = flatten_lscpu_json(json.loads(lscpu_res.stdout))

        cpuinfo_flat: dict[str, str] = {}
        if cpuinfo_res.ok and cpuinfo_res.stdout.strip():
            cpuinfo_flat = parse_proc_cpuinfo(cpuinfo_res.stdout)

        if not lscpu_flat and not cpuinfo_flat:
            return ProbeResult(success=False, error="both lscpu and /proc/cpuinfo unavailable")

        structured = build_output(lscpu_flat, cpuinfo_flat)
        target_id = target.host_id or target.hostname or "unknown"

        all_flags_blob = lscpu_flat.get("Flags") or cpuinfo_flat.get("flags") or ""
        all_flags = sorted(all_flags_blob.split())

        observations: list[ObservationData] = []
        scalar_keys: tuple[tuple[str, Any], ...] = (
            ("host.cpu.model", structured.model),
            ("host.cpu.vendor", structured.vendor),
            ("host.cpu.architecture", structured.architecture),
            ("host.cpu.sockets", structured.sockets),
            ("host.cpu.cores", structured.cores),
            ("host.cpu.threads", structured.threads),
            ("host.cpu.threads_per_core", structured.threads_per_core),
            ("host.cpu.base_freq_mhz", structured.base_freq_mhz),
            ("host.cpu.max_freq_mhz", structured.max_freq_mhz),
            ("host.cpu.l1d_bytes", structured.l1d_bytes),
            ("host.cpu.l1i_bytes", structured.l1i_bytes),
            ("host.cpu.l2_bytes", structured.l2_bytes),
            ("host.cpu.l3_bytes", structured.l3_bytes),
        )
        for key, value in scalar_keys:
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
        if all_flags:
            observations.append(
                ObservationData(
                    key="host.cpu.flags",
                    value=all_flags,
                    target_type=IntentTargetType.HOST,
                    target_id=target_id,
                )
            )
        if structured.interesting_flags:
            observations.append(
                ObservationData(
                    key="host.cpu.interesting_flags",
                    value=structured.interesting_flags,
                    target_type=IntentTargetType.HOST,
                    target_id=target_id,
                )
            )

        return ProbeResult(
            observations=observations,
            success=True,
            raw_payload=structured.model_dump(),
        )


__all__ = [
    "INTERESTING_FLAGS",
    "HostCpuOutput",
    "HostCpuProbe",
    "build_output",
    "flatten_lscpu_json",
    "parse_cache_bytes",
    "parse_freq_mhz",
    "parse_int_or_none",
    "parse_proc_cpuinfo",
]
