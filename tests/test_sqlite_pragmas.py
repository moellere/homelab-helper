"""SQLite connections run in WAL mode with a busy timeout.

The daemon writes while the status page and the MCP server read; without WAL
a reader blocks the writer, and without a timeout a second writer fails at once.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from homelab_helper.db.session import SQLITE_BUSY_TIMEOUT_MS, make_engine


@pytest.mark.sqlite_only
async def test_a_file_database_opens_in_wal_with_a_busy_timeout(tmp_path) -> None:
    eng = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    try:
        async with eng.connect() as conn:
            assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
            timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
            assert timeout == SQLITE_BUSY_TIMEOUT_MS
    finally:
        await eng.dispose()
