"""Per-user data/config directories — what makes an installed ``helper`` work
from any working directory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from homelab_helper.cli.config import render_env_template
from homelab_helper.cli.main import app
from homelab_helper.config import (
    SOURCES,
    config_dir,
    data_dir,
    database_url,
    default_database_url,
    find_env_files,
    sqlite_path,
)
from homelab_helper.engine.workloads import DEFAULT_LIBRARY_PATH, load_workload_library

runner = CliRunner()


def test_home_var_overrides_both_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_HOME", str(tmp_path / "one"))
    assert data_dir() == tmp_path / "one"
    assert config_dir() == tmp_path / "one"


def test_xdg_dirs_apply_without_home_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert data_dir() == tmp_path / "data" / "homelab-helper"
    assert config_dir() == tmp_path / "cfg" / "homelab-helper"


def test_defaults_fall_back_to_dot_local_and_dot_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert data_dir() == tmp_path / ".local" / "share" / "homelab-helper"
    assert config_dir() == tmp_path / ".config" / "homelab-helper"


def test_default_database_lives_in_data_dir_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_HOME", str(tmp_path))
    monkeypatch.delenv("HOMELAB_HELPER_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    assert sqlite_path(database_url()) == tmp_path / "homelab.db"
    assert default_database_url() == f"sqlite+aiosqlite:///{tmp_path / 'homelab.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", "postgresql+asyncpg://x/y")
    assert sqlite_path(database_url()) is None


def test_env_search_includes_config_dir_between_project_and_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cfg.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HOMELAB_HELPER_HOME", str(cfg))
    (repo / ".env").write_text("A=1\n")
    (cfg / ".env").write_text("A=2\n")
    (home / ".env").write_text("A=3\n")
    assert find_env_files(repo) == [repo / ".env", cfg / ".env", home / ".env"]
    # Outside any checkout, the per-user file is first.
    assert find_env_files(tmp_path / "elsewhere") == [cfg / ".env", home / ".env"]


def test_db_init_creates_the_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "fresh" / "nested"
    monkeypatch.setenv("HOMELAB_HELPER_HOME", str(home))
    monkeypatch.delenv("HOMELAB_HELPER_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["db", "init", "--skip-probe-sync"])
    assert result.exit_code == 0, result.stdout
    assert (home / "homelab.db").is_file()
    assert not (tmp_path / "homelab.db").exists()


def test_config_init_writes_template_and_refuses_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_HOME", str(tmp_path / "cfg"))
    first = runner.invoke(app, ["config", "init"])
    assert first.exit_code == 0, first.stdout
    target = tmp_path / "cfg" / ".env"
    assert target.is_file()
    text = target.read_text()
    for source in SOURCES:
        for var in (*source.required, *source.optional):
            assert f"# {var}=" in text
    assert "HOMELAB_HELPER_DATABASE_URL" in text
    assert "HOMELAB_HELPER_ANTHROPIC_API_KEY" in text
    second = runner.invoke(app, ["config", "init"])
    assert second.exit_code == 1
    assert "--force" in second.stdout  # short token: the path folds under CliRunner
    forced = runner.invoke(app, ["config", "init", "--force"])
    assert forced.exit_code == 0


def test_config_init_template_has_no_uncommented_assignments() -> None:
    for line in render_env_template().splitlines():
        if line.strip():
            assert line.startswith("#"), line


def test_config_shows_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_HOME", str(tmp_path))
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "data dir" in result.stdout
    assert "config dir" in result.stdout


def test_workload_library_ships_inside_the_package() -> None:
    assert DEFAULT_LIBRARY_PATH.parent.name == "data"
    assert DEFAULT_LIBRARY_PATH.parent.parent.name == "homelab_helper"
    assert DEFAULT_LIBRARY_PATH.is_file()
    assert "immich" in load_workload_library()


def test_root_help_offers_completion() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--install-completion" in result.stdout
