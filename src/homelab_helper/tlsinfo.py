"""Reading the certificate a TLS port presents. A leaf module: the fingerprint probe and the
``http-check`` assertion both use it, and neither may import the other."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from datetime import UTC, datetime
from typing import Any

from cryptography import x509
from cryptography.x509.oid import NameOID


def parse_tls_der(der: bytes, *, now: datetime | None = None) -> dict[str, Any]:
    """The same fields as :func:`parse_tls_cert`, from the certificate as the server sent it."""
    moment = now or datetime.now(UTC)
    cert = x509.load_der_x509_certificate(der)

    def _first(name: x509.Name, *oids: x509.ObjectIdentifier) -> str | None:
        for oid in oids:
            found = name.get_attributes_for_oid(oid)
            if found:
                return str(found[0].value)
        return None

    sans: list[str] = []
    try:
        alt = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        sans = [str(v) for v in alt.get_values_for_type(x509.DNSName)]
        sans += [str(v) for v in alt.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        pass
    expires = cert.not_valid_after_utc
    return {
        "subject": _first(cert.subject, NameOID.COMMON_NAME),
        "issuer": _first(cert.issuer, NameOID.COMMON_NAME, NameOID.ORGANIZATION_NAME),
        "sans": sans,
        "not_after": expires.isoformat(),
        "days_remaining": (expires - moment).days,
    }


async def inspect_tls(
    host: str, port: int, timeout_s: float, *, server_name: str | None = None
) -> dict[str, Any] | None:
    """Read the certificate a TLS port presents, whoever signed it and whatever name it carries.

    The handshake is deliberately unverified: an internal or self-signed certificate is exactly
    the kind whose expiry nobody else is watching, and a verifying client never gets to see it.
    Nothing is sent over the connection, so trusting the peer is not at stake.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=server_name),
            timeout=timeout_s,
        )
        ssl_obj = writer.get_extra_info("ssl_object")
        der = ssl_obj.getpeercert(binary_form=True) if ssl_obj is not None else None
        return parse_tls_der(der) if der else None
    except Exception:
        return None
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


__all__ = ["inspect_tls", "parse_tls_der"]
