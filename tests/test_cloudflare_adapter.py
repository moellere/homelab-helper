"""CloudflareAdapter unit tests against httpx.MockTransport — no live API."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from homelab_helper.adapters.cloudflare import (
    CloudflareAdapter,
    CloudflareAPIError,
    CloudflareConfig,
    CloudflareConfigError,
    parse_dns_record,
)

_API_BASE = "https://api.cloudflare.com/client/v4"


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: CloudflareConfig | None = None,
) -> CloudflareAdapter:
    client = httpx.AsyncClient(base_url=_API_BASE, transport=httpx.MockTransport(handler))
    cfg = config or CloudflareConfig(api_token="tok", zone="example.com")
    return CloudflareAdapter(cfg, client=client)


def _ok(result: object) -> dict:
    """Cloudflare wraps every payload in a success envelope."""
    return {"success": True, "errors": [], "messages": [], "result": result}


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_from_env_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setenv("HOMELAB_HELPER_CLOUDFLARE_ZONE", "example.com")
    with pytest.raises(CloudflareConfigError):
        CloudflareConfig.from_env()


def test_config_from_env_requires_a_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.delenv("HOMELAB_HELPER_CLOUDFLARE_ZONE", raising=False)
    monkeypatch.delenv("HOMELAB_HELPER_CLOUDFLARE_ZONE_ID", raising=False)
    with pytest.raises(CloudflareConfigError):
        CloudflareConfig.from_env()


def test_config_from_env_accepts_zone_id_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.delenv("HOMELAB_HELPER_CLOUDFLARE_ZONE", raising=False)
    monkeypatch.setenv("HOMELAB_HELPER_CLOUDFLARE_ZONE_ID", "zone123")
    cfg = CloudflareConfig.from_env()
    assert cfg.zone_id == "zone123"


def test_adapter_requires_config_or_client() -> None:
    with pytest.raises(CloudflareConfigError):
        CloudflareAdapter()


# ---------------------------------------------------------------------------
# parse function (pure)
# ---------------------------------------------------------------------------


def test_parse_dns_record_maps_name_and_content() -> None:
    out = parse_dns_record(
        {"name": "ha.example.com", "content": "203.0.113.9", "type": "A", "proxied": True}
    )
    assert out["hostname"] == "ha.example.com"
    assert out["value"] == "203.0.113.9"
    assert out["record_type"] == "A"
    assert out["proxied"] is True
    assert out["enabled"] is True  # Cloudflare records are always live


def test_parse_dns_record_defaults_record_type_to_a() -> None:
    out = parse_dns_record({"name": "x.example.com", "content": "203.0.113.1"})
    assert out["record_type"] == "A"
    assert out["proxied"] is False


# ---------------------------------------------------------------------------
# reads over the mock transport
# ---------------------------------------------------------------------------


async def test_list_dns_records_resolves_zone_then_lists() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/client/v4/zones":
            assert request.url.params.get("name") == "example.com"
            return httpx.Response(200, json=_ok([{"id": "zoneABC", "name": "example.com"}]))
        if request.url.path == "/client/v4/zones/zoneABC/dns_records":
            return httpx.Response(
                200, json=_ok([{"name": "ha.example.com", "content": "203.0.113.9", "type": "A"}])
            )
        return httpx.Response(404, json={"success": False, "errors": [{"message": "nope"}]})

    adapter = _adapter(handler)
    try:
        records = await adapter.list_dns_records()
    finally:
        await adapter.aclose()
    assert records[0]["hostname"] == "ha.example.com"
    assert "/client/v4/zones" in seen[0]
    assert "/client/v4/zones/zoneABC/dns_records" in seen[1]


async def test_zone_id_config_skips_name_lookup() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_ok([]))

    cfg = CloudflareConfig(api_token="tok", zone_id="zoneXYZ")
    adapter = _adapter(handler, config=cfg)
    try:
        await adapter.list_dns_records()
    finally:
        await adapter.aclose()
    # No /zones name lookup — went straight to the records path.
    assert paths == ["/client/v4/zones/zoneXYZ/dns_records"]


async def test_list_dns_records_paginates() -> None:
    page_one = [
        {"name": f"h{i}.example.com", "content": "203.0.113.1", "type": "A"} for i in range(100)
    ]
    page_two = [{"name": "last.example.com", "content": "203.0.113.2", "type": "A"}]

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        return httpx.Response(200, json=_ok(page_one if page == "1" else page_two))

    cfg = CloudflareConfig(api_token="tok", zone_id="z")
    adapter = _adapter(handler, config=cfg)
    try:
        records = await adapter.list_dns_records()
    finally:
        await adapter.aclose()
    assert len(records) == 101
    assert records[-1]["hostname"] == "last.example.com"


async def test_success_false_envelope_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": False,
                "errors": [{"message": "Invalid access token"}],
                "result": None,
            },
        )

    cfg = CloudflareConfig(api_token="tok", zone_id="z")
    adapter = _adapter(handler, config=cfg)
    try:
        with pytest.raises(CloudflareAPIError) as excinfo:
            await adapter.list_dns_records()
    finally:
        await adapter.aclose()
    assert "Invalid access token" in str(excinfo.value)


async def test_non_2xx_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    cfg = CloudflareConfig(api_token="tok", zone_id="z")
    adapter = _adapter(handler, config=cfg)
    try:
        with pytest.raises(CloudflareAPIError) as excinfo:
            await adapter.list_dns_records()
    finally:
        await adapter.aclose()
    assert excinfo.value.status_code == 403


async def test_health_check_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tokens/verify"):
            return httpx.Response(200, json=_ok({"status": "active"}))
        return httpx.Response(200, json=_ok([{"id": "z", "name": "example.com"}]))

    adapter = _adapter(handler)
    try:
        ok, err = await adapter.health_check()
    finally:
        await adapter.aclose()
    assert ok is True
    assert err is None


async def test_health_check_reports_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad token")

    adapter = _adapter(handler)
    try:
        ok, err = await adapter.health_check()
    finally:
        await adapter.aclose()
    assert ok is False
    assert err is not None


# ---------------------------------------------------------------------------
# account-owned vs user tokens
# ---------------------------------------------------------------------------


def test_verify_path_depends_on_token_ownership() -> None:
    """Account-owned tokens (cfat_ prefix) verify under /accounts/<id>/.

    Asking /user/tokens/verify for one returns 401 "Invalid API Token" — a live,
    correctly-scoped token reported as invalid.
    """
    user_tok = CloudflareConfig(api_token="t", zone="example.com")
    assert user_tok.verify_path == "/user/tokens/verify"

    acct_tok = CloudflareConfig(api_token="cfat_x", zone="example.com", account_id="acct123")
    assert acct_tok.verify_path == "/accounts/acct123/tokens/verify"


def test_config_from_env_reads_account_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_CLOUDFLARE_API_TOKEN", "cfat_x")
    monkeypatch.setenv("HOMELAB_HELPER_CLOUDFLARE_ZONE", "example.com")
    monkeypatch.setenv("HOMELAB_HELPER_CLOUDFLARE_ACCOUNT_ID", "acct123")
    cfg = CloudflareConfig.from_env()
    assert cfg.account_id == "acct123"
    assert cfg.verify_path == "/accounts/acct123/tokens/verify"


async def test_health_check_uses_the_account_verify_path() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/tokens/verify"):
            return httpx.Response(200, json=_ok({"status": "active"}))
        return httpx.Response(200, json=_ok([{"id": "z1", "name": "example.com"}]))

    cfg = CloudflareConfig(api_token="cfat_x", zone="example.com", account_id="acct123")
    adapter = _adapter(handler, config=cfg)
    try:
        ok, err = await adapter.health_check()
    finally:
        await adapter.aclose()

    assert ok, err
    assert "/client/v4/accounts/acct123/tokens/verify" in seen


async def test_health_check_401_without_account_id_names_the_likely_cause() -> None:
    """The bare 401 is unactionable — say what to set."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"success": False, "errors": [{"code": 1000, "message": "Invalid API Token"}]}
        )

    cfg = CloudflareConfig(api_token="cfat_x", zone="example.com")  # no account id
    adapter = _adapter(handler, config=cfg)
    try:
        ok, err = await adapter.health_check()
    finally:
        await adapter.aclose()

    assert not ok
    assert err is not None
    assert "HOMELAB_HELPER_CLOUDFLARE_ACCOUNT_ID" in err
    assert "cfat_" in err
