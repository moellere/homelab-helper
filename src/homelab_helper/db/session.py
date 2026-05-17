"""Async SQLAlchemy engine + session factory.

The harness uses async SQLAlchemy 2.0 throughout (per dev choices). Synchronous
contexts (CLI, Alembic) bridge to async with ``asyncio.run`` / a sync engine
helper as needed. Connection URL comes from configuration (TBD in a later task);
this module currently exposes only the factories so importers don't drift.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

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
    return create_async_engine(url, echo=echo, future=True)


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


__all__ = ["make_engine", "make_sessionmaker", "session_scope"]
