"""Adapter write methods are reachable only through the executor.

The Proxmox write surface (``vm_power``, ``create_snapshot``,
``rollback_snapshot``) exists solely for ``engine/executor.py`` and the
rollback orchestrator it drives; the contract used to be a block comment plus
grep-ability. This makes the grep a test: no other module under
``src/homelab_helper`` may name those methods.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "homelab_helper"

WRITE_METHODS = ("vm_power", "create_snapshot", "rollback_snapshot")
ALLOWED = {
    SRC / "adapters" / "proxmox.py",  # the definitions
    SRC / "engine" / "executor.py",  # the gate's enforcement point
    SRC / "engine" / "rollback.py",  # driven by the executor
}


def _callers() -> dict[str, list[str]]:
    pattern = re.compile(r"\.(" + "|".join(WRITE_METHODS) + r")\(")
    hits: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if path in ALLOWED:
            continue
        for match in pattern.finditer(path.read_text()):
            hits.setdefault(str(path.relative_to(SRC)), []).append(match.group(1))
    return hits


def test_no_module_outside_the_executor_calls_a_proxmox_write() -> None:
    assert _callers() == {}


def test_allowed_files_still_exist() -> None:
    """If one of these moves, the allowlist must move with it — not silently pass."""
    for path in ALLOWED:
        assert path.is_file(), path
