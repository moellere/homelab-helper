"""Probe entry-point discovery and DB synchronization.

Probes register themselves via Python entry points in the
``homelab_helper.probes`` group. Each entry point points at a ``Probe``
subclass.

Two operations live here:

- :func:`discover_probes` — load entry points, return ``{name: Probe class}``.
- :func:`sync_probes_to_db` — upsert ``Probe`` rows from discovered probes.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from sqlalchemy import select

from homelab_helper.db.models import Probe as ProbeRow

from .base import Probe

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


ENTRY_POINT_GROUP = "homelab_helper.probes"


def discover_probes() -> dict[str, type[Probe]]:
    """Load all probes registered as entry points; return ``{name: class}``.

    Misbehaving entry points (import errors, non-Probe targets) are skipped
    with a printed warning rather than aborting discovery — one bad probe
    shouldn't keep the framework from booting.
    """

    found: dict[str, type[Probe]] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            cls = ep.load()
        except Exception as exc:  # pragma: no cover - import-side defense
            print(f"warning: failed to load probe entry point {ep.name!r}: {exc}")
            continue
        if not (isinstance(cls, type) and issubclass(cls, Probe)):
            print(f"warning: entry point {ep.name!r} is not a Probe subclass; skipped")
            continue
        if cls.__abstractmethods__:
            print(f"warning: entry point {ep.name!r} is abstract; skipped")
            continue
        if not getattr(cls, "name", None):
            print(f"warning: entry point {ep.name!r} has no class-level name; skipped")
            continue
        found[cls.name] = cls
    return found


async def sync_probes_to_db(session: AsyncSession) -> tuple[int, int]:
    """Upsert ``Probe`` rows from discovered entry points.

    Returns ``(inserted, updated)``. Idempotent: a second run with no
    classpath changes is a no-op (every row's ``updated_at`` may bump,
    but the canonical fields are unchanged).
    """

    discovered = discover_probes()
    existing_rows = (await session.execute(select(ProbeRow))).scalars().all()
    by_name = {row.name: row for row in existing_rows}

    inserted = 0
    updated = 0

    for name, cls in discovered.items():
        module_path = f"{cls.__module__}:{cls.__qualname__}"
        if name in by_name:
            row = by_name[name]
            row.version = cls.version
            row.module_path = module_path
            row.schema_version = cls.schema_version
            row.required_privilege = cls.required_privilege
            row.produces_keys = list(cls.produces_keys)
            row.description = cls.description
            updated += 1
        else:
            session.add(
                ProbeRow(
                    name=name,
                    version=cls.version,
                    module_path=module_path,
                    schema_version=cls.schema_version,
                    required_privilege=cls.required_privilege,
                    produces_keys=list(cls.produces_keys),
                    description=cls.description,
                    enabled=True,
                )
            )
            inserted += 1

    return inserted, updated


__all__ = ["ENTRY_POINT_GROUP", "discover_probes", "sync_probes_to_db"]
