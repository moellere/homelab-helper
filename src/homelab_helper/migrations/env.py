"""Alembic environment for homelab-helper.

Takes the URL from the ``sqlalchemy.url`` option when the caller set one
(``homelab_helper.db.migrate.alembic_config`` does), else the
harness default (``HOMELAB_HELPER_DATABASE_URL`` or the per-user data directory). Supports both online (with a
live engine) and offline (SQL-emitting) modes.

Async note: Alembic itself runs sync; for an async URL we create an
``AsyncEngine`` and use ``run_sync`` to invoke Alembic's migration context.
This is the standard async-Alembic pattern documented in the Alembic cookbook.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import homelab_helper.db.models

# Ensure all models are imported so Base.metadata is fully populated.
from homelab_helper.config import database_url
from homelab_helper.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("HOMELAB_HELPER_DATABASE_URL")
    if url:
        return url
    # Local dev fallback. Async driver so the engine matches runtime usage.
    return "sqlite+aiosqlite:///./homelab.db"


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emits SQL, no DB connection."""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),  # SQLite needs batch ALTER
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Async online migration path — used when the URL is async-flavored."""
    connectable: AsyncEngine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a real DB connection."""
    url = _database_url()
    if url.startswith(("sqlite+aiosqlite", "postgresql+asyncpg")):
        asyncio.run(run_async_migrations())
        return
    # Sync fallback for URLs like "sqlite:///..." or "postgresql://..."
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = url
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
