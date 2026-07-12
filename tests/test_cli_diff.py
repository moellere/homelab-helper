"""CLI tests for `helper diff git-vs-cluster`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from typer.testing import CliRunner

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

import homelab_helper.cli.diff as cli_diff
from homelab_helper.adapters.argocd import ArgoCDAdapter, ArgoCDConfig
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.session import make_engine

runner = CliRunner()


def _argocd(handler: Callable[[httpx.Request], httpx.Response]) -> ArgoCDAdapter:
    client = httpx.AsyncClient(
        base_url="https://argo.lan/api/v1", transport=httpx.MockTransport(handler)
    )
    return ArgoCDAdapter(ArgoCDConfig(url="https://argo.lan", api_token="tok"), client=client)


def _app(name: str, *, sync: str = "Synced", health: str = "Healthy", oos=None) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {
            "source": {"repoURL": "https://git/hl.git", "targetRevision": "main"},
            "destination": {"namespace": name},
        },
        "status": {
            "sync": {"status": sync},
            "health": {"status": health},
            "resources": oos or [],
        },
    }


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/session/userinfo"):
        return httpx.Response(200, json={"loggedIn": True})
    if request.url.path.endswith("/applications"):
        return httpx.Response(
            200,
            json={
                "items": [
                    _app("grafana"),
                    _app(
                        "loki",
                        sync="OutOfSync",
                        health="Degraded",
                        oos=[{"kind": "Deployment", "name": "loki", "status": "OutOfSync"}],
                    ),
                ]
            },
        )
    return httpx.Response(404, text="nope")


def _healthy_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/session/userinfo"):
        return httpx.Response(200, json={"loggedIn": True})
    if request.url.path.endswith("/applications"):
        return httpx.Response(200, json={"items": [_app("grafana")]})
    return httpx.Response(404, text="nope")


def _empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    import asyncio

    url = f"sqlite+aiosqlite:///{tmp_path / 'diff.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    async def _init() -> None:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_init())
    return url


def test_diff_lists_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_diff, "_load_argocd_adapter", lambda: _argocd(_handler))
    result = runner.invoke(app, ["diff", "git-vs-cluster"])
    assert result.exit_code == 0
    assert "2 application(s)" in result.stdout
    assert "1 drifted" in result.stdout
    assert "loki" in result.stdout


def test_diff_no_drift_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_diff, "_load_argocd_adapter", lambda: _argocd(_healthy_handler))
    result = runner.invoke(app, ["diff", "git-vs-cluster"])
    assert result.exit_code == 0
    assert "no drift" in result.stdout.lower()


def test_diff_persist_opens_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _empty_db(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_diff, "_load_argocd_adapter", lambda: _argocd(_handler))
    result = runner.invoke(app, ["diff", "git-vs-cluster", "--persist"])
    assert result.exit_code == 0
    assert "1 opened" in result.stdout
    # The finding shows up in `helper findings list`.
    listed = runner.invoke(app, ["findings", "list"])
    assert listed.exit_code == 0
    assert "loki" in listed.stdout.lower() or "drift" in listed.stdout.lower()


def test_diff_dry_run_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _empty_db(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_diff, "_load_argocd_adapter", lambda: _argocd(_handler))
    result = runner.invoke(app, ["diff", "git-vs-cluster", "--persist", "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.stdout.lower()
    listed = runner.invoke(app, ["findings", "list"])
    # Nothing persisted — no drift finding present.
    assert "drift" not in listed.stdout.lower()


def test_diff_unreachable_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    monkeypatch.setattr(cli_diff, "_load_argocd_adapter", lambda: _argocd(down))
    result = runner.invoke(app, ["diff", "git-vs-cluster"])
    assert result.exit_code == 1
    assert "unreachable" in result.stdout.lower()


def test_diff_help_loads() -> None:
    result = runner.invoke(app, ["diff", "git-vs-cluster", "--help"])
    assert result.exit_code == 0
