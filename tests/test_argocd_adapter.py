"""ArgoCDAdapter unit tests against httpx.MockTransport — no live server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from homelab_helper.adapters.argocd import (
    ArgoCDAdapter,
    ArgoCDAPIError,
    ArgoCDConfig,
    ArgoCDConfigError,
    application_is_drifted,
    parse_application,
)

_CONFIG = ArgoCDConfig(url="https://argocd.example.lan", api_token="tok")


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> ArgoCDAdapter:
    client = httpx.AsyncClient(
        base_url=_CONFIG.url.rstrip("/") + "/api/v1",
        transport=httpx.MockTransport(handler),
    )
    return ArgoCDAdapter(_CONFIG, client=client)


def _app(
    name: str,
    *,
    sync: str = "Synced",
    health: str = "Healthy",
    resources: list[dict] | None = None,
) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {
            "source": {
                "repoURL": "https://git.example/homelab.git",
                "path": f"apps/{name}",
                "targetRevision": "main",
            },
            "destination": {"namespace": name, "server": "https://kubernetes.default.svc"},
        },
        "status": {
            "sync": {"status": sync},
            "health": {"status": health},
            "resources": resources or [],
        },
    }


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_from_env_requires_url_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_ARGOCD_URL", raising=False)
    monkeypatch.delenv("HOMELAB_HELPER_ARGOCD_API_TOKEN", raising=False)
    with pytest.raises(ArgoCDConfigError):
        ArgoCDConfig.from_env()


def test_config_from_env_defaults_verify_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_ARGOCD_URL", "https://a.lan")
    monkeypatch.setenv("HOMELAB_HELPER_ARGOCD_API_TOKEN", "tok")
    monkeypatch.delenv("HOMELAB_HELPER_ARGOCD_VERIFY_SSL", raising=False)
    cfg = ArgoCDConfig.from_env()
    assert cfg.verify_ssl is False


def test_adapter_requires_config_or_client() -> None:
    with pytest.raises(ArgoCDConfigError):
        ArgoCDAdapter()


# ---------------------------------------------------------------------------
# parse function (pure)
# ---------------------------------------------------------------------------


def test_parse_application_pulls_git_and_verdict() -> None:
    out = parse_application(_app("grafana"))
    assert out["name"] == "grafana"
    assert out["namespace"] == "grafana"
    assert out["repo_url"] == "https://git.example/homelab.git"
    assert out["path"] == "apps/grafana"
    assert out["target_revision"] == "main"
    assert out["sync_status"] == "Synced"
    assert out["health_status"] == "Healthy"
    assert out["out_of_sync_resources"] == []


def test_parse_application_lists_out_of_sync_resources() -> None:
    raw = _app(
        "prometheus",
        sync="OutOfSync",
        resources=[
            {"kind": "Deployment", "name": "prom", "namespace": "mon", "status": "OutOfSync"},
            {"kind": "Service", "name": "prom", "namespace": "mon", "status": "Synced"},
        ],
    )
    out = parse_application(raw)
    assert len(out["out_of_sync_resources"]) == 1
    assert out["out_of_sync_resources"][0]["kind"] == "Deployment"


def test_parse_application_tolerates_missing_status() -> None:
    out = parse_application({"metadata": {"name": "bare"}})
    assert out["name"] == "bare"
    assert out["sync_status"] is None
    assert out["out_of_sync_resources"] == []


def test_application_is_drifted_on_out_of_sync() -> None:
    assert application_is_drifted(parse_application(_app("x", sync="OutOfSync")))


def test_application_is_drifted_on_unhealthy() -> None:
    assert application_is_drifted(parse_application(_app("x", health="Degraded")))


def test_application_not_drifted_when_synced_and_healthy() -> None:
    assert not application_is_drifted(parse_application(_app("x")))


# ---------------------------------------------------------------------------
# reads over the mock transport
# ---------------------------------------------------------------------------


async def test_list_applications_unwraps_items() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200, json={"items": [_app("grafana"), _app("loki", sync="OutOfSync")]}
        )

    adapter = _adapter(handler)
    try:
        apps = await adapter.list_applications()
    finally:
        await adapter.aclose()
    assert seen["path"] == "/api/v1/applications"
    assert {a["name"] for a in apps} == {"grafana", "loki"}


async def test_list_applications_handles_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": None})

    adapter = _adapter(handler)
    try:
        apps = await adapter.list_applications()
    finally:
        await adapter.aclose()
    assert apps == []


async def test_api_error_raised_on_non_2xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthenticated")

    adapter = _adapter(handler)
    try:
        with pytest.raises(ArgoCDAPIError) as excinfo:
            await adapter.list_applications()
    finally:
        await adapter.aclose()
    assert excinfo.value.status_code == 401


async def test_health_check_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/session/userinfo")
        return httpx.Response(200, json={"loggedIn": True, "username": "admin"})

    adapter = _adapter(handler)
    try:
        ok, err = await adapter.health_check()
    finally:
        await adapter.aclose()
    assert ok is True
    assert err is None


async def test_health_check_reports_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    adapter = _adapter(handler)
    try:
        ok, err = await adapter.health_check()
    finally:
        await adapter.aclose()
    assert ok is False
    assert err is not None
