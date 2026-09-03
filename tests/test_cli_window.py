"""CLI tests for ``helper window open|list|revoke|kill`` and ``trust boundary``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.models import Host
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.trust import seed_domains

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture
async def trust_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Seeded domains and one host to hang a boundary on."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'window.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    monkeypatch.setenv("HOMELAB_HELPER_OPERATOR", "enoch")

    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)
    async with session_scope(sm) as s:
        await seed_domains(s)
        s.add(Host(hostname="node2"))
    await engine.dispose()
    return url


def test_window_open_and_list(trust_db: str) -> None:
    opened = runner.invoke(
        app,
        ["window", "open", "--reason", "array work", "--minutes", "60", "--domain", "storage"],
    )
    assert opened.exit_code == 0, opened.output
    assert "window open" in opened.output
    assert "no auto-renew" in opened.output

    listed = runner.invoke(app, ["window", "list"])
    assert listed.exit_code == 0
    assert "1 window(s)" in listed.output
    assert "array work" in listed.output


def test_window_open_refuses_blanket_scope(trust_db: str) -> None:
    result = runner.invoke(app, ["window", "open", "--reason", "just in case"])
    assert result.exit_code == 2
    assert "blanket" in result.output


def test_window_open_refuses_absolute_domain(trust_db: str) -> None:
    """AC6: secrets rejects every window."""
    result = runner.invoke(app, ["window", "open", "--reason", "rotate", "--domain", "secrets"])
    assert result.exit_code == 2
    assert "absolute" in result.output


def test_window_open_refuses_an_over_long_window(trust_db: str) -> None:
    result = runner.invoke(
        app,
        ["window", "open", "--reason", "forever", "--minutes", "10000", "--host", "node2"],
    )
    assert result.exit_code == 2
    assert "duration" in result.output


def test_window_revoke_closes_it(trust_db: str) -> None:
    opened = runner.invoke(app, ["window", "open", "--reason", "array work", "--host", "node2"])
    window_id = opened.output.split("window open")[1].split("—")[0].strip()

    revoked = runner.invoke(app, ["window", "revoke", window_id[:8]])
    assert revoked.exit_code == 0, revoked.output
    assert "revoked" in revoked.output

    listed = runner.invoke(app, ["window", "list"])
    assert "0 window(s)" in listed.output
    assert "no open windows" in listed.output

    with_all = runner.invoke(app, ["window", "list", "--all"])
    assert "1 window(s)" in with_all.output


def test_window_kill_revokes_everything(trust_db: str) -> None:
    for domain in ("storage", "dns", "containers"):
        runner.invoke(app, ["window", "open", "--reason", f"{domain} work", "--domain", domain])
    assert "3 window(s)" in runner.invoke(app, ["window", "list"]).output

    killed = runner.invoke(app, ["window", "kill", "--yes"])
    assert killed.exit_code == 0, killed.output
    assert "3 window(s) revoked" in killed.output
    assert "0 window(s)" in runner.invoke(app, ["window", "list"]).output


def test_window_kill_declined_changes_nothing(trust_db: str) -> None:
    runner.invoke(app, ["window", "open", "--reason", "array work", "--host", "node2"])
    result = runner.invoke(app, ["window", "kill"], input="n\n")
    assert result.exit_code == 3
    assert "1 window(s)" in runner.invoke(app, ["window", "list"]).output


def test_window_revoke_unknown_id(trust_db: str) -> None:
    result = runner.invoke(app, ["window", "revoke", "ffffffff"])
    assert result.exit_code == 1
    assert "no window matches" in result.output


def test_trust_boundary_sets_an_absolute_ceiling(trust_db: str) -> None:
    result = runner.invoke(
        app, ["trust", "boundary", "node2", "propose", "--absolute", "--notes", "the NAS"]
    )
    assert result.exit_code == 0, result.output
    assert "boundary set" in result.output
    assert "window-proof" in result.output

    shown = runner.invoke(app, ["trust", "show"])
    assert "node2" in shown.output
    assert "absolute" in shown.output

    history = runner.invoke(app, ["trust", "history"])
    assert "boundary-set" in history.output


def test_trust_boundary_unknown_host(trust_db: str) -> None:
    result = runner.invoke(app, ["trust", "boundary", "ghost", "propose"])
    assert result.exit_code == 1
    assert "no such host" in result.output


def test_trust_boundary_rejects_a_bad_level(trust_db: str) -> None:
    result = runner.invoke(app, ["trust", "boundary", "node2", "sideways"])
    assert result.exit_code != 0
    assert "ceiling must be one of" in result.output
