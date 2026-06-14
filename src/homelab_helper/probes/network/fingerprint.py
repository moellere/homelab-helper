"""``network.fingerprint`` probe — identify what's listening on a host's ports.

Given a target IP and a list of ports (usually fed from a prior subnet-scan
run), attempts a small set of cheap fingerprints per port:

- **SSH** (22 or any port whose first received line starts with ``SSH-``) →
  ``hint=ssh``, evidence carries the banner version string.
- **HTTP** (any port that returns a valid HTTP/x response to ``HEAD /``) →
  ``hint=http`` plus, where the heuristic matches, a more specific
  ``service=proxmox|cockpit|k8s-api|generic`` based on port and ``Server``
  header.

Pure stdlib (``asyncio.open_connection``) — no httpx dependency to keep the
probe boundary minimal. HTTPS is recorded as a guess based on port (443 / 8006
/ 6443 are well-known TLS endpoints) without performing a real TLS handshake
in this slice; a follow-up can wrap the connection in an SSL context to read
real certificate SANs.

Emits one observation per ``(ip, port)`` with a fingerprint hit:

- key: ``network.host.service.<port>``  (target_id = ip)
- value: ``{"port": int, "hint": str, "service": str | None, "evidence": dict}``

Plus a single summary observation per target:

- key: ``network.host.services``  (target_id = ip)
- value: list of the per-port dicts above
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult

# Port -> default service guess. The fingerprint methods below override this
# when the banner gives us better evidence; if the banner is silent or
# unreadable, the port-based guess is what's recorded.
_PORT_HINTS: dict[int, str] = {
    22: "ssh",
    80: "http",
    443: "https",
    53: "dns",
    8006: "proxmox-ve",
    8080: "http",
    9090: "cockpit",
    6443: "k8s-api",
}

_DEFAULT_TIMEOUT_S = 1.5
_BANNER_READ_BYTES = 1024
_HTTP_PORT = 80
_PROXMOX_PORT = 8006
_COCKPIT_PORT = 9090
_K8S_API_PORT = 6443
_HTTP_STATUS_PARTS_MIN = 2

# ASCII range for "is this text" detection (printable + common whitespace).
_PRINTABLE_LO = 0x20
_PRINTABLE_HI = 0x7F
_PRINTABLE_THRESHOLD = 0.85


class ServiceFingerprint(BaseModel):
    port: int
    hint: str
    service: str | None = None
    evidence: dict[str, Any] = {}


class NetworkFingerprintOutput(BaseModel):
    ip: str
    services: list[ServiceFingerprint]


class NetworkFingerprintProbe(Probe):
    name: ClassVar[str] = "network.fingerprint"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.NONE
    target_kinds: ClassVar[list[str]] = ["network", "host"]
    produces_keys: ClassVar[list[str]] = [
        "network.host.services",
        "network.host.service.*",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = NetworkFingerprintOutput
    description: ClassVar[str | None] = (
        "Banner-grab + port heuristic fingerprint for a single host. "
        "Identifies SSH, HTTP-family endpoints, Proxmox, Cockpit, K8s API."
    )

    def __init__(
        self,
        *,
        ports: list[int] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        super().__init__()
        self._ports_override: list[int] | None = list(ports) if ports is not None else None
        self.timeout_s: float = timeout_s if timeout_s is not None else _DEFAULT_TIMEOUT_S

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        target = ctx.target
        if not target.primary_ip:
            return ProbeResult(
                success=False,
                error="target.primary_ip is required for fingerprint",
            )

        ports = self._ports_override or list(_PORT_HINTS.keys())
        ip = target.primary_ip

        results = await asyncio.gather(
            *(self._fingerprint_port(ip, p) for p in ports),
            return_exceptions=False,
        )
        services = [r for r in results if r is not None]
        services.sort(key=lambda s: s.port)

        structured = NetworkFingerprintOutput(ip=ip, services=services)
        observations: list[ObservationData] = [
            ObservationData(
                key="network.host.services",
                value=[s.model_dump() for s in services],
                target_id=ip,
            )
        ]
        for s in services:
            observations.append(
                ObservationData(
                    key=f"network.host.service.{s.port}",
                    value=s.model_dump(),
                    target_id=ip,
                )
            )

        return ProbeResult(
            observations=observations,
            success=True,
            raw_payload=structured.model_dump(),
        )

    async def _fingerprint_port(self, ip: str, port: int) -> ServiceFingerprint | None:
        """Return a ServiceFingerprint or ``None`` if nothing responded."""
        try:
            fut = asyncio.open_connection(ip, port)
            reader, writer = await asyncio.wait_for(fut, timeout=self.timeout_s)
        except (TimeoutError, OSError):
            return None

        try:
            # Try SSH-style banner first: SSH servers greet immediately.
            try:
                banner = await asyncio.wait_for(reader.read(_BANNER_READ_BYTES), timeout=0.3)
            except TimeoutError:
                banner = b""

            if banner.startswith(b"SSH-"):
                version = banner.split(b"\n", 1)[0].decode("ascii", errors="replace").strip()
                return ServiceFingerprint(
                    port=port,
                    hint="ssh",
                    service="ssh",
                    evidence={"banner": version},
                )

            # Not SSH — try an HTTP HEAD. _http_probe returns None if the
            # server's response doesn't look HTTP-shaped, in which case we
            # fall back to a port-based hint below.
            http_fp = await self._http_probe(reader, writer, ip, port, banner)
            if http_fp is not None:
                return http_fp

            # Bare TCP response that's not SSH and not HTTP — record the port
            # hint so the operator still sees that something's listening.
            fallback_hint = _PORT_HINTS.get(port, "unknown")
            evidence: dict[str, Any] = {}
            if banner:
                evidence["first_bytes"] = banner[:64].decode("latin-1", errors="replace")
            return ServiceFingerprint(
                port=port,
                hint=fallback_hint,
                service=None,
                evidence=evidence,
            )
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _http_probe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        ip: str,
        port: int,
        already_read: bytes,
    ) -> ServiceFingerprint | None:
        """HTTP HEAD probe. Returns None if the response isn't HTTP-shaped."""
        # If the server greeted us with binary noise (probably TLS), don't try
        # to talk plaintext HTTP at it — record a TLS-likely guess instead.
        if already_read and not _looks_textual(already_read):
            return ServiceFingerprint(
                port=port,
                hint="tls",
                service=_PORT_HINTS.get(port, "unknown") if port != _HTTP_PORT else None,
                evidence={"reason": "binary greeting suggests TLS"},
            )

        request = f"HEAD / HTTP/1.0\r\nHost: {ip}\r\nUser-Agent: helper/0.1\r\n\r\n".encode()
        try:
            writer.write(request)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(_BANNER_READ_BYTES), timeout=0.5)
        except (TimeoutError, OSError):
            response = b""

        text = (already_read + response).decode("latin-1", errors="replace")
        first_line = text.split("\r\n", 1)[0]
        if not first_line.startswith("HTTP/"):
            return None  # caller falls back to a port-hint guess

        status_code = ""
        parts = first_line.split(" ", 2)
        if len(parts) >= _HTTP_STATUS_PARTS_MIN:
            status_code = parts[1]
        server_header = _header(text, "Server")

        service = _classify_http(port, server_header)
        evidence: dict[str, Any] = {"status": status_code}
        if server_header:
            evidence["server_header"] = server_header
        return ServiceFingerprint(
            port=port,
            hint="http",
            service=service,
            evidence=evidence,
        )


_WHITESPACE_BYTES = (0x09, 0x0A, 0x0D)


def _looks_textual(b: bytes) -> bool:
    """Cheap "is this ASCII-ish" check for distinguishing plaintext from TLS noise."""
    if not b:
        return True
    sample = b[:32]
    printable = sum(
        1 for byte in sample if _PRINTABLE_LO <= byte < _PRINTABLE_HI or byte in _WHITESPACE_BYTES
    )
    return printable / len(sample) > _PRINTABLE_THRESHOLD


def _header(http_text: str, name: str) -> str | None:
    needle = f"\r\n{name.lower()}:"
    lower = http_text.lower()
    idx = lower.find(needle)
    if idx < 0:
        return None
    start = idx + len(needle)
    end = http_text.find("\r\n", start)
    return http_text[start : end if end >= 0 else None].strip()


_HTTP_GENERIC_PORTS = frozenset({80, 443, 8080})
# Port -> service when the Server header doesn't already classify it.
_PORT_TO_SERVICE: dict[int, str] = {
    _PROXMOX_PORT: "proxmox-ve",
    _COCKPIT_PORT: "cockpit",
    _K8S_API_PORT: "k8s-api",
}


def _classify_http(port: int, server_header: str | None) -> str | None:
    """Map (port, Server) onto a more specific service name when we can."""
    if server_header:
        s = server_header.lower()
        if "proxmox" in s:
            return "proxmox-ve"
        if "cockpit" in s:
            return "cockpit"
    if port in _PORT_TO_SERVICE:
        return _PORT_TO_SERVICE[port]
    if port in _HTTP_GENERIC_PORTS:
        return "http-generic"
    return None


__all__ = [
    "NetworkFingerprintOutput",
    "NetworkFingerprintProbe",
    "ServiceFingerprint",
]
