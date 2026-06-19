"""Tests for the `helper config` verb."""

from __future__ import annotations

from typer.testing import CliRunner

from homelab_helper.cli.main import app

runner = CliRunner()


def test_config_shows_defaults_when_env_unset(monkeypatch) -> None:
    for var in (
        "HOMELAB_HELPER_DATABASE_URL",
        "HOMELAB_HELPER_NETBOX_URL",
        "HOMELAB_HELPER_NETBOX_TOKEN",
        "HOMELAB_HELPER_NETBOX_VERIFY_SSL",
        "HOMELAB_HELPER_SSH_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "database url" in result.stdout
    assert "homelab.db" in result.stdout  # default sqlite path
    assert "disabled until" in result.stdout  # NetBox unset hint


def test_config_reports_env_overrides_without_leaking_token(monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_NETBOX_URL", "https://netbox.example.lan")
    monkeypatch.setenv("HOMELAB_HELPER_NETBOX_TOKEN", "supersecret-token-value")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "netbox.example.lan" in result.stdout
    assert "supersecret-token-value" not in result.stdout  # never printed
    assert "set" in result.stdout
