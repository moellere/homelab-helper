"""Tests for the host.pci and host.gpu probe parsers."""

from __future__ import annotations

from homelab_helper.probes.host.gpu import select_gpus
from homelab_helper.probes.host.pci import parse_lspci, split_code

_LSPCI = """Slot:\t00:00.0
Class:\tHost bridge [0600]
Vendor:\tIntel Corporation [8086]
Device:\t8th Gen Core Processor Host Bridge [3ed0]
Rev:\t08

Slot:\t00:02.0
Class:\tVGA compatible controller [0300]
Vendor:\tIntel Corporation [8086]
Device:\tCoffee Lake-U GT2 [UHD Graphics 620] [3ea9]
Rev:\t01

Slot:\t01:00.0
Class:\t3D controller [0302]
Vendor:\tNVIDIA Corporation [10de]
Device:\tGA106 [RTX 3060] [2503]
"""


def test_split_code_takes_last_bracket_group() -> None:
    # Device names can contain their own brackets — code is the trailing group.
    assert split_code("VGA compatible controller [0300]") == ("VGA compatible controller", "0300")
    assert split_code("Coffee Lake-U GT2 [UHD Graphics 620] [3ea9]") == (
        "Coffee Lake-U GT2 [UHD Graphics 620]",
        "3ea9",
    )
    assert split_code("No code here") == ("No code here", None)


def test_parse_lspci_blocks() -> None:
    devs = parse_lspci(_LSPCI)
    assert [d["slot"] for d in devs] == ["00:00.0", "00:02.0", "01:00.0"]
    vga = devs[1]
    assert vga["class_code"] == "0300"
    assert vga["vendor_id"] == "8086"
    assert vga["device"] == "Coffee Lake-U GT2 [UHD Graphics 620]"
    assert vga["device_id"] == "3ea9"


def test_parse_lspci_empty() -> None:
    assert parse_lspci("") == []


def test_select_gpus_filters_class_03xx() -> None:
    gpus = select_gpus(parse_lspci(_LSPCI))
    # VGA (0300) + 3D (0302); the host bridge (0600) is excluded.
    assert {g["slot"] for g in gpus} == {"00:02.0", "01:00.0"}
    assert any("NVIDIA" in (g["vendor"] or "") for g in gpus)
