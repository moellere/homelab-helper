"""Skill Inferer tests — scoring, ratchet, manual override, chat/CLI wiring."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

import homelab_helper.cli.chat as cli_chat
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import SkillLevel, SkillSource
from homelab_helper.db.models import SkillProfile
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.skill_inferer import (
    _score_text,
    get_profile,
    level_for_score,
    observe_text,
    render_profile_for_prompt,
    set_skill,
)
from tests.test_cli_chat import EchoRouter

runner = CliRunner()


@pytest.fixture
async def engine():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine):
    return make_sessionmaker(engine)


# ---------------------------------------------------------------------------
# scoring (pure)
# ---------------------------------------------------------------------------


def test_advanced_terms_outweigh_basic() -> None:
    basic = _score_text("my disk is full")
    advanced = _score_text("the zfs scrub found errors")
    assert basic["storage"] == 1.0
    assert advanced["storage"] == 6.0  # two advanced terms


def test_terms_match_whole_words_only() -> None:
    # "disks" != "disk"; "cephalopod" != "ceph".
    assert "storage" not in _score_text("cephalopod discotheque")


def test_multiple_domains_in_one_message() -> None:
    scores = _score_text("running ceph on kubernetes with a vlan for storage traffic")
    assert set(scores) >= {"storage", "container-orchestration", "networking"}


def test_level_thresholds() -> None:
    assert level_for_score(0.0) == SkillLevel.NOVICE
    assert level_for_score(2.0) == SkillLevel.BASIC
    assert level_for_score(6.0) == SkillLevel.INTERMEDIATE
    assert level_for_score(12.0) == SkillLevel.ADVANCED


# ---------------------------------------------------------------------------
# accumulation + ratchet + manual override
# ---------------------------------------------------------------------------


async def test_ac6_chatting_builds_profile(sessionmaker) -> None:
    """AC6: after chatting about ZFS/Ceph/K8s, the profile reflects it."""
    messages = [
        "how do I check my zfs zpool status after a scrub?",
        "ceph says an osd is down, should I resilver?",
        "my kubernetes cluster runs on talos with argocd gitops",
        "helm chart for the ingress operator keeps failing",
    ]
    async with session_scope(sessionmaker) as s:
        for m in messages:
            await observe_text(s, m)
    async with sessionmaker() as s:
        rows = {r.domain: r for r in await get_profile(s)}
    assert rows["storage"].level in {SkillLevel.INTERMEDIATE, SkillLevel.ADVANCED}
    assert rows["container-orchestration"].level in {
        SkillLevel.INTERMEDIATE,
        SkillLevel.ADVANCED,
    }
    assert rows["storage"].source == SkillSource.INFERRED


async def test_levels_ratchet_up_never_down(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        for _ in range(5):
            await observe_text(s, "zfs ceph zpool vdev scrub")  # drives to advanced
        result = await observe_text(s, "my disk is full")  # basic talk afterwards
    assert result.promoted == {}  # no demotion, no re-promotion
    async with sessionmaker() as s:
        row = (
            await s.execute(select(SkillProfile).where(SkillProfile.domain == "storage"))
        ).scalar_one()
        assert row.level == SkillLevel.ADVANCED


async def test_manual_override_wins_over_inference(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await set_skill(s, "storage", SkillLevel.NOVICE)
        for _ in range(10):
            await observe_text(s, "zfs ceph zpool vdev scrub osd resilver")
    async with sessionmaker() as s:
        row = (
            await s.execute(select(SkillProfile).where(SkillProfile.domain == "storage"))
        ).scalar_one()
        assert row.level == SkillLevel.NOVICE  # pinned
        assert row.source == SkillSource.MANUAL
        assert row.evidence_count == 10  # bookkeeping continues


async def test_observe_ignores_unrelated_text(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        result = await observe_text(s, "what's the weather like today?")
    assert result.touched is False
    async with sessionmaker() as s:
        assert await get_profile(s) == []


def test_render_profile_for_prompt() -> None:
    row = SkillProfile(domain="storage", level=SkillLevel.ADVANCED)
    line = render_profile_for_prompt([row])
    assert "storage=advanced" in line
    assert render_profile_for_prompt([]) == ""


# ---------------------------------------------------------------------------
# chat wiring + CLI
# ---------------------------------------------------------------------------


def _seed_db(tmp_path: Path, monkeypatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'skills.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    async def _init() -> None:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_init())
    return url


def test_chat_observes_skills_passively(tmp_path: Path, monkeypatch) -> None:
    """AC6 end-to-end: a chat question updates the profile without being asked."""
    _seed_db(tmp_path, monkeypatch)
    router = EchoRouter(reply="answer")
    monkeypatch.setattr(cli_chat, "_load_router", lambda: router)
    result = runner.invoke(app, ["chat", "why is my zfs scrub slow on the ceph osd node?"])
    assert result.exit_code == 0

    listed = runner.invoke(app, ["skills"])
    assert listed.exit_code == 0
    assert "storage" in listed.stdout
    assert "inferred" in listed.stdout


def test_chat_system_prompt_carries_profile(tmp_path: Path, monkeypatch) -> None:
    _seed_db(tmp_path, monkeypatch)
    runner.invoke(app, ["skills", "set", "storage", "advanced"])
    router = EchoRouter(reply="answer")
    monkeypatch.setattr(cli_chat, "_load_router", lambda: router)
    result = runner.invoke(app, ["chat", "hello there"])
    assert result.exit_code == 0
    assert "storage=advanced" in router.systems[0]


def test_skills_cli_empty_and_set(tmp_path: Path, monkeypatch) -> None:
    _seed_db(tmp_path, monkeypatch)
    empty = runner.invoke(app, ["skills"])
    assert empty.exit_code == 0
    assert "no skill signal yet" in empty.stdout

    pinned = runner.invoke(app, ["skills", "set", "networking", "intermediate"])
    assert pinned.exit_code == 0
    assert "pinned" in pinned.stdout

    listed = runner.invoke(app, ["skills"])
    assert "networking" in listed.stdout
    assert "manual" in listed.stdout


def test_skills_set_rejects_bad_level(tmp_path: Path, monkeypatch) -> None:
    _seed_db(tmp_path, monkeypatch)
    result = runner.invoke(app, ["skills", "set", "storage", "wizard"])
    assert result.exit_code != 0
