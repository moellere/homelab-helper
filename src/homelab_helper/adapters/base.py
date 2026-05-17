"""Adapter Protocol — the uniform interface every adapter must implement.

This file pins the shape; concrete adapters in sibling modules implement it.
Kept minimal at scaffold time; the protocol body will fill in as the first
adapter (KernelSSHAdapter) lands.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Adapter(Protocol):
    """Common shape for any external source the harness talks to.

    Concrete adapter docstrings document the full method semantics; this
    Protocol exists primarily so the engine layer can be typed against
    "any adapter" without importing concrete classes.
    """

    name: str

    def health_check(self) -> object:  # placeholder return type
        """Return a HealthStatus describing whether the adapter is reachable."""
        ...


__all__ = ["Adapter"]
