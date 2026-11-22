"""CLI test for ``helper discover restore-drill`` against a file-backed SQLite."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from typer.testing import CliRunner

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.models import Observation
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.backups import RESTORE_DRILL_KEY, restore_drill_timestamps

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


async def test_restore_drill_is_recorded_and_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'drill.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    import asyncio  # noqa: PLC0415

    result = await asyncio.to_thread(
        runner.invoke, app, ["discover", "restore-drill", "103", "--notes", "booted in a sandbox"]
    )
    assert result.exit_code == 0, result.output
    assert "restore drill recorded" in result.output

    engine = make_engine(url)
    try:
        async with session_scope(make_sessionmaker(engine)) as s:
            obs = (
                await s.execute(select(Observation).where(Observation.key == RESTORE_DRILL_KEY))
            ).scalar_one()
            assert obs.value["notes"] == "booted in a sandbox"
            assert "103" in await restore_drill_timestamps(s)
    finally:
        await engine.dispose()
