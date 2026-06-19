"""ConfigurationAssertion library loader — YAML in, DB rows out.

The starter library ships in ``fixtures/assertion-library-starter.yaml`` as
operator-editable text. Reload is idempotent: entries upsert by ``name``, so
editing the YAML and re-loading produces the change exactly. Missing hosts
(referenced by ``host:`` but not yet in the harness DB) are skipped with a
reason, never an error — the library is intentionally tolerant so an operator
can land it before every host has been discovered.

YAML schema (version 1)::

    version: 1
    assertions:
      - name: node0.has_cpu_observation
        host: node0                  # optional — sets scope=host, scope_target=<uuid>
        description: node0 should have been observed by host.cpu
        rationale: Probe coverage gap means downstream planners run on stale info
        severity_on_fail: medium     # default: medium
        kind: observation-predicate  # default: observation-predicate
        schedule: null               # P2; ignored today
        artifact_link: null
        enabled: true
        verifier_spec:
          key: host.cpu.cores
          predicate: exists

Entries without a ``host:`` field default to ``scope: global``; provide
``scope:`` and ``scope_target:`` explicitly when that's intended.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml
from sqlalchemy import select

from homelab_helper.db.enums import (
    AssertionKind,
    AssertionScope,
    FindingSeverity,
)
from homelab_helper.db.models import ConfigurationAssertion, Host

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

_LIBRARY_VERSION = 1


@dataclass
class LoadResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # ``skipped`` is (name, reason). ``unchanged`` are entries that matched the
    # DB row exactly (idempotent re-load).

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated) + len(self.unchanged) + len(self.skipped)


class LibraryError(ValueError):
    """Raised when the YAML can't be interpreted as a v1 assertion library."""


def parse_library_yaml(text: str) -> list[dict[str, Any]]:
    """Parse + structurally validate. Returns the raw entry list.

    Doesn't touch the DB; lets the CLI's ``--dry-run`` mode print what would
    happen without opening a session.
    """
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise LibraryError("top-level must be a mapping (got non-dict YAML)")
    version = raw.get("version")
    if version != _LIBRARY_VERSION:
        raise LibraryError(f"unsupported library version {version!r}; expected {_LIBRARY_VERSION}")
    entries = raw.get("assertions")
    if not isinstance(entries, list):
        raise LibraryError("'assertions' must be a list")
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise LibraryError(f"assertions[{i}] must be a mapping")
    return entries


async def load_library(
    session: AsyncSession,
    path: Path,
    *,
    dry_run: bool = False,
) -> LoadResult:
    """Read the YAML at ``path``, upsert ConfigurationAssertion rows.

    With ``dry_run=True``, nothing is written; the result still reports
    created/updated/skipped buckets so the operator can preview the diff.
    """
    text = path.read_text()  # noqa: ASYNC240 — one-off small config file
    entries = parse_library_yaml(text)
    result = LoadResult()
    for entry in entries:
        await _load_one(session, entry, result, dry_run=dry_run)
    if not dry_run:
        await session.flush()
    return result


async def _resolve_scope(
    session: AsyncSession,
    name: str,
    entry: dict[str, Any],
    result: LoadResult,
) -> tuple[AssertionScope, str | None] | None:
    """Resolve `host:` / `scope:` / `scope_target:` into the DB-ready pair.

    Returns ``None`` and appends to ``result.skipped`` if the entry is invalid.
    """
    host_name = entry.get("host")
    if isinstance(host_name, str) and host_name:
        host = (
            await session.execute(select(Host).where(Host.hostname == host_name))
        ).scalar_one_or_none()
        if host is None:
            result.skipped.append((name, f"host {host_name!r} not in harness DB"))
            return None
        return AssertionScope.HOST, str(host.id)
    try:
        scope = AssertionScope(entry.get("scope", "global"))
    except ValueError:
        result.skipped.append((name, f"unknown scope {entry.get('scope')!r}"))
        return None
    scope_target = entry.get("scope_target")
    if scope_target is not None and not isinstance(scope_target, str):
        result.skipped.append((name, "scope_target must be a string"))
        return None
    return scope, scope_target


def _build_fields(
    name: str,
    entry: dict[str, Any],
    scope: AssertionScope,
    scope_target: str | None,
    result: LoadResult,
) -> dict[str, Any] | None:
    """Build the DB-row field dict, or skip-record if the entry is malformed."""
    spec = entry.get("verifier_spec")
    if not isinstance(spec, dict) or not spec:
        result.skipped.append((name, "missing or empty 'verifier_spec'"))
        return None
    description = entry.get("description")
    if not isinstance(description, str) or not description:
        result.skipped.append((name, "missing 'description'"))
        return None
    try:
        kind = AssertionKind(entry.get("kind", "observation-predicate"))
    except ValueError:
        result.skipped.append((name, f"unknown kind {entry.get('kind')!r}"))
        return None
    try:
        severity = FindingSeverity(entry.get("severity_on_fail", "medium"))
    except ValueError:
        result.skipped.append((name, f"unknown severity_on_fail {entry.get('severity_on_fail')!r}"))
        return None
    return {
        "scope": scope,
        "scope_target": scope_target,
        "description": description,
        "rationale": entry.get("rationale"),
        "kind": kind,
        "verifier_spec": spec,
        "severity_on_fail": severity,
        "schedule": entry.get("schedule"),
        "artifact_link": entry.get("artifact_link"),
        "enabled": bool(entry.get("enabled", True)),
    }


async def _load_one(
    session: AsyncSession,
    entry: dict[str, Any],
    result: LoadResult,
    *,
    dry_run: bool,
) -> None:
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        result.skipped.append(("<unnamed>", "missing 'name'"))
        return

    scope_pair = await _resolve_scope(session, name, entry, result)
    if scope_pair is None:
        return
    scope, scope_target = scope_pair

    fields = _build_fields(name, entry, scope, scope_target, result)
    if fields is None:
        return

    existing = (
        await session.execute(
            select(ConfigurationAssertion).where(ConfigurationAssertion.name == name)
        )
    ).scalar_one_or_none()

    if existing is not None:
        if _matches(existing, fields):
            result.unchanged.append(name)
            return
        if not dry_run:
            for attr, value in fields.items():
                setattr(existing, attr, value)
        result.updated.append(name)
        return

    if not dry_run:
        session.add(ConfigurationAssertion(name=name, **fields))
    result.created.append(name)


def _matches(existing: ConfigurationAssertion, fields: dict[str, Any]) -> bool:
    """Compare row to the (validated) fields dict — used for unchanged-vs-updated."""
    return all(getattr(existing, attr) == value for attr, value in fields.items())


__all__ = ["LibraryError", "LoadResult", "load_library", "parse_library_yaml"]
