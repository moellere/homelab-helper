"""Copy every row of one homelab-helper database into another.

``helper db copy`` moves a deployment between backends (SQLite to Postgres is
the expected case) without exporting anything by hand. The rules:

- Both databases must already be at the **same Alembic revision**. The copy
  never migrates: run ``helper db init`` against each first, so the schema is
  the one the migrations built rather than one inferred from the models.
- The target's rows are **replaced**, not merged. ``db init`` seeds probes and
  trust domains, so a freshly initialised target is never empty; deleting
  first is what makes the copy an exact mirror. Tables are emptied children
  first and filled parents first (``Base.metadata.sorted_tables``).
- Everything happens in **one transaction on the target**. Row counts are
  compared table by table before the commit; any mismatch rolls the whole copy
  back, leaving the target as it was.
- The source is only read.

Primary keys are UUIDs, so no sequences need resetting afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DBAPIError

import homelab_helper.db.models  # noqa: F401 - registers every table on Base.metadata
from homelab_helper.db.base import Base
from homelab_helper.db.session import make_engine

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

DEFAULT_BATCH_SIZE = 5000


class CopyError(RuntimeError):
    """The copy was refused or failed verification; the target is unchanged."""


@dataclass(frozen=True)
class TableCopy:
    """Rows copied into one table."""

    table: str
    rows: int


async def _revision(engine: AsyncEngine) -> str | None:
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    except DBAPIError:
        return None


async def _count(conn: AsyncConnection, table_name: str) -> int:
    table = Base.metadata.tables[table_name]
    return int((await conn.execute(select(func.count()).select_from(table))).scalar_one())


async def _check_revisions(source: AsyncEngine, target: AsyncEngine) -> None:
    source_rev = await _revision(source)
    target_rev = await _revision(target)
    if source_rev is None:
        raise CopyError("the source has no Alembic revision; is it a homelab-helper database?")
    if target_rev is None:
        raise CopyError("the target has no Alembic revision; run `helper db init` against it first")
    if source_rev != target_rev:
        raise CopyError(
            f"revisions differ (source {source_rev}, target {target_rev}); "
            "run `helper db init` against both so they match"
        )


async def _copy_table(
    src: AsyncConnection, dst: AsyncConnection, table: Table, batch_size: int
) -> int:
    # A row may reference a row of its own table that comes later in the scan
    # (execution_receipt.rollback_receipt_id). Insert those columns empty, then
    # fill them in once every row of the table exists.
    self_refs = [fk.parent.name for fk in table.foreign_keys if fk.column.table is table]
    (pk,) = table.primary_key.columns
    deferred: list[dict[str, object]] = []
    rows = 0
    result = await src.stream(select(table))
    async for batch in result.partitions(batch_size):
        values = [dict(row._mapping) for row in batch]
        for row in values:
            refs = {c: row[c] for c in self_refs if row[c] is not None}
            if refs:
                deferred.append({"_pk": row[pk.name], **refs})
                row.update(dict.fromkeys(refs))
        await dst.execute(table.insert(), values)
        rows += len(batch)
    for row in deferred:
        pk_value = row.pop("_pk")
        await dst.execute(table.update().where(pk == pk_value).values(**row))
    return rows


async def copy_database(
    source_url: str, target_url: str, *, batch_size: int = DEFAULT_BATCH_SIZE
) -> list[TableCopy]:
    """Replace the target's rows with the source's; see the module docstring."""
    if make_url(source_url) == make_url(target_url):
        raise CopyError("source and target are the same database")
    source = make_engine(source_url)
    target = make_engine(target_url)
    try:
        await _check_revisions(source, target)
        tables = Base.metadata.sorted_tables
        copied: list[TableCopy] = []
        async with target.begin() as dst, source.connect() as src:
            for table in reversed(tables):
                await dst.execute(table.delete())
            for table in tables:
                copied.append(TableCopy(table.name, await _copy_table(src, dst, table, batch_size)))
            for entry in copied:
                expected = await _count(src, entry.table)
                actual = await _count(dst, entry.table)
                if expected != actual:
                    raise CopyError(
                        f"{entry.table}: source has {expected} rows, target has {actual}; "
                        "rolled back"
                    )
        return copied
    finally:
        await source.dispose()
        await target.dispose()
