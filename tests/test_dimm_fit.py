"""DIMM fit rule tests (#106) — free slots add, full boards swap, maxima only warn."""

from __future__ import annotations

from homelab_helper.engine.dimm_fit import Board

_GB = 1024**3


def _board(installed_gb: list[int], free: int | None, max_gb: int | None = None) -> Board:
    caps: dict[str, int] = {}
    if free is not None:
        caps["dimm_slots_free"] = free
    if max_gb is not None:
        caps["memory_max_bytes"] = max_gb * _GB
    return Board.of(caps, [g * _GB for g in installed_gb])


def test_a_free_slot_adds_the_whole_dimm() -> None:
    fit = _board([16], free=1).fit(8 * _GB)
    assert fit is not None
    assert fit.net_bytes == 8 * _GB
    assert fit.replaces_bytes is None
    assert fit.warnings == ()


def test_a_full_board_swaps_out_its_smallest_dimm() -> None:
    fit = _board([16, 4], free=0).fit(8 * _GB)
    assert fit is not None
    assert fit.net_bytes == 4 * _GB
    assert fit.replaces_bytes == 4 * _GB


def test_a_swap_that_gains_nothing_is_no_fit() -> None:
    assert _board([16, 16], free=0).fit(8 * _GB) is None
    assert _board([8, 16], free=0).fit(8 * _GB) is None


def test_a_full_board_with_unknown_dimms_is_no_fit() -> None:
    assert _board([], free=0).fit(8 * _GB) is None


def test_unobserved_slots_assume_a_free_slot_and_say_so() -> None:
    fit = _board([16, 16], free=None).fit(8 * _GB)
    assert fit is not None
    assert fit.net_bytes == 8 * _GB
    assert fit.warnings == ("slot count not observed: assumes a free slot",)


def test_the_stated_maximum_warns_and_never_refuses() -> None:
    """Boards run past SMBIOS Maximum Capacity: a 7060 says 32 GB and runs 40."""
    fit = _board([32], free=1, max_gb=32).fit(8 * _GB)
    assert fit is not None
    assert fit.net_bytes == 8 * _GB
    assert "40 GiB exceeds the board's stated 32 GiB maximum" in fit.warnings[0]


def test_after_consumes_the_free_slot_then_swaps() -> None:
    board = _board([16], free=1)
    first = board.fit(8 * _GB)
    assert first is not None
    board = board.after(8 * _GB, first)
    assert board.free_slots == 0
    assert board.installed == (8 * _GB, 16 * _GB)
    assert board.fit(8 * _GB) is None  # only an equal 8 GiB left to replace
    second = board.fit(32 * _GB)
    assert second is not None
    assert second.replaces_bytes == 8 * _GB
