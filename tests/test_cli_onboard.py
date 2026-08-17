"""CLI tests for `helper chat onboard` — scripted fake router, real DB writes."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from sqlalchemy import select
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

import homelab_helper.cli.chat as cli_chat
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.models import Host
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.llm.router import CapabilityTier, LLMRouter, PrivacyPolicy, RouterResult

runner = CliRunner()


def _turn(say: str, proposal: dict | None = None, *, done: bool = False) -> str:
    return json.dumps({"say": say, "proposal": proposal, "done": done})


_PROPOSAL = {
    "hostname": "minipc1",
    "primary_ip": "10.0.6.27",
    "role": "k8s-node",
    "arch": "amd64",
    "ssh_user": "ops",
    "ssh_key_path": "~/.ssh/id_ed25519",
    "notes": None,
}


class ScriptedRouter(LLMRouter):
    """Plays back a fixed sequence of model replies; records the history."""

    def __init__(self, replies: list[str]) -> None:
        super().__init__([], PrivacyPolicy.PREFER_LOCAL)
        self.replies = list(replies)
        self.histories: list[list[dict[str, str]]] = []
        self.systems: list[str] = []

    async def complete(self, task, system, messages, *, min_tier=None) -> RouterResult:  # type: ignore[override]
        self.histories.append(list(messages))
        self.systems.append(system)
        text = self.replies.pop(0) if self.replies else _turn("Anything else?", done=True)
        return RouterResult(
            text=text, backend="fake", model="fake", tier=CapabilityTier.SMALL, local=True
        )


def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, seed_host: bool = False) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'onboard.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    async def _init() -> None:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        if seed_host:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as s:
                s.add(Host(hostname="minipc1", primary_ip="10.0.6.27"))
        await engine.dispose()

    asyncio.run(_init())
    return url


def _hosts(url: str) -> list[str]:
    async def _q() -> list[str]:
        engine = make_engine(url)
        sm = make_sessionmaker(engine)
        async with sm() as s:
            names = [h.hostname for h in (await s.execute(select(Host))).scalars().all()]
        await engine.dispose()
        return names

    return asyncio.run(_q())


def test_onboard_full_interview_registers_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _db(tmp_path, monkeypatch)
    router = ScriptedRouter(
        [
            _turn("What's the hostname and IP?"),
            _turn("Ready to register.", _PROPOSAL),
            _turn("Done! minipc1 is registered.", done=True),
        ]
    )
    monkeypatch.setattr(cli_chat, "_load_router", lambda: router)
    result = runner.invoke(
        app,
        ["onboard", "a new mini-PC"],
        input="minipc1 at 10.0.6.27, k8s node, ssh ops key ~/.ssh/id_ed25519\ny\n",
    )
    assert result.exit_code == 0
    assert "registered" in result.stdout
    assert _hosts(url) == ["minipc1"]
    # Post-registration next step names the discover command with the creds.
    assert "helper discover host minipc1" in result.stdout
    # The registration confirmation flowed back to the model.
    assert any("registered minipc1" in m["content"] for m in router.histories[-1])


def test_onboard_decline_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = _db(tmp_path, monkeypatch)
    router = ScriptedRouter(
        [
            _turn("Ready to register.", _PROPOSAL),
            _turn("Okay, tell me what to change.", done=True),
        ]
    )
    monkeypatch.setattr(cli_chat, "_load_router", lambda: router)
    result = runner.invoke(app, ["onboard", "add minipc1"], input="n\n")
    assert result.exit_code == 0
    assert _hosts(url) == []


def test_onboard_duplicate_is_rejected_and_fed_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _db(tmp_path, monkeypatch, seed_host=True)
    router = ScriptedRouter(
        [
            _turn("Ready to register.", _PROPOSAL),  # duplicates the seeded host
            _turn("That host already exists; stopping.", done=True),
        ]
    )
    monkeypatch.setattr(cli_chat, "_load_router", lambda: router)
    result = runner.invoke(app, ["onboard", "add minipc1"], input="")
    assert result.exit_code == 0
    assert "rejected" in result.stdout
    assert _hosts(url) == ["minipc1"]  # only the seeded row
    # The rejection reason was fed back to the model as a user message.
    assert any("already exists" in m["content"] for m in router.histories[-1])


def test_onboard_protocol_junk_gets_corrective_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _db(tmp_path, monkeypatch)
    router = ScriptedRouter(
        [
            "Sure, I'd be happy to help you add a host!",  # not protocol JSON
            _turn("What's the hostname?"),
            _turn("Bye.", done=True),
        ]
    )
    monkeypatch.setattr(cli_chat, "_load_router", lambda: router)
    result = runner.invoke(app, ["onboard", "add a host"], input="never mind, exit\n")
    assert result.exit_code == 0
    corrective = router.histories[1][-1]["content"]
    assert "Protocol error" in corrective
    assert _hosts(url) == []


def test_onboard_persistent_junk_bails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _db(tmp_path, monkeypatch)
    router = ScriptedRouter(["junk one", "junk two", "junk three", "junk four"])
    monkeypatch.setattr(cli_chat, "_load_router", lambda: router)
    result = runner.invoke(app, ["onboard", "add a host"], input="")
    assert result.exit_code == 2
    assert "protocol" in result.stdout.lower()


def test_onboard_existing_hosts_in_system_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db(tmp_path, monkeypatch, seed_host=True)
    router = ScriptedRouter([_turn("Hi — what host?", done=True)])
    monkeypatch.setattr(cli_chat, "_load_router", lambda: router)
    runner.invoke(app, ["onboard", "hello"], input="")
    # The seeded host appears in EXISTING HOSTS so the model can catch dupes.
    assert "minipc1" in router.systems[0]
    assert router.histories[0][0]["content"] == "hello"
