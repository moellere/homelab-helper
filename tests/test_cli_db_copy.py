"""``helper db copy``: every row into another database at the same revision.

SQLite-to-SQLite stands in for SQLite-to-Postgres here; under the Postgres
suite every engine is redirected to one database, so these are sqlite_only.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

import homelab_helper.db.copy as copy_mod
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.models import Host
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.sqlite_only

runner = CliRunner()


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _init(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0, result.stdout


def _add_host(url: str, hostname: str) -> None:
    async def _go() -> None:
        engine = make_engine(url)
        try:
            async with session_scope(make_sessionmaker(engine)) as session:
                session.add(Host(hostname=hostname, primary_ip="10.0.6.20"))
        finally:
            await engine.dispose()

    asyncio.run(_go())


def _counts(url: str) -> dict[str, int]:
    async def _go() -> dict[str, int]:
        engine = make_engine(url)
        try:
            async with engine.connect() as conn:
                return {
                    t.name: (await conn.execute(select(func.count()).select_from(t))).scalar_one()
                    for t in Base.metadata.sorted_tables
                }
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _hostnames(url: str) -> list[str]:
    async def _go() -> list[str]:
        engine = make_engine(url)
        try:
            async with engine.connect() as conn:
                return sorted((await conn.execute(select(Host.hostname))).scalars())
        finally:
            await engine.dispose()

    return asyncio.run(_go())


@pytest.fixture
def pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    src, dst = _url(tmp_path / "src.db"), _url(tmp_path / "dst.db")
    _init(src, monkeypatch)
    _init(dst, monkeypatch)
    _add_host(src, "node0")
    _add_host(src, "node1")
    _add_host(dst, "stale")
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", src)
    return src, dst


def test_copy_replaces_the_target_with_the_source(pair: tuple[str, str]) -> None:
    src, dst = pair
    result = runner.invoke(app, ["db", "copy", "--to", dst, "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "counts verified" in result.stdout
    assert _hostnames(dst) == ["node0", "node1"]
    assert _counts(dst) == _counts(src)


def test_copy_reads_the_target_from_a_secret_reference(
    pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = pair
    monkeypatch.setenv("COPY_TARGET", dst)
    result = runner.invoke(app, ["db", "copy", "--from", src, "--to", "env:COPY_TARGET", "-y"])
    assert result.exit_code == 0, result.stdout
    assert _hostnames(dst) == ["node0", "node1"]


def test_copy_refuses_an_uninitialised_target(tmp_path: Path, pair: tuple[str, str]) -> None:
    empty = _url(tmp_path / "empty.db")
    result = runner.invoke(app, ["db", "copy", "--to", empty, "--yes"])
    assert result.exit_code == 1
    assert "db init" in result.stdout


def test_copy_refuses_the_same_database(pair: tuple[str, str]) -> None:
    src, _ = pair
    result = runner.invoke(app, ["db", "copy", "--to", src, "--yes"])
    assert result.exit_code == 1
    assert "same database" in result.stdout


def test_copy_asks_before_replacing(pair: tuple[str, str]) -> None:
    _, dst = pair
    result = runner.invoke(app, ["db", "copy", "--to", dst], input="n\n")
    assert result.exit_code == 1
    assert _hostnames(dst) == ["stale"]


def test_a_count_mismatch_rolls_the_whole_copy_back(
    pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dst = pair
    real_count = copy_mod._count

    async def off_by_one(conn, table_name):  # type: ignore[no-untyped-def]
        n = await real_count(conn, table_name)
        return n + 1 if table_name == "host" and "dst" in str(conn.engine.url) else n

    monkeypatch.setattr(copy_mod, "_count", off_by_one)
    result = runner.invoke(app, ["db", "copy", "--to", dst, "--yes"])
    assert result.exit_code == 1
    assert "rolled back" in result.stdout
    assert _hostnames(dst) == ["stale"]
