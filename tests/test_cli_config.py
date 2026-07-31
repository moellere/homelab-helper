"""Tests for the `helper config` verb."""

from __future__ import annotations

from typer.testing import CliRunner

from homelab_helper.cli.main import app
from homelab_helper.config import find_env_files

runner = CliRunner()

_ALL_VARS = (
    "HOMELAB_HELPER_DATABASE_URL",
    "HOMELAB_HELPER_NETBOX_URL",
    "HOMELAB_HELPER_NETBOX_TOKEN",
    "HOMELAB_HELPER_NETBOX_VERIFY_SSL",
    "HOMELAB_HELPER_SSH_KEY",
    "HOMELAB_HELPER_PROXMOX_URL",
    "HOMELAB_HELPER_PROXMOX_TOKEN_ID",
    "HOMELAB_HELPER_PROXMOX_TOKEN_SECRET",
    "HOMELAB_HELPER_UNIFI_URL",
    "HOMELAB_HELPER_UNIFI_API_KEY",
    "HOMELAB_HELPER_CLOUDFLARE_API_TOKEN",
    "HOMELAB_HELPER_ARGOCD_URL",
    "HOMELAB_HELPER_ARGOCD_API_TOKEN",
    "HOMELAB_HELPER_OMV_URL",
    "HOMELAB_HELPER_OMV_USERNAME",
    "HOMELAB_HELPER_OMV_PASSWORD",
)


def _clear(monkeypatch) -> None:
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)


def test_config_shows_defaults_when_env_unset(monkeypatch) -> None:
    _clear(monkeypatch)
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "database url" in result.stdout
    assert "homelab.db" in result.stdout  # default sqlite path
    assert "not configured" in result.stdout  # sources without credentials


def test_config_reports_env_overrides_without_leaking_secrets(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("HOMELAB_HELPER_NETBOX_URL", "https://netbox.example.lan")
    monkeypatch.setenv("HOMELAB_HELPER_NETBOX_TOKEN", "supersecret-token-value")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_URL", "https://unifi.example.lan")
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_API_KEY", "supersecret-unifi-key")
    result = runner.invoke(app, ["config"])

    assert result.exit_code == 0
    # Non-secret endpoints are shown — that's the point of the view.
    assert "netbox.example.lan" in result.stdout
    # Secret values are never rendered, only their presence.
    assert "supersecret-token-value" not in result.stdout
    assert "supersecret-unifi-key" not in result.stdout
    assert "set" in result.stdout


def test_config_marks_source_ready_only_when_all_required_vars_present(monkeypatch) -> None:
    _clear(monkeypatch)
    # URL without the API key is not enough for UniFi.
    monkeypatch.setenv("HOMELAB_HELPER_UNIFI_URL", "https://unifi.example.lan")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "unifi_api_key" in result.stdout  # named as missing


def test_env_file_search_is_bounded_by_the_project_root(tmp_path, monkeypatch) -> None:
    """A stray .env above the repo must not be loaded — only the project's own
    file, or the explicitly-supported ~/.env."""
    outer = tmp_path / "outer"
    repo = outer / "repo"
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    (outer / ".env").write_text("HOMELAB_HELPER_NETBOX_URL=https://should-not-load\n")
    (repo / ".git").mkdir()

    found = find_env_files(nested)
    assert (outer / ".env") not in found

    # The project's own .env is found from a nested directory.
    (repo / ".env").write_text("HOMELAB_HELPER_NETBOX_URL=https://project\n")
    assert find_env_files(nested)[0] == repo / ".env"
