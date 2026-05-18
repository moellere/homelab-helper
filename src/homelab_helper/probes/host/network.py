"""``host.network`` probe — interfaces, MACs, MTUs, link state, speeds, addresses.

Source of truth: ``ip -j link show`` + ``ip -j addr show`` for the structural
view, plus ``/sys/class/net/<iface>/speed`` for the link-speed scalar (which
``ip`` doesn't expose). Loopback is filtered out.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult


class InterfaceAddress(BaseModel):
    family: str  # "inet" or "inet6"
    ip: str
    prefix: int


class InterfaceSummary(BaseModel):
    name: str
    mac: str | None = None
    mtu: int | None = None
    operstate: str | None = None  # UP / DOWN / UNKNOWN
    link_type: str | None = None  # ether / loopback / sit / ...
    master: str | None = None  # bridge / bond parent
    speed_mbps: int | None = None  # from /sys; None for virtual/down
    addresses: list[InterfaceAddress] = []


def _merge_addrs(addr_payload: list[dict[str, Any]]) -> dict[str, list[InterfaceAddress]]:
    """Build ``{ifname: [addresses]}`` from ``ip -j addr show`` output."""
    out: dict[str, list[InterfaceAddress]] = {}
    for entry in addr_payload:
        name = entry.get("ifname")
        if not name:
            continue
        addrs: list[InterfaceAddress] = []
        for info in entry.get("addr_info", []):
            family = info.get("family")
            ip = info.get("local")
            prefix = info.get("prefixlen")
            if family in {"inet", "inet6"} and ip and isinstance(prefix, int):
                addrs.append(InterfaceAddress(family=family, ip=ip, prefix=prefix))
        out[name] = addrs
    return out


def parse_ip_link_show(
    link_json: str, addr_json: str, speed_map: dict[str, int]
) -> list[InterfaceSummary]:
    """Build the per-interface list from ``ip -j link`` + ``ip -j addr`` outputs.

    Loopback is dropped. ``speed_map`` maps ifname → link-speed-Mb/s as
    reported by ``/sys/class/net/<i>/speed`` (-1 / read failure → omitted).
    """
    addr_by_name: dict[str, list[InterfaceAddress]] = {}
    with contextlib.suppress(json.JSONDecodeError):
        addr_by_name = _merge_addrs(json.loads(addr_json or "[]"))

    out: list[InterfaceSummary] = []
    try:
        link_entries = json.loads(link_json or "[]")
    except json.JSONDecodeError:
        return out

    for entry in link_entries:
        name = entry.get("ifname")
        link_type = entry.get("link_type")
        if not name or name == "lo" or link_type == "loopback":
            continue
        out.append(
            InterfaceSummary(
                name=name,
                mac=entry.get("address"),
                mtu=entry.get("mtu"),
                operstate=entry.get("operstate"),
                link_type=link_type,
                master=entry.get("master"),
                speed_mbps=speed_map.get(name),
                addresses=addr_by_name.get(name, []),
            )
        )
    return out


def parse_speed_lines(content: str) -> dict[str, int]:
    """Parse the output of ``for i in /sys/class/net/*/speed; do echo "$i $(cat $i)"; done``.

    Lines we drop: those whose value is ``-1`` (interface down / virtual).
    """
    out: dict[str, int] = {}
    for line in content.splitlines():
        parts = line.split()
        if len(parts) != _SPEED_PARTS_PER_LINE:
            continue
        path, raw = parts
        try:
            value = int(raw)
        except ValueError:
            continue
        if value <= 0:
            continue
        # path is "/sys/class/net/<ifname>/speed"
        segments = path.split("/")
        if len(segments) < _SPEED_PATH_MIN_SEGMENTS or segments[-1] != "speed":
            continue
        ifname = segments[-2]
        out[ifname] = value
    return out


_SPEED_PARTS_PER_LINE = 2
_SPEED_PATH_MIN_SEGMENTS = 2


class HostNetworkOutput(BaseModel):
    interfaces: list[InterfaceSummary]
    interface_count: int


class HostNetworkProbe(Probe):
    name: ClassVar[str] = "host.network"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = [
        "host.network.interfaces",
        "host.network.interface_count",
        "host.network.interface_names",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = HostNetworkOutput
    description: ClassVar[str | None] = (
        "Interfaces, MACs, MTUs, link state, link speed, and L3 addresses (no loopback)."
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
                link_res = await ssh.run("ip -j link show")
                addr_res = await ssh.run("ip -j addr show")
                speed_res = await ssh.run(
                    "for s in /sys/class/net/*/speed; do "
                    '[ -r "$s" ] && echo "$s $(cat "$s" 2>/dev/null)"; '
                    "done"
                )
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")

        if not link_res.ok or not link_res.stdout.strip():
            return ProbeResult(
                success=False,
                error=f"ip -j link show failed (exit {link_res.exit_code})",
            )

        speed_map = parse_speed_lines(speed_res.stdout) if speed_res.ok else {}
        interfaces = parse_ip_link_show(link_res.stdout, addr_res.stdout, speed_map)
        structured = HostNetworkOutput(interfaces=interfaces, interface_count=len(interfaces))

        target_id = target.host_id or target.hostname or "unknown"
        observations: list[ObservationData] = [
            ObservationData(
                key="host.network.interface_count",
                value=len(interfaces),
                target_type=IntentTargetType.HOST,
                target_id=target_id,
            ),
            ObservationData(
                key="host.network.interface_names",
                value=[i.name for i in interfaces],
                target_type=IntentTargetType.HOST,
                target_id=target_id,
            ),
            ObservationData(
                key="host.network.interfaces",
                value=[i.model_dump() for i in interfaces],
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
    "HostNetworkOutput",
    "HostNetworkProbe",
    "InterfaceAddress",
    "InterfaceSummary",
    "parse_ip_link_show",
    "parse_speed_lines",
]
