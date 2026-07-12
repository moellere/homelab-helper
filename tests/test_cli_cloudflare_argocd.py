"""CLI tests for `helper discover cloudflare` and `helper discover argocd`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

import homelab_helper.cli.discover as cli_discover
from homelab_helper.adapters.argocd import ArgoCDAdapter, ArgoCDConfig
from homelab_helper.adapters.cloudflare import CloudflareAdapter, CloudflareConfig
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.session import make_engine

runner = CliRunner()

_CF_BASE = "https://api.cloudflare.com/client/v4"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cloudflare(handler: Callable[[httpx.Request], httpx.Response]) -> CloudflareAdapter:
    client = httpx.AsyncClient(base_url=_CF_BASE, transport=httpx.MockTransport(handler))
    return CloudflareAdapter(CloudflareConfig(api_token="tok", zone_id="z"), client=client)


def _argocd(handler: Callable[[httpx.Request], httpx.Response]) -> ArgoCDAdapter:
    client = httpx.AsyncClient(
        base_url="https://argo.lan/api/v1", transport=httpx.MockTransport(handler)
    )
    return ArgoCDAdapter(ArgoCDConfig(url="https://argo.lan", api_token="tok"), client=client)


def _cf_ok(result: object) -> dict:
    return {"success": True, "errors": [], "result": result}


def _cf_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/tokens/verify"):
        return httpx.Response(200, json=_cf_ok({"status": "active"}))
    if request.url.path.endswith("/dns_records"):
        return httpx.Response(
            200,
            json=_cf_ok([{"name": "ha.example.com", "content": "203.0.113.9", "type": "A"}]),
        )
    return httpx.Response(200, json=_cf_ok([]))


def _argo_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/session/userinfo"):
        return httpx.Response(200, json={"loggedIn": True})
    if request.url.path.endswith("/applications"):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {"name": "grafana"},
                        "spec": {
                            "source": {"repoURL": "https://git/hl.git", "targetRevision": "main"},
                            "destination": {"namespace": "grafana"},
                        },
                        "status": {
                            "sync": {"status": "Synced"},
                            "health": {"status": "Healthy"},
                        },
                    },
                    {
                        "metadata": {"name": "loki"},
                        "spec": {
                            "source": {"repoURL": "https://git/hl.git", "targetRevision": "main"},
                            "destination": {"namespace": "loki"},
                        },
                        "status": {
                            "sync": {"status": "OutOfSync"},
                            "health": {"status": "Degraded"},
                            "resources": [
                                {"kind": "Deployment", "name": "loki", "status": "OutOfSync"}
                            ],
                        },
                    },
                ]
            },
        )
    return httpx.Response(404, text="nope")


@pytest.fixture
async def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cf.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return url


# ---------------------------------------------------------------------------
# discover cloudflare
# ---------------------------------------------------------------------------


def test_discover_cloudflare_prints_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_discover, "_load_cloudflare_adapter", lambda: _cloudflare(_cf_handler))
    result = runner.invoke(app, ["discover", "cloudflare"])
    assert result.exit_code == 0
    assert "1 DNS record" in result.stdout
    assert "ha.example.com" in result.stdout


def test_discover_cloudflare_unreachable_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad token")

    monkeypatch.setattr(cli_discover, "_load_cloudflare_adapter", lambda: _cloudflare(down))
    result = runner.invoke(app, ["discover", "cloudflare"])
    assert result.exit_code == 1
    assert "unreachable" in result.stdout.lower()


def test_discover_cloudflare_persist_creates_external_endpoints(
    empty_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_discover, "_load_cloudflare_adapter", lambda: _cloudflare(_cf_handler))
    result = runner.invoke(app, ["discover", "cloudflare", "--persist"])
    assert result.exit_code == 0
    assert "1 created" in result.stdout
    assert "external" in result.stdout
    # A subsequent view finds the external endpoint.
    v = runner.invoke(app, ["view", "service", "ha.example.com"])
    assert v.exit_code == 0
    assert "external" in v.stdout


def test_discover_cloudflare_dry_run_rolls_back(
    empty_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_discover, "_load_cloudflare_adapter", lambda: _cloudflare(_cf_handler))
    result = runner.invoke(app, ["discover", "cloudflare", "--persist", "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.stdout.lower()
    v = runner.invoke(app, ["view", "service", "ha.example.com"])
    assert v.exit_code == 2  # nothing persisted


# ---------------------------------------------------------------------------
# discover argocd
# ---------------------------------------------------------------------------


def test_discover_argocd_lists_and_flags_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_discover, "_load_argocd_adapter", lambda: _argocd(_argo_handler))
    result = runner.invoke(app, ["discover", "argocd"])
    assert result.exit_code == 0
    assert "2 application(s)" in result.stdout
    assert "1 drifted" in result.stdout
    assert "grafana" in result.stdout
    assert "loki" in result.stdout


def test_discover_argocd_unreachable_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    monkeypatch.setattr(cli_discover, "_load_argocd_adapter", lambda: _argocd(down))
    result = runner.invoke(app, ["discover", "argocd"])
    assert result.exit_code == 1
    assert "unreachable" in result.stdout.lower()


@pytest.mark.parametrize(
    "argv",
    [["discover", "cloudflare", "--help"], ["discover", "argocd", "--help"]],
)
def test_new_discover_help_loads(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0
