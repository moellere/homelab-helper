"""``network.subnet-scan`` probe — find live hosts on a CIDR via TCP connect.

For each host in the network, attempts an asyncio TCP connect against a small
panel of "homelab-y" ports in parallel. If any port accepts the connection
within ``per_port_timeout`` the host is recorded as live, along with the set
of ports that responded. Bounded concurrency (semaphore) keeps a /16 from
opening ten thousand simultaneous sockets.

Emits per scan:

- ``network.scan.cidr`` (str)         — what was scanned, target_id == cidr
- ``network.scan.scanned_count`` (int) — hosts considered (i.e. ``.hosts()``)
- ``network.scan.live_hosts`` (list)   — sorted list of ``{ip, open_ports}``

Emits per live host:

- ``network.host.open_ports`` (list[int]) — target_id == ip
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from typing import ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult

# Common homelab-discovery ports. Tuned for high "if anything answers, it's
# probably real" signal; not exhaustive on purpose — every additional port is
# more time per host.
DEFAULT_PORTS: tuple[int, ...] = (
    22,  # SSH
    80,  # HTTP
    443,  # HTTPS
    53,  # DNS (TCP)
    8006,  # Proxmox VE
    8080,  # generic HTTP-alt
    9090,  # Cockpit
    6443,  # Kubernetes API
)

_DEFAULT_PER_PORT_TIMEOUT_S = 0.8
_DEFAULT_CONCURRENCY = 200


class LiveHost(BaseModel):
    ip: str
    open_ports: list[int]


class NetworkScanOutput(BaseModel):
    cidr: str
    scanned_count: int
    live_hosts: list[LiveHost]


class NetworkSubnetScanProbe(Probe):
    name: ClassVar[str] = "network.subnet-scan"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.NONE
    target_kinds: ClassVar[list[str]] = ["network"]
    produces_keys: ClassVar[list[str]] = [
        "network.scan.cidr",
        "network.scan.scanned_count",
        "network.scan.live_hosts",
        "network.host.open_ports",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = NetworkScanOutput
    description: ClassVar[str | None] = (
        "Asyncio TCP connect-scan of a CIDR against a small panel of common ports. "
        "No nmap dependency; no raw sockets."
    )

    def __init__(
        self,
        *,
        ports: tuple[int, ...] | None = None,
        per_port_timeout_s: float | None = None,
        concurrency: int | None = None,
    ) -> None:
        super().__init__()
        self.ports: tuple[int, ...] = tuple(ports) if ports is not None else DEFAULT_PORTS
        self.per_port_timeout_s: float = (
            per_port_timeout_s if per_port_timeout_s is not None else _DEFAULT_PER_PORT_TIMEOUT_S
        )
        self.concurrency: int = concurrency if concurrency is not None else _DEFAULT_CONCURRENCY

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        target = ctx.target
        if target.kind != "network":
            return ProbeResult(success=False, error=f"unsupported target kind: {target.kind!r}")
        if not target.network_cidr:
            return ProbeResult(success=False, error="target.network_cidr is required")
        try:
            network = ipaddress.ip_network(target.network_cidr, strict=False)
        except ValueError as exc:
            return ProbeResult(success=False, error=f"invalid CIDR: {exc}")

        addresses = list(network.hosts())
        if not addresses:
            return ProbeResult(
                success=False, error=f"{target.network_cidr!r} has no host addresses"
            )

        sem = asyncio.Semaphore(self.concurrency)
        live: list[LiveHost] = []

        async def scan_one(ip_obj: ipaddress._BaseAddress) -> None:
            async with sem:
                ports_up = await self._scan_host(str(ip_obj))
            if ports_up:
                live.append(LiveHost(ip=str(ip_obj), open_ports=ports_up))

        await asyncio.gather(*(scan_one(ip) for ip in addresses))
        live.sort(key=lambda h: ipaddress.ip_address(h.ip))

        structured = NetworkScanOutput(
            cidr=target.network_cidr,
            scanned_count=len(addresses),
            live_hosts=live,
        )

        observations: list[ObservationData] = [
            ObservationData(
                key="network.scan.cidr",
                value=target.network_cidr,
                target_id=target.network_cidr,
            ),
            ObservationData(
                key="network.scan.scanned_count",
                value=len(addresses),
                target_id=target.network_cidr,
            ),
            ObservationData(
                key="network.scan.live_hosts",
                value=[h.model_dump() for h in live],
                target_id=target.network_cidr,
            ),
        ]
        for h in live:
            observations.append(
                ObservationData(
                    key="network.host.open_ports",
                    value=h.open_ports,
                    target_id=h.ip,
                )
            )

        return ProbeResult(
            observations=observations,
            success=True,
            raw_payload=structured.model_dump(),
        )

    async def _scan_host(self, ip: str) -> list[int]:
        """Return sorted list of open ports on ``ip`` from ``self.ports``."""
        results = await asyncio.gather(
            *(self._try_port(ip, p) for p in self.ports),
            return_exceptions=False,
        )
        return sorted(p for p in results if p is not None)

    async def _try_port(self, ip: str, port: int) -> int | None:
        try:
            fut = asyncio.open_connection(ip, port)
            _reader, writer = await asyncio.wait_for(fut, timeout=self.per_port_timeout_s)
        except (TimeoutError, OSError):
            return None
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return port


__all__ = [
    "DEFAULT_PORTS",
    "LiveHost",
    "NetworkScanOutput",
    "NetworkSubnetScanProbe",
]
