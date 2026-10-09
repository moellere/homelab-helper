"""Integration test: the committed lab fixture produces the day-one audit.

Closes roadmap AC3 ("at least eleven findings against the lab") and AC4
(idempotent re-runs) as a CI test with no live access — the fixture is replayed
into an in-memory DB and the finding corpus is asserted.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingStatus
from homelab_helper.db.models import Host, ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.lab_replay import (
    LabFixtureError,
    bundled_labs,
    load_lab_fixture,
    parse_lab_fixture,
    resolve_lab_fixture,
)

_FIXTURE = resolve_lab_fixture("example")
_MIN_FINDINGS = 11  # AC3: "at least eleven findings"
_PACKAGE = Path(__file__).resolve().parent.parent / "src" / "homelab_helper"


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


async def _open_finding_count(sm) -> int:
    async with sm() as s:
        return (
            await s.execute(
                select(func.count(ReconciliationFinding.id)).where(
                    ReconciliationFinding.status == FindingStatus.OPEN
                )
            )
        ).scalar_one()


async def test_lab_fixture_produces_day_one_findings(sessionmaker) -> None:
    data = parse_lab_fixture(_FIXTURE.read_text())
    async with session_scope(sessionmaker) as s:
        result = await load_lab_fixture(s, data)

    assert result.hosts_loaded == 3
    assert result.assertions_run >= 1

    async with sessionmaker() as s:
        findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        kinds = Counter(f.kind for f in findings)

    # AC3: at least eleven day-one findings.
    assert len(findings) >= _MIN_FINDINGS
    # Every finding source the engine produces is represented.
    assert kinds[FindingKind.INVENTORY_GAP] >= 1
    assert kinds[FindingKind.STORAGE_PROVENANCE_DELTA] == 2  # forged WWN on lab-b + lab-c
    assert kinds[FindingKind.CONFIG_DRIFT] >= 1


async def test_lab_replay_is_idempotent(sessionmaker) -> None:
    data = parse_lab_fixture(_FIXTURE.read_text())
    async with session_scope(sessionmaker) as s:
        await load_lab_fixture(s, data)
    first = await _open_finding_count(sessionmaker)

    # AC4: re-running the same world neither duplicates hosts nor findings.
    async with session_scope(sessionmaker) as s:
        await load_lab_fixture(s, data)
    second = await _open_finding_count(sessionmaker)

    assert first == second
    async with sessionmaker() as s:
        assert (await s.execute(select(func.count(Host.id)))).scalar_one() == 3


def test_the_bundled_labs_ship_in_the_wheel() -> None:
    """The on-ramp must work from a bare install: no checkout, no fixtures/ dir."""
    labs = bundled_labs()
    assert set(labs) == {"example", "asymmetric"}
    assert all(path.is_relative_to(_PACKAGE) for path in labs.values())
    assert resolve_lab_fixture(None) == labs["example"]
    assert resolve_lab_fixture("asymmetric-lab") == labs["asymmetric"]
    with pytest.raises(LabFixtureError, match="bundled: asymmetric, example"):
        resolve_lab_fixture("nope")


@pytest.fixture
async def replay_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'replay.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    eng = make_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()
    return url


def test_cli_replay_defaults_to_the_example_lab(replay_db_url: str) -> None:
    """Getting-started step 3, verbatim: `helper discover replay` with no argument."""
    result = CliRunner().invoke(app, ["discover", "replay"])
    assert result.exit_code == 0, result.output
    assert "lab: example-lab.yaml" in result.output
    assert "3 host(s)" in result.output

    by_name = CliRunner().invoke(app, ["discover", "replay", "asymmetric"])
    assert by_name.exit_code == 0, by_name.output
    assert "1 cluster(s), 3 guest(s)" in by_name.output

    unknown = CliRunner().invoke(app, ["discover", "replay", "nope"])
    assert unknown.exit_code == 1
    # Rich wraps under CliRunner's narrow terminal; assert on the unwrappable part.
    assert "neither a file nor a bundled lab" in unknown.output
