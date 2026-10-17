"""DIMM fit — what one more DIMM actually does to a host (#106).

A board has a fixed number of slots. With one free, a DIMM adds its whole
size. With none, it can only replace an installed DIMM, so the host gains the
difference, and a swap that gains nothing is not a move. The board's stated
maximum (SMBIOS ``Maximum Capacity``, ``memory_max_bytes``) is advisory: boards
routinely run more than they claim, so exceeding it is a warning, never a
refusal. A host whose slots were never observed keeps the additive answer and
says so, rather than reading "unknown" as "full" or "free".

The surplus reasoner and the rebalance solver both ask this one question, so
neither can propose a DIMM into a board that has no room for it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

_GB = 1024**3


@dataclass(frozen=True)
class DimmFit:
    net_bytes: int
    """What the host's RAM grows by."""
    replaces_bytes: int | None = None
    """The installed DIMM it displaces; None when it takes a free slot."""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Board:
    free_slots: int | None
    """None when the slot count was never observed."""
    installed: tuple[int, ...] = ()
    max_bytes: int | None = None

    @classmethod
    def of(cls, capabilities: dict[str, Any] | None, installed: list[int]) -> Board:
        caps = capabilities or {}
        free = caps.get("dimm_slots_free")
        max_bytes = caps.get("memory_max_bytes")
        return cls(
            free_slots=int(free) if free is not None else None,
            installed=tuple(sorted(installed)),
            max_bytes=int(max_bytes) if max_bytes else None,
        )

    def fit(self, size: int) -> DimmFit | None:
        """How ``size`` bytes of DIMM lands here, or None when it cannot gain anything."""
        warnings: list[str] = []
        if self.free_slots is None:
            fit = DimmFit(net_bytes=size)
            warnings.append("slot count not observed: assumes a free slot")
        elif self.free_slots > 0:
            fit = DimmFit(net_bytes=size)
        elif not self.installed:
            return None  # full, and which DIMM would go is unknown
        else:
            smallest = self.installed[0]
            if size <= smallest:
                return None
            fit = DimmFit(net_bytes=size - smallest, replaces_bytes=smallest)
        total = sum(self.installed) + fit.net_bytes
        if self.max_bytes and self.installed and total > self.max_bytes:
            warnings.append(
                f"{total / _GB:.0f} GiB exceeds the board's stated "
                f"{self.max_bytes / _GB:.0f} GiB maximum (boards often run more; check the model)"
            )
        return replace(fit, warnings=tuple(warnings))

    def after(self, size: int, fit: DimmFit) -> Board:
        """The board once ``fit`` has been applied, for placing a second DIMM."""
        installed = list(self.installed)
        if fit.replaces_bytes is not None:
            installed.remove(fit.replaces_bytes)
        installed.append(size)
        free = self.free_slots
        if free is not None and fit.replaces_bytes is None:
            free -= 1
        return Board(free_slots=free, installed=tuple(sorted(installed)), max_bytes=self.max_bytes)


__all__ = ["Board", "DimmFit"]
