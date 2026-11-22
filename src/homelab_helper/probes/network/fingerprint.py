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
import ssl
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

from homelab_helper.db.enums import FindingKind, FindingSeverity, PrivilegeLevel
from homelab_helper.engine.category_findings import CategoryIssue
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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

_TLS_PORTS = frozenset({443, 6443, 8006, 8443})
TLS_EXPIRY_WARN_DAYS = 30
TLS_EXPIRY_HIGH_DAYS = 7
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


def _rdn_field(rdn_seq: Any, key: str) -> str | None:
    if not isinstance(rdn_seq, tuple | list):
        return None
    for rdn in rdn_seq:
        if not isinstance(rdn, tuple | list):
            continue
        for pair in rdn:
            if isinstance(pair, tuple | list) and len(pair) == 2 and pair[0] == key:  # noqa: PLR2004
                return str(pair[1])
    return None


def parse_tls_cert(cert: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Extract ``subject``, ``issuer``, ``sans``, ``not_after``, and ``days_remaining`` from a decoded peer cert dict."""
    moment = now or datetime.now(UTC)
    subject = _rdn_field(cert.get("subject"), "commonName") or cert.get("subject_cn")
    issuer = (
        _rdn_field(cert.get("issuer"), "commonName")
        or _rdn_field(cert.get("issuer"), "organizationName")
        or cert.get("issuer_cn")
    )
    sans: list[str] = []
    for entry in cert.get("subjectAltName") or ():
        if (
            isinstance(entry, tuple | list)
            and len(entry) == 2  # noqa: PLR2004
            and entry[0] in ("DNS", "IP Address")
        ):
            sans.append(str(entry[1]))
    not_after_raw = cert.get("notAfter") or cert.get("not_after")
    days_remaining: int | None = cert.get("days_remaining")
    not_after_iso: str | None = None
    if isinstance(not_after_raw, str) and not_after_raw.strip():
        try:
            expires = datetime.strptime(not_after_raw.strip(), "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=UTC
            )
            not_after_iso = expires.isoformat()
            days_remaining = (expires - moment).days
        except ValueError:
            not_after_iso = not_after_raw.strip()
    out: dict[str, Any] = {
        "subject": subject,
        "issuer": issuer,
        "sans": sans,
    }
    if not_after_iso is not None:
        out["not_after"] = not_after_iso
    if days_remaining is not None:
        out["days_remaining"] = int(days_remaining)
    return out


async def _default_tls_inspector(ip: str, port: int, timeout_s: float) -> dict[str, Any] | None:
    ctx = ssl.create_default_context()
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port, ssl=ctx, server_hostname=ip),
            timeout=timeout_s,
        )
        ssl_obj = writer.get_extra_info("ssl_object")
        cert = ssl_obj.getpeercert() if ssl_obj is not None else None
        if isinstance(cert, dict) and cert:
            return parse_tls_cert(cert)
    except Exception:
        return None
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
    return None


def tls_expiry_issues(
    ip: str,
    services: list[dict[str, Any]],
    *,
    warn_days: int = TLS_EXPIRY_WARN_DAYS,
    high_days: int = TLS_EXPIRY_HIGH_DAYS,
) -> list[CategoryIssue]:
    """Emit ``tls-cert-expiring`` ``VERSION_DRIFT`` issues for fingerprinted TLS ports near expiry."""
    issues: list[CategoryIssue] = []
    for svc in services:
        port = svc.get("port")
        ev = svc.get("evidence") or {}
        tls = ev.get("tls")
        if not isinstance(tls, dict):
            continue
        days = tls.get("days_remaining")
        if not isinstance(days, int) or days > warn_days:
            continue
        subject = tls.get("subject") or f"{ip}:{port}"
        severity = FindingSeverity.HIGH if days <= high_days else FindingSeverity.MEDIUM
        title = (
            f"TLS certificate on {ip}:{port} ({subject}) expired {-days} day(s) ago"
            if days < 0
            else f"TLS certificate on {ip}:{port} ({subject}) expires in {days} day(s)"
        )
        issues.append(
            CategoryIssue(
                kind=FindingKind.VERSION_DRIFT,
                category="tls-cert-expiring",
                target_type="host",
                target_id=f"{ip}:{port}",
                severity=severity,
                title=title,
                description=(
                    f"Certificate for {subject} (issuer {tls.get('issuer') or 'unknown'}, "
                    f"SANs {', '.join(tls.get('sans') or []) or 'none'}) has {days} day(s) remaining."
                ),
                evidence={"ip": ip, "port": port, **tls},
            )
        )
    return issues


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
    version: ClassVar[str] = "0.2.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.NONE
    target_kinds: ClassVar[list[str]] = ["network", "host"]
    produces_keys: ClassVar[list[str]] = [
        "network.host.services",
        "network.host.service.*",
    ]
    output_schema: ClassVar[type[BaseModel] | None] = NetworkFingerprintOutput
    description: ClassVar[str | None] = (
        "Banner-grab + port heuristic + TLS certificate fingerprint for a single host. "
        "Identifies SSH, HTTP/HTTPS endpoints, Proxmox, Cockpit, K8s API, and TLS SANs/expiry."
    )

    def __init__(
        self,
        *,
        ports: list[int] | None = None,
        timeout_s: float | None = None,
        tls_inspector: Callable[[str, int, float], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> None:
        super().__init__()
        self._ports_override: list[int] | None = list(ports) if ports is not None else None
        self.timeout_s: float = timeout_s if timeout_s is not None else _DEFAULT_TIMEOUT_S
        self._tls_inspector = tls_inspector

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

    async def _inspect_tls(self, ip: str, port: int) -> dict[str, Any] | None:
        inspector = self._tls_inspector or _default_tls_inspector
        return await inspector(ip, port, self.timeout_s)

    async def _fingerprint_port(self, ip: str, port: int) -> ServiceFingerprint | None:
        """Return a ServiceFingerprint or ``None`` if nothing responded."""
        if self._tls_inspector is not None and port in _TLS_PORTS:
            tls_info = await self._inspect_tls(ip, port)
            if tls_info is not None:
                return ServiceFingerprint(
                    port=port,
                    hint="https",
                    service=_PORT_HINTS.get(port, "https"),
                    evidence={"tls": tls_info},
                )
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
            if port in _TLS_PORTS:
                tls_info = await self._inspect_tls(ip, port)
                if tls_info is not None:
                    evidence["tls"] = tls_info
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
        # If the server greeted us with binary noise (probably TLS), inspect its
        # TLS certificate if possible and record a TLS fingerprint.
        if already_read and not _looks_textual(already_read):
            ev: dict[str, Any] = {"reason": "binary greeting suggests TLS"}
            tls_info = await self._inspect_tls(ip, port)
            if tls_info is not None:
                ev["tls"] = tls_info
            return ServiceFingerprint(
                port=port,
                hint="tls",
                service=_PORT_HINTS.get(port, "unknown") if port != _HTTP_PORT else None,
                evidence=ev,
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
    "TLS_EXPIRY_HIGH_DAYS",
    "TLS_EXPIRY_WARN_DAYS",
    "NetworkFingerprintOutput",
    "NetworkFingerprintProbe",
    "ServiceFingerprint",
    "parse_tls_cert",
    "tls_expiry_issues",
]
