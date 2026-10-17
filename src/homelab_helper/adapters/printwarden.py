"""print-warden adapter — read-only 3D-printer manager source.

print-warden (github.com/moellere/print-warden) slices, queues and watches Klipper printers. Its unauthenticated
``GET /healthz`` is a versioned status contract: per printer whether Moonraker is connected, Klipper's state, the
print state and progress, bed state, queue length and how long since the last telemetry update. Only api 1 is
understood; a newer api is refused rather than misread. Configuration: ``HOMELAB_HELPER_PRINTWARDEN_URL`` and
optional ``HOMELAB_HELPER_PRINTWARDEN_VERIFY_SSL``. No credentials: the endpoint is open by design and carries no
file or job names. Tests inject an ``httpx.AsyncClient`` on a ``MockTransport``.
"""

from __future__ import annotations

import os
from typing import Any, cast

import httpx

SUPPORTED_API = 1


class PrintWardenConfigError(RuntimeError):
    """Raised when required configuration is missing."""


class PrintWardenAPIError(RuntimeError):
    """Raised when the status contract is missing or a version this adapter does not know."""


class PrintWardenAdapter:
    def __init__(
        self,
        url: str,
        *,
        verify_ssl: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.url, verify=verify_ssl, timeout=10.0
        )

    @classmethod
    def from_env(cls) -> PrintWardenAdapter:
        url = os.environ.get("HOMELAB_HELPER_PRINTWARDEN_URL")
        if not url:
            raise PrintWardenConfigError("HOMELAB_HELPER_PRINTWARDEN_URL is required")
        verify = os.environ.get("HOMELAB_HELPER_PRINTWARDEN_VERIFY_SSL", "true").lower() != "false"
        return cls(url, verify_ssl=verify)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def health_check(self) -> tuple[bool, str | None]:
        try:
            await self.status()
        except (httpx.HTTPError, PrintWardenAPIError) as exc:
            return False, str(exc)
        return True, None

    async def status(self) -> dict[str, Any]:
        resp = await self._client.get("/healthz")
        resp.raise_for_status()
        body = cast("dict[str, Any]", resp.json())
        api = body.get("api")
        if api != SUPPORTED_API:
            raise PrintWardenAPIError(
                f"print-warden status api {api!r} is not supported (expected {SUPPORTED_API}); "
                "upgrade homelab-helper or print-warden"
            )
        if not isinstance(body.get("printers"), dict):
            raise PrintWardenAPIError("print-warden status has no printers map")
        return body
