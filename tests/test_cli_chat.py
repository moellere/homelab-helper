"""CLI tests for `helper chat` and `helper findings narrate` — fake router."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

import homelab_helper.cli.chat as cli_chat
import homelab_helper.cli.findings as cli_findings
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.db.models import Host, ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.llm.router import (
    CapabilityTier,
    LLMRouter,
    PrivacyPolicy,
    RouterResult,
    TaskClass,
)

runner = CliRunner()


class EchoRouter(LLMRouter):
    """Records the prompt; replies with a canned line. No backends, no HTTP."""

    def __init__(self, reply: str = "you have 1 host: node0") -> None:
        super().__init__([], PrivacyPolicy.PREFER_LOCAL)
        self.reply = reply
        self.systems: list[str] = []
        self.messages: list[list[dict[str, str]]] = []

    async def complete(self, task, system, messages, *, min_tier=None) -> RouterResult:  # type: ignore[override]
        self.systems.append(system)
        self.messages.append(list(messages))
        return RouterResult(
            text=self.reply,
            backend="fake",
            model="fake-model",
            tier=CapabilityTier.SMALL,
            local=True,
        )


class RefusingRouter(LLMRouter):
    def __init__(self) -> None:
        super().__init__([], PrivacyPolicy.STRICT_LOCAL)

    async def complete(self, task, system, messages, *, min_tier=None) -> RouterResult:  # type: ignore[override]
        from homelab_helper.llm.router import RouterRefusal

        raise RouterRefusal(TaskClass.CHAT, CapabilityTier.SMALL, self.policy, ["no backends"])


def _seed_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    async def _init() -> None:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as s:
            s.add(Host(hostname="node0", primary_ip="10.0.1.20"))
            s.add(
                ReconciliationFinding(
                    kind=FindingKind.STRAY_CONFIG,
                    severity=FindingSeverity.LOW,
                    fingerprint="feedfacecafe0000",
                    title="Stray config: IoT",
                    description="network IoT has no clients",
                )
            )
        await engine.dispose()

    asyncio.run(_init())


# ---------------------------------------------------------------------------
# helper chat
# ---------------------------------------------------------------------------


def test_chat_one_shot_grounds_in_lab_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_db(tmp_path, monkeypatch)
    router = EchoRouter()
    monkeypatch.setattr(cli_chat, "_load_router", lambda: router)
    result = runner.invoke(app, ["chat", "what hosts do I have?"])
    assert result.exit_code == 0
    assert "you have 1 host: node0" in result.stdout
    # The system prompt carried the reconciled facts (AC1 grounding).
    assert "node0" in router.systems[0]
    assert "feedfacecafe0000" in router.systems[0]
    assert "L1" in router.systems[0] or "never changes" in router.systems[0]
    # Transparency footer names the backend.
    assert "fake-model" in result.stdout


def test_chat_refusal_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_db(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_chat, "_load_router", RefusingRouter)
    result = runner.invoke(app, ["chat", "plan my lab"])
    assert result.exit_code == 2
    assert "refused" in result.stdout.lower()


def test_chat_repl_answers_then_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_db(tmp_path, monkeypatch)
    router = EchoRouter(reply="repl answer")
    monkeypatch.setattr(cli_chat, "_load_router", lambda: router)
    result = runner.invoke(app, ["chat"], input="what hosts?\nexit\n")
    assert result.exit_code == 0
    assert "repl answer" in result.stdout
    assert len(router.messages) == 1  # one question answered before exit


def test_chat_repl_keeps_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_db(tmp_path, monkeypatch)
    router = EchoRouter(reply="answer")
    monkeypatch.setattr(cli_chat, "_load_router", lambda: router)
    result = runner.invoke(app, ["chat"], input="first?\nsecond?\nexit\n")
    assert result.exit_code == 0
    # Second turn carries the first exchange (user, assistant, user).
    assert len(router.messages[1]) == 3


def test_chat_privacy_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_db(tmp_path, monkeypatch)
    router = EchoRouter()
    monkeypatch.setattr(cli_chat, "_load_router", lambda: router)
    result = runner.invoke(app, ["chat", "--privacy", "strict-local", "hi"])
    assert result.exit_code == 0
    assert router.policy == PrivacyPolicy.STRICT_LOCAL


# ---------------------------------------------------------------------------
# helper findings narrate
# ---------------------------------------------------------------------------


def test_findings_narrate_prose_and_footer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_db(tmp_path, monkeypatch)
    router = EchoRouter(reply="The IoT network is configured but unused.")
    monkeypatch.setattr(cli_findings, "_load_router", lambda: router)
    result = runner.invoke(app, ["findings", "narrate"])
    assert result.exit_code == 0
    assert "configured but unused" in result.stdout
    assert "1 finding(s)" in result.stdout
    # The narrator prompt carried the finding's fingerprint (citable identity).
    assert "feedfacecafe0000" in router.messages[0][0]["content"]


def test_findings_narrate_by_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_db(tmp_path, monkeypatch)
    router = EchoRouter(reply="narrated one")
    monkeypatch.setattr(cli_findings, "_load_router", lambda: router)
    result = runner.invoke(app, ["findings", "narrate", "feedface"])
    assert result.exit_code == 0
    assert "narrated one" in result.stdout


def test_findings_narrate_empty_is_graceful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_db(tmp_path, monkeypatch)
    router = EchoRouter()
    monkeypatch.setattr(cli_findings, "_load_router", lambda: router)
    result = runner.invoke(app, ["findings", "narrate", "--status", "resolved"])
    assert result.exit_code == 0
    assert "no findings to narrate" in result.stdout
    assert router.messages == []  # no LLM call for an empty set
