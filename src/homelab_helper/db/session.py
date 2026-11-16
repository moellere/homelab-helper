"""Async SQLAlchemy engine + session factory.

The harness uses async SQLAlchemy 2.0 throughout (per dev choices). Synchronous
contexts (CLI, Alembic) bridge to async with ``asyncio.run`` / a sync engine
helper as needed. Connection URL comes from configuration (TBD in a later task);
this module currently exposes only the factories so importers don't drift.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine


def make_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Build an async engine for the given database URL.

    The default SQLite URL during development is ``sqlite+aiosqlite:///./homelab.db``.
    Postgres support is opt-in via the ``postgres`` extra (``asyncpg``); the URL
    shape there is ``postgresql+asyncpg://user:pw@host/db``.
    """
    engine = create_async_engine(url, echo=echo, future=True)
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
    return engine


SQLITE_BUSY_TIMEOUT_MS = 5000


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """WAL lets readers (the status page, the MCP server) run beside the daemon's
    writes, and a busy timeout makes a second writer wait instead of failing at once."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Async context manager that commits on clean exit, rolls back on exception."""
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def advisory_lock(engine: AsyncEngine, key: int) -> AsyncIterator[bool]:
    """Hold a Postgres advisory lock for one pass; yields ``False`` if another pass has it.

    Postgres only: off Postgres (the single-user SQLite default) it yields ``True``.
    """
    if engine.dialect.name != "postgresql":
        yield True
        return
    async with engine.connect() as conn:
        held = bool(
            (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar()
        )
        await conn.commit()
        try:
            yield held
        finally:
            if held:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                await conn.commit()


__all__ = ["advisory_lock", "make_engine", "make_sessionmaker", "session_scope"]
