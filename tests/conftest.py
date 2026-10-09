"""Shared pytest fixtures for the homelab-helper test suite.

The suite runs hermetically: ``.env`` loading is disabled before any harness
module is imported. Without this a run on an operator's workstation reads that
operator's real credentials — tests would pass or fail depending on whose
machine they ran on, and a test that monkeypatches an adapter factory is
silently bypassed whenever the ambient config names something real to build.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import TYPE_CHECKING

import pytest

from homelab_helper.config import HOME_VAR, NO_DOTENV_VAR

if TYPE_CHECKING:
    from collections.abc import Iterator

# The operator's shell exports HOMELAB_HELPER_* from ~/.env; a test that builds
# an adapter from the environment would then reach a real controller or NAS.
# Only the two variables this file sets survive. The Anthropic key is also read
# under its SDK name, so it goes too.
for _name in [k for k in os.environ if k.startswith("HOMELAB_HELPER_")]:
    if _name not in {HOME_VAR, NO_DOTENV_VAR}:
        del os.environ[_name]
os.environ.pop("ANTHROPIC_API_KEY", None)

# Set before the CLI/MCP entry points call load_env(); pytest imports conftest
# ahead of the test modules, so this lands first.
os.environ[NO_DOTENV_VAR] = "1"

# Point the per-user data/config directories at a throwaway location so a test
# that never sets HOMELAB_HELPER_DATABASE_URL can't touch the operator's real
# database or config file.
os.environ[HOME_VAR] = tempfile.mkdtemp(prefix="homelab-helper-tests-")


class _NoImplicitLoopPolicy(asyncio.DefaultEventLoopPolicy):
    """``get_event_loop()`` never conjures a loop.

    pytest-asyncio remembers the "old" loop before installing its runner by
    calling ``asyncio.get_event_loop()``. After a CliRunner test, ``asyncio.run``
    has left the main thread with no loop, and Python 3.12's default policy
    answers by *creating* one — which pytest-asyncio stores, restores and never
    closes. Under ``filterwarnings = error`` that unclosed loop (and its
    self-pipe sockets) surfaces as a random test error. Raising here takes the
    ``except RuntimeError: old_loop = None`` branch instead.
    """

    def get_event_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._local._loop  # type: ignore[attr-defined]
        if loop is None:
            raise RuntimeError("no current event loop (tests never create one implicitly)")
        return loop


@pytest.fixture(scope="session")
def event_loop_policy() -> Iterator[asyncio.AbstractEventLoopPolicy]:
    policy = _NoImplicitLoopPolicy()
    previous = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(policy)
    try:
        yield policy
    finally:
        asyncio.set_event_loop_policy(previous)


# ---------------------------------------------------------------------------
# Postgres: the same suite against a real server (stability.md, "Database")
# ---------------------------------------------------------------------------

# Deliberately outside the HOMELAB_HELPER_ prefix so the scrub above leaves it.
PG_VAR = "HELPER_TEST_DATABASE_URL"
_PG_URL = os.environ.get(PG_VAR)


_RESET_SQL = """
DO $$
DECLARE r record;
BEGIN
    SET LOCAL session_replication_role = replica;  -- FK triggers off: any order works
    FOR r IN SELECT tablename FROM pg_tables
             WHERE schemaname = 'public' AND tablename <> 'alembic_version'
    LOOP
        EXECUTE 'DELETE FROM ' || quote_ident(r.tablename);
    END LOOP;
END $$;
"""


def _reset_postgres_schema(url: str) -> None:
    """Empty every table between tests, keeping the schema.

    Tables and enum types survive from the first ``create_all`` (or migration),
    so each test pays a few deletes rather than a 0.5 s schema build; emptying
    a fresh database is a no-op.
    """
    import asyncpg  # noqa: PLC0415 - only when the suite targets Postgres

    dsn = url.replace("postgresql+asyncpg://", "postgresql://", 1)

    async def _go() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_RESET_SQL)
        finally:
            await conn.close()

    # A thread keeps asyncio.run() away from the loop pytest-asyncio owns.
    import threading  # noqa: PLC0415

    errors: list[BaseException] = []

    def _run() -> None:
        try:
            asyncio.run(_go())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            errors.append(exc)

    t = threading.Thread(target=_run)
    t.start()
    t.join()
    if errors:
        raise errors[0]


@pytest.fixture(autouse=True)
def _postgres_redirect(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``HELPER_TEST_DATABASE_URL`` set, every engine the code builds —
    whatever SQLite URL a test or the CLI asked for — lands on that Postgres
    database, emptied first. Tests that inspect the SQLite file itself carry
    ``@pytest.mark.sqlite_only`` and are skipped."""
    if not _PG_URL:
        return
    if request.node.get_closest_marker("sqlite_only"):
        pytest.skip("inspects the SQLite file; the suite is running against Postgres")
    import sqlalchemy.ext.asyncio as sa_async  # noqa: PLC0415
    import sqlalchemy.ext.asyncio.engine as sa_async_engine  # noqa: PLC0415

    import homelab_helper.db.session as session_mod  # noqa: PLC0415

    real = sa_async_engine.create_async_engine

    def redirected(url, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(url).startswith("sqlite"):
            url = _PG_URL
        return real(url, *args, **kwargs)

    # The session module, the package attribute Alembic's env.py imports from
    # at each run, and the engine module async_engine_from_config reads.
    monkeypatch.setattr(session_mod, "create_async_engine", redirected)
    monkeypatch.setattr(sa_async, "create_async_engine", redirected)
    monkeypatch.setattr(sa_async_engine, "create_async_engine", redirected)
    _reset_postgres_schema(_PG_URL)
