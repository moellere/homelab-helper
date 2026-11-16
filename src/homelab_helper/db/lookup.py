"""Find a row by the leading characters of its id, in the database.

Operators and agents quote ids by prefix (``helper maintenance list`` prints
eight characters). Postgres renders a uuid with hyphens and SQLite stores it
as 32 bare hex digits, so both the column and the prefix are compared with
the hyphens removed. Anything but hex digits and hyphens is no id at all,
which also keeps ``%`` and ``_`` out of the ``LIKE``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import String, cast, func, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_ID_PREFIX = re.compile(r"^[0-9a-f-]*$")


def id_prefix(text: str) -> str | None:
    """The bare-hex form of ``text``, or None when it cannot be part of an id.

    An empty prefix is every id: with more than one row it is ambiguous.
    """
    value = text.strip().lower()
    return value.replace("-", "") if _ID_PREFIX.match(value) else None


async def rows_by_id_prefix[T](
    session: AsyncSession, model: type[T], prefix: str, *where: Any, limit: int = 2
) -> list[T]:
    """Up to ``limit`` rows of ``model`` whose id starts with ``prefix``; two means ambiguous."""
    bare = id_prefix(prefix)
    if bare is None:
        return []
    column = model.id  # type: ignore[attr-defined]
    stmt = (
        select(model)
        .where(func.replace(cast(column, String), "-", "").like(f"{bare}%"), *where)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


__all__ = ["id_prefix", "rows_by_id_prefix"]
