"""Sync helper used by CLI commands.

The CLI is sync; the probe-sync code is async. This module bridges the two
with a single ``asyncio.run`` invocation so callers don't have to think about
event loops.
"""

from __future__ import annotations

import asyncio

from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.probes.registry import sync_probes_to_db


def sync_probes_sync(database_url: str) -> tuple[int, int]:
    """Run probe sync against ``database_url``; return ``(inserted, updated)``."""

    async def _run() -> tuple[int, int]:
        engine = make_engine(database_url)
        try:
            sm = make_sessionmaker(engine)
            async with session_scope(sm) as session:
                return await sync_probes_to_db(session)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


__all__ = ["sync_probes_sync"]
