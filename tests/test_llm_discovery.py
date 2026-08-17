"""Discovery Agent tests — protocol parse, validation, deterministic apply."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import Architecture, DiscoverySource
from homelab_helper.db.models import Host
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.llm.discovery import (
    HostProposal,
    apply_proposal,
    parse_agent_reply,
    render_hosts_for_prompt,
    validate_proposal,
)


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
# parse (pure)
# ---------------------------------------------------------------------------


def test_parse_clean_json() -> None:
    turn = parse_agent_reply('{"say": "What is the hostname?", "proposal": null, "done": false}')
    assert turn.parse_error is None
    assert turn.say == "What is the hostname?"
    assert turn.proposal is None
    assert turn.done is False


def test_parse_fenced_json() -> None:
    text = 'Sure!\n```json\n{"say": "hi", "proposal": null, "done": true}\n```\nthanks'
    turn = parse_agent_reply(text)
    assert turn.parse_error is None
    assert turn.done is True


def test_parse_json_embedded_in_prose() -> None:
    text = 'Here is my reply: {"say": "ok", "proposal": null, "done": false} hope that helps'
    turn = parse_agent_reply(text)
    assert turn.parse_error is None
    assert turn.say == "ok"


def test_parse_proposal_fields() -> None:
    text = (
        '{"say": "Ready to register.", "done": false, "proposal": '
        '{"hostname": "minipc1", "primary_ip": "10.0.6.27", "role": "k8s-node", '
        '"arch": "amd64", "ssh_user": "ops", "ssh_key_path": "~/.ssh/id_ed25519", '
        '"notes": null}}'
    )
    turn = parse_agent_reply(text)
    assert turn.proposal is not None
    assert turn.proposal.hostname == "minipc1"
    assert turn.proposal.ssh_key_path == "~/.ssh/id_ed25519"
    assert turn.proposal.notes is None


def test_parse_junk_reports_error() -> None:
    turn = parse_agent_reply("I think you should add the host now!")
    assert turn.parse_error is not None
    assert "JSON" in turn.parse_error


def test_parse_proposal_without_hostname_is_error() -> None:
    turn = parse_agent_reply('{"say": "x", "proposal": {"primary_ip": "10.0.0.1"}, "done": false}')
    assert turn.parse_error is not None
    assert "hostname" in turn.parse_error


# ---------------------------------------------------------------------------
# validation (deterministic — the LLM cannot waive these)
# ---------------------------------------------------------------------------


async def test_validate_ok(sessionmaker) -> None:
    async with sessionmaker() as s:
        errors = await validate_proposal(
            s, HostProposal(hostname="minipc1", primary_ip="10.0.6.27", arch="amd64")
        )
    assert errors == []


async def test_validate_bad_ip(sessionmaker) -> None:
    async with sessionmaker() as s:
        errors = await validate_proposal(s, HostProposal(hostname="x", primary_ip="10.0.6.999"))
    assert any("not a valid IP" in e for e in errors)


async def test_validate_bad_arch(sessionmaker) -> None:
    async with sessionmaker() as s:
        errors = await validate_proposal(s, HostProposal(hostname="x", arch="pentium"))
    assert any("arch" in e for e in errors)


async def test_validate_duplicate_hostname(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(Host(hostname="minipc1", primary_ip="10.0.6.27"))
    async with sessionmaker() as s:
        errors = await validate_proposal(s, HostProposal(hostname="minipc1"))
    assert any("already exists" in e for e in errors)


async def test_validate_duplicate_ip(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(Host(hostname="other", primary_ip="10.0.6.27"))
    async with sessionmaker() as s:
        errors = await validate_proposal(s, HostProposal(hostname="fresh", primary_ip="10.0.6.27"))
    assert any("already exists" in e for e in errors)


async def test_validate_rejects_key_material(sessionmaker) -> None:
    async with sessionmaker() as s:
        errors = await validate_proposal(
            s,
            HostProposal(
                hostname="x",
                ssh_key_path="-----BEGIN OPENSSH PRIVATE KEY----- b3BlbnNzaC1rZXk...",
            ),
        )
    assert any("key material" in e for e in errors)


# ---------------------------------------------------------------------------
# apply (only after operator confirmation — caller's contract)
# ---------------------------------------------------------------------------


async def test_apply_writes_manual_host(sessionmaker) -> None:
    proposal = HostProposal(
        hostname="minipc1",
        primary_ip="10.0.6.27",
        role="k8s-node",
        arch="amd64",
        ssh_user="ops",
        ssh_key_path="~/.ssh/id_ed25519",
        notes="new mini-PC",
    )
    async with session_scope(sessionmaker) as s:
        await apply_proposal(s, proposal)
    async with sessionmaker() as s:
        host = (await s.execute(select(Host))).scalar_one()
        assert host.hostname == "minipc1"
        assert host.discovery_source == DiscoverySource.MANUAL
        assert host.arch == Architecture.AMD64
        assert host.capabilities == {"role": "k8s-node"}
        assert host.credentials_ref == "ssh:ops:~/.ssh/id_ed25519"
        assert host.notes == "new mini-PC"


async def test_apply_without_creds_leaves_ref_none(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await apply_proposal(s, HostProposal(hostname="bare"))
    async with sessionmaker() as s:
        host = (await s.execute(select(Host))).scalar_one()
        assert host.credentials_ref is None
        assert host.arch == Architecture.OTHER


def test_render_hosts_for_prompt_empty() -> None:
    assert "none" in render_hosts_for_prompt([])
