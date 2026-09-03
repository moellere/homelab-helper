"""CLI tests for ``helper exec list|run|receipts`` against a seeded SQLite.

The adapter is a MockTransport injected by monkeypatching the module-level
factory ``cli.execute._build_adapter`` — no live Proxmox, no env config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from homelab_helper.adapters.proxmox import ProxmoxAdapter, ProxmoxConfig
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import AutonomyLevel, TrustDomain
from homelab_helper.db.models import ProposalLog
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.escalation import PROMOTION_STREAK
from homelab_helper.engine.trust import grant_cell, seed_domains

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_ARTIFACT = {
    "kind": "action",
    "action": {
        "domain": "containers",
        "action_kind": "restart",
        "target": {"cluster": "homelab", "node": "pve1", "vmid": 101, "vm_kind": "lxc"},
        "hostnames": ["pve1"],
    },
    "rollback": {"verified": False, "strategy": "prior-power-state"},
}


def _mock_adapter(requests: list[httpx.Request]) -> ProxmoxAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/status/current"):
            return httpx.Response(200, json={"data": {"status": "running", "name": "web01"}})
        return httpx.Response(200, json={"data": "UPID:pve1:0000:reboot"})

    config = ProxmoxConfig(url="https://pve.test:8006", token_id="t@pam!x", token_secret="s")
    client = httpx.AsyncClient(
        base_url=config.url + "/api2/json", transport=httpx.MockTransport(handler)
    )
    return ProxmoxAdapter(config, client=client)


@pytest.fixture
async def exec_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Seeded domains, one CONFIRM grant on AC2's cell, one pending proposal."""
    db_path = tmp_path / "exec.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)
    async with session_scope(sm) as s:
        await seed_domains(s)
        await grant_cell(
            s,
            TrustDomain.CONTAINERS,
            "restart",
            "single-host",
            AutonomyLevel.CONFIRM,
            actor="enoch",
        )
        proposal = ProposalLog(
            title="Restart web01",
            artifact=_ARTIFACT,
            blast_radius="single-host",
            proposed_by="agent:planner",
        )
        s.add(proposal)
        await s.flush()
        proposal_id = str(proposal.id)
    await engine.dispose()
    return proposal_id


async def _seed_db(
    url: str, monkeypatch: pytest.MonkeyPatch, proposals: list[dict], *, grant: bool
) -> list[str]:
    """Create the schema, seed domains, optionally grant AC2's cell, add proposals."""
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = make_sessionmaker(engine)
    ids: list[str] = []
    async with session_scope(sm) as s:
        await seed_domains(s)
        if grant:
            await grant_cell(
                s,
                TrustDomain.CONTAINERS,
                "restart",
                "single-host",
                AutonomyLevel.CONFIRM,
                actor="enoch",
            )
        for artifact in proposals:
            proposal = ProposalLog(
                title="Restart web01",
                artifact=artifact,
                blast_radius="single-host",
                proposed_by="agent:planner",
            )
            s.add(proposal)
            await s.flush()
            ids.append(str(proposal.id))
    await engine.dispose()
    return ids


@pytest.fixture
async def nogrant_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Domains seeded, no grants — every cell sits at the PROPOSE floor."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'nogrant.db'}"
    return (await _seed_db(url, monkeypatch, [_ARTIFACT], grant=False))[0]


@pytest.fixture
async def shopping_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """One non-executable proposal — no action manifest, so no cell."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'shopping.db'}"
    artifact = {"kind": "shopping-list", "items": ["USB 2.5GbE NIC"]}
    return (await _seed_db(url, monkeypatch, [artifact], grant=False))[0]


@pytest.fixture
async def promotion_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Exactly one full streak of pending action proposals on the same cell."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'promotion.db'}"
    return await _seed_db(url, monkeypatch, [_ARTIFACT] * PROMOTION_STREAK, grant=False)


def test_exec_list_shows_pending_action(exec_db: str) -> None:
    result = runner.invoke(app, ["exec", "list"])
    assert result.exit_code == 0
    assert exec_db[:8] in result.output
    assert "1 proposal(s)" in result.output


def test_exec_run_confirms_and_dispatches(exec_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        "homelab_helper.cli.execute._build_adapter", lambda: _mock_adapter(requests)
    )
    result = runner.invoke(app, ["exec", "run", exec_db[:8]], input="y\n")
    assert result.exit_code == 0, result.output
    assert "confirm required" in result.output
    assert "executed" in result.output
    paths = [r.url.path for r in requests]
    assert paths[-1] == "/api2/json/nodes/pve1/lxc/101/status/reboot"

    receipts = runner.invoke(app, ["exec", "receipts"])
    assert receipts.exit_code == 0
    assert "1 receipt(s)" in receipts.output
    assert "succeeded" in receipts.output


def test_exec_run_declined_dispatches_nothing(
    exec_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        "homelab_helper.cli.execute._build_adapter", lambda: _mock_adapter(requests)
    )
    result = runner.invoke(app, ["exec", "run", exec_db[:8]], input="n\n")
    assert result.exit_code == 3
    assert "declined" in result.output
    assert [r for r in requests if r.method != "GET"] == [], "a decline changes nothing"

    listing = runner.invoke(app, ["exec", "list"])
    assert "1 proposal(s)" in listing.output, "declined proposal stays pending"


def test_exec_run_refuses_at_propose_without_grant(
    nogrant_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh DB with no grants: the gate refuses and the adapter is never built."""
    proposal_id = nogrant_db
    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        "homelab_helper.cli.execute._build_adapter", lambda: _mock_adapter(requests)
    )
    result = runner.invoke(app, ["exec", "run", proposal_id[:8]])
    assert result.exit_code == 3
    assert "refused" in result.output
    assert requests == []


def test_exec_run_unknown_id(exec_db: str) -> None:
    result = runner.invoke(app, ["exec", "run", "ffffffff"])
    assert result.exit_code == 1
    assert "no pending proposal" in result.output


def test_exec_accept_credits_the_cell(exec_db: str) -> None:
    """Accepting a hand-applied proposal is one clean approval toward the rung."""
    result = runner.invoke(app, ["exec", "accept", exec_db[:8], "--note", "did it by hand"])
    assert result.exit_code == 0, result.output
    assert "accepted" in result.output
    assert f"1/{PROMOTION_STREAK}" in result.output

    listing = runner.invoke(app, ["exec", "list"])
    assert "0 proposal(s)" in listing.output, "accepted proposals leave the pending queue"


def test_exec_reject_resets_the_streak(exec_db: str) -> None:
    result = runner.invoke(app, ["exec", "reject", exec_db[:8], "--note", "wrong guest"])
    assert result.exit_code == 0, result.output
    assert "rejected" in result.output
    assert "streak reset" in result.output


def test_exec_accept_non_action_proposal_credits_nothing(shopping_db: str) -> None:
    """A shopping-list proposal has no cell, so acceptance credits no trust."""
    result = runner.invoke(app, ["exec", "accept", shopping_db[:8]])
    assert result.exit_code == 0
    assert "no cell to credit" in result.output


def test_trust_history_shows_promotions(promotion_db: list[str]) -> None:
    """A full streak of accepted proposals promotes the cell; the spine says so."""
    for proposal_id in promotion_db:
        # Full ids here: uuid7 is time-ordered, so same-run proposals share a prefix.
        accepted = runner.invoke(app, ["exec", "accept", proposal_id])
        assert accepted.exit_code == 0, accepted.output

    history = runner.invoke(app, ["trust", "history"])
    assert history.exit_code == 0
    assert "auto-promote" in history.output

    show = runner.invoke(app, ["trust", "show"])
    assert "confirm" in show.output


def test_exec_rollback_undoes_an_execution(exec_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run, then roll back: the undo gets its own receipt and links the original."""
    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        "homelab_helper.cli.execute._build_adapter", lambda: _mock_adapter(requests)
    )
    run = runner.invoke(app, ["exec", "run", exec_db[:8], "--yes"])
    assert run.exit_code == 0, run.output
    receipt_id = run.output.rsplit("receipt ", 1)[-1].strip()

    undo = runner.invoke(app, ["exec", "rollback", receipt_id[:8], "--yes"])
    assert undo.exit_code == 0, undo.output
    assert "rolled back" in undo.output

    listing = runner.invoke(app, ["exec", "receipts"])
    assert "2 receipt(s)" in listing.output
    assert "rolled back" in listing.output
    assert "is an undo" in listing.output


def test_exec_rollback_unknown_receipt(exec_db: str) -> None:
    result = runner.invoke(app, ["exec", "rollback", "ffffffff", "--yes"])
    assert result.exit_code == 1
    assert "no receipt matches" in result.output


def test_exec_run_yes_flag_preconsents(exec_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        "homelab_helper.cli.execute._build_adapter", lambda: _mock_adapter(requests)
    )
    result = runner.invoke(app, ["exec", "run", exec_db[:8], "--yes"])
    assert result.exit_code == 0, result.output
    assert "pre-consented" in result.output
    assert len(requests) == 2
