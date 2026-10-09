"""Service suggestions (Phase 8.7) — capability the lab owns but does not use.

Two deterministic checks, matched against the workload library:

- ``capability-idle-gpu`` — a host reporting a display/compute adapter while
  nothing the library knows as GPU-capable appears to be running. The library
  carries ``gpu`` and ``gpu_purpose`` per entry, so the suggestion can say what
  the silicon would be *for* rather than merely that it is idle.
- ``building-block-missing`` — one of the three pieces the roadmap names:
  metrics, alerting, and the local model the LLM router expects. Each is a set
  of library entries; the lab is missing the block when none of them is
  running.

**What "running" means here, precisely.** The harness does not track installed
software; it tracks guests, services and DNS endpoints. So presence is decided
by *name*: a library entry counts as present when a guest, service or endpoint
is named after it. That is a real limitation, not a hidden one — a guest called
``media`` running Plex is invisible to this check — so every finding says what
it matched on, and these are INFO suggestions rather than problems. Being wrong
here costs the operator a glance; pretending to certainty would cost more.

An LLM may narrate a suggestion; the matching is table-driven and lives here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.engine.category_findings import CategoryIssue

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from homelab_helper.engine.workloads import WorkloadProfile

_SPLIT = re.compile(r"[^a-z0-9]+")

BUILDING_BLOCKS: dict[str, tuple[str, tuple[str, ...]]] = {
    "metrics": (
        "nothing is collecting metrics",
        ("prometheus", "influxdb", "netdata", "grafana"),
    ),
    "alerting": (
        "nothing is watching for things going down",
        ("uptime-kuma", "grafana", "prometheus"),
    ),
    "local-llm": (
        "the LLM router prefers a local model and none is running",
        ("ollama",),
    ),
}
"""Block → (what its absence means, library entries that would provide it).
Deliberately short: the roadmap names metrics, alerting and a local model, and
a longer list would be this module inventing policy."""


def normalize(name: str) -> str:
    """``Plex-Media_Server`` and ``plex media server`` compare equal."""
    return _SPLIT.sub("-", name.strip().lower()).strip("-")


def present_names(*sources: Iterable[str]) -> set[str]:
    """Every normalised name the lab knows, from guests, services and endpoints."""
    return {normalize(n) for source in sources for n in source if n and n.strip()}


def _matches(entry: str, present: set[str]) -> bool:
    """A library entry is present when a name equals it or contains it as a word.

    Substring alone would match ``postgres`` inside ``postgres-backup-cleaner``,
    which is fine, and ``loki`` inside ``lokiadmin``, which is not — so the
    containment test is on hyphen-separated words, not raw characters.
    """
    target = normalize(entry)
    if target in present:
        return True
    return any(target in name.split("-") for name in present)


def _issue(**kw: Any) -> CategoryIssue:
    return CategoryIssue(kind=FindingKind.SERVICE_SUGGESTION, **kw)


def idle_gpu_issues(
    hosts: Iterable[tuple[str, str, Mapping[str, Any]]],
    library: Mapping[str, WorkloadProfile],
    present: set[str],
) -> list[CategoryIssue]:
    """Hosts with a GPU where no GPU-capable library entry seems to be running.

    ``hosts`` entries are ``(host_id, hostname, capabilities)``.
    """
    candidates = {
        name: profile
        for name, profile in library.items()
        if profile.gpu != "none" and profile.gpu_purpose
    }
    running = sorted(name for name in candidates if _matches(name, present))
    if running:
        return []  # something is already using (or could use) the silicon

    issues: list[CategoryIssue] = []
    for host_id, hostname, capabilities in hosts:
        count = capabilities.get("gpu_count") or 0
        if not count:
            continue
        vendors = capabilities.get("gpu_vendors") or []
        vendor_label = ", ".join(str(v) for v in vendors) if vendors else "unknown vendor"
        offers = sorted(candidates.items())[:4]
        suggestions = "; ".join(f"{name} ({profile.gpu_purpose})" for name, profile in offers)
        issues.append(
            _issue(
                category="capability-idle-gpu",
                target_type="host",
                target_id=host_id,
                severity=FindingSeverity.INFO,
                title=f"{hostname} has a GPU nothing appears to use",
                description=(
                    f"{count} display/compute adapter(s) ({vendor_label}) and no guest, service "
                    f"or endpoint is named after a GPU-capable workload the library knows. "
                    f"Candidates: {suggestions}. Matched on names only, so a guest named for "
                    "something else that already uses the GPU would not show up here."
                ),
                evidence={
                    "gpu_count": count,
                    "gpu_vendors": list(vendors),
                    "candidates": [name for name, _ in offers],
                },
            )
        )
    return issues


def building_block_issues(
    library: Mapping[str, WorkloadProfile], present: set[str]
) -> list[CategoryIssue]:
    """One issue per building block nothing in the lab provides."""
    issues: list[CategoryIssue] = []
    for block, (meaning, entries) in sorted(BUILDING_BLOCKS.items()):
        known = [name for name in entries if name in library]
        if not known or any(_matches(name, present) for name in known):
            continue
        issues.append(
            _issue(
                category="building-block-missing",
                target_type="lab",
                target_id=block,
                severity=FindingSeverity.INFO,
                title=f"No {block}: {meaning}",
                description=(
                    "Nothing is named after "
                    + ", ".join(known)
                    + ". The library sizes the lightest of these at "
                    + f"{min(library[n].ram_mb for n in known)} MiB RAM. "
                    "Matched on names only."
                ),
                evidence={"block": block, "candidates": known},
            )
        )
    return issues


__all__ = [
    "BUILDING_BLOCKS",
    "building_block_issues",
    "idle_gpu_issues",
    "normalize",
    "present_names",
]
