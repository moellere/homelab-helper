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
    assert requests == []

    listing = runner.invoke(app, ["exec", "list"])
    assert "1 proposal(s)" in listing.output, "declined proposal stays pending"


def test_exec_run_refuses_at_propose_without_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh DB with no grants: the gate refuses and the adapter is never built."""
    db_path = tmp_path / "nogrant.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)

    import asyncio

    async def seed() -> str:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as s:
            await seed_domains(s)
            proposal = ProposalLog(
                title="Restart web01",
                artifact=_ARTIFACT,
                blast_radius="single-host",
            )
            s.add(proposal)
            await s.flush()
            pid = str(proposal.id)
        await engine.dispose()
        return pid

    proposal_id = asyncio.run(seed())

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


def test_exec_run_yes_flag_preconsents(exec_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        "homelab_helper.cli.execute._build_adapter", lambda: _mock_adapter(requests)
    )
    result = runner.invoke(app, ["exec", "run", exec_db[:8], "--yes"])
    assert result.exit_code == 0, result.output
    assert "pre-consented" in result.output
    assert len(requests) == 2
