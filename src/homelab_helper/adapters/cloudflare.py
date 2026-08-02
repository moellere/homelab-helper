"""Cloudflare adapter — read-only external-DNS source (Phase 3, L1).

The external half of the DNS split-brain. Where UniFi reports what a hostname
resolves to *inside* the network, Cloudflare reports what the same-named
service resolves to from *outside* it. Its A/AAAA records feed external
``ServiceEndpoint`` reconciliation (``scope=external``, ``resolver=cloudflare``);
paired against UniFi's internal endpoints, they make the split-brain explicit.

**Read-only at L1.** Cloudflare's API can obviously write DNS, but the harness
only reads until the Phase-6 trust gradient gates writes.

Auth is a scoped API token sent as a Bearer credential (``Zone.DNS:Read`` is
sufficient). The v4 API wraps every payload in
``{"success": ..., "errors": [...], "result": ..., "result_info": {...}}``;
:meth:`_request` checks ``success`` and unwraps ``result``. DNS reads are
zone-scoped, so the adapter resolves the configured zone *name* to its id once
and caches it (or takes an explicit zone id).

Configuration::

    HOMELAB_HELPER_CLOUDFLARE_API_TOKEN   <scoped token: Zone.DNS:Read>
    HOMELAB_HELPER_CLOUDFLARE_ZONE        example.com      (zone name)
    HOMELAB_HELPER_CLOUDFLARE_ZONE_ID     <zone id>        (optional; skips name lookup)

Tests inject an ``httpx.AsyncClient`` with ``MockTransport`` — no live API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

_HTTP_ERROR_THRESHOLD = 400
_HTTP_UNAUTHORIZED = 401
_API_BASE = "https://api.cloudflare.com/client/v4"
_PAGE_SIZE = 100
_MAX_PAGES = 50  # safety cap: 5000 records is far beyond any homelab zone


class CloudflareConfigError(RuntimeError):
    """Raised when required Cloudflare configuration is missing."""


class CloudflareAPIError(RuntimeError):
    """Non-2xx response, or an API envelope with ``success: false``."""

    def __init__(self, status_code: int, detail: str, *, method: str, path: str) -> None:
        super().__init__(f"Cloudflare {method} {path} -> {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class CloudflareConfig:
    api_token: str
    zone: str | None = None
    zone_id: str | None = None
    account_id: str | None = None
    timeout_s: float = 10.0

    @property
    def verify_path(self) -> str:
        """Where this token's ``tokens/verify`` lives.

        Account-owned tokens (the ``cfat_`` prefix Cloudflare now issues) verify
        under ``/accounts/<id>/``; classic user tokens under ``/user/``. Asking
        the wrong one returns 401 "Invalid API Token" for a perfectly live
        token, so the account id is what distinguishes them.
        """
        if self.account_id:
            return f"/accounts/{self.account_id}/tokens/verify"
        return "/user/tokens/verify"

    @classmethod
    def from_env(cls) -> CloudflareConfig:
        token = os.environ.get("HOMELAB_HELPER_CLOUDFLARE_API_TOKEN")
        zone = os.environ.get("HOMELAB_HELPER_CLOUDFLARE_ZONE")
        zone_id = os.environ.get("HOMELAB_HELPER_CLOUDFLARE_ZONE_ID")
        account_id = os.environ.get("HOMELAB_HELPER_CLOUDFLARE_ACCOUNT_ID")
        if not token:
            raise CloudflareConfigError(
                "A Cloudflare API token is required. Set HOMELAB_HELPER_CLOUDFLARE_API_TOKEN."
            )
        if not zone and not zone_id:
            raise CloudflareConfigError(
                "A zone is required. Set HOMELAB_HELPER_CLOUDFLARE_ZONE (name) or "
                "HOMELAB_HELPER_CLOUDFLARE_ZONE_ID."
            )
        return cls(api_token=token, zone=zone, zone_id=zone_id, account_id=account_id)


def parse_dns_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one Cloudflare ``/dns_records`` row into the stable record dict.

    Cloudflare names the FQDN ``name`` and the target ``content``; ``type`` is
    ``A`` / ``AAAA`` / ``CNAME`` / ``TXT`` / … Records are always live (there is
    no "disabled" state), so ``enabled`` is ``True``; ``proxied`` — whether the
    record fronts through Cloudflare's edge — is carried separately since it
    changes what "external IP" even means.
    """
    return {
        "hostname": raw.get("name"),
        "value": raw.get("content"),
        "record_type": raw.get("type") or "A",
        "enabled": True,
        "proxied": bool(raw.get("proxied")),
        "ttl": raw.get("ttl"),
    }


class CloudflareAdapter:
    """Read-only async client for the Cloudflare v4 API."""

    name = "cloudflare"

    def __init__(
        self,
        config: CloudflareConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if config is None and client is None:
            raise CloudflareConfigError("CloudflareAdapter needs a config or an injected client")
        self.config = config or CloudflareConfig(api_token="x", zone_id="injected")
        self._client = client
        self._owns_client = client is None
        self._zone_id_cache = self.config.zone_id

    @classmethod
    def from_env(cls) -> CloudflareAdapter:
        return cls(CloudflareConfig.from_env())

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_API_BASE,
            headers={
                "Authorization": f"Bearer {self.config.api_token}",
                "Accept": "application/json",
            },
            timeout=self.config.timeout_s,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> CloudflareAdapter:
        _ = self.client
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        response = await self.client.request(method, path, params=params)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            detail = response.text.strip()[:300] or response.reason_phrase
            raise CloudflareAPIError(response.status_code, detail, method=method, path=path)
        payload = response.json()
        if isinstance(payload, dict) and not payload.get("success", True):
            errors = payload.get("errors") or []
            detail = "; ".join(str(e.get("message", e)) for e in errors) or "success=false"
            raise CloudflareAPIError(response.status_code, detail, method=method, path=path)
        return payload.get("result") if isinstance(payload, dict) else payload

    async def _resolve_zone_id(self) -> str:
        """Return the zone id, resolving it from the zone name once and caching."""
        if self._zone_id_cache:
            return self._zone_id_cache
        if not self.config.zone:
            raise CloudflareConfigError("no zone or zone_id configured")
        rows = await self._request("GET", "/zones", params={"name": self.config.zone}) or []
        if not rows:
            raise CloudflareAPIError(
                404, f"zone {self.config.zone!r} not found", method="GET", path="/zones"
            )
        zone_id = str(rows[0].get("id"))
        self._zone_id_cache = zone_id
        return zone_id

    # ------------------------------------------------------------------ reads

    async def list_dns_records(self) -> list[dict[str, Any]]:
        """All DNS records for the configured zone (paginated)."""
        zone_id = await self._resolve_zone_id()
        path = f"/zones/{zone_id}/dns_records"
        out: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            rows = (
                await self._request("GET", path, params={"per_page": _PAGE_SIZE, "page": page})
                or []
            )
            out.extend(parse_dns_record(r) for r in rows)
            if len(rows) < _PAGE_SIZE:
                break
        return out

    async def health_check(self) -> tuple[bool, str | None]:
        """Verify the token is live and (if named) the zone resolves."""
        try:
            await self._request("GET", self.config.verify_path)
            await self._resolve_zone_id()
        except CloudflareAPIError as exc:
            if exc.status_code == _HTTP_UNAUTHORIZED and not self.config.account_id:
                return False, (
                    f"{exc} — if this is an account-owned token (the 'cfat_' prefix), it "
                    "verifies under /accounts/<id>/ rather than /user/. Set "
                    "HOMELAB_HELPER_CLOUDFLARE_ACCOUNT_ID."
                )
            return False, str(exc)
        except (CloudflareConfigError, httpx.HTTPError) as exc:
            return False, str(exc)
        return True, None


__all__ = [
    "CloudflareAPIError",
    "CloudflareAdapter",
    "CloudflareConfig",
    "CloudflareConfigError",
    "parse_dns_record",
]
