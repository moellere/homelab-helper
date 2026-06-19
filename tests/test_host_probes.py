"""Unit tests for the parse helpers of host.cpu / host.memory / host.network / host.storage / host.services.

These tests don't go over SSH — they exercise the pure-function parsers
on canned command output. Real SSH paths are covered by the opt-in
integration tests in test_integration_ssh.py.
"""

from __future__ import annotations

import json

import pytest

from homelab_helper.probes.host.cpu import (
    HostCpuOutput,
    build_output,
    flatten_lscpu_json,
    parse_cache_bytes,
    parse_freq_mhz,
    parse_int_or_none,
    parse_proc_cpuinfo,
)
from homelab_helper.probes.host.memory import parse_meminfo
from homelab_helper.probes.host.network import parse_ip_link_show, parse_speed_lines
from homelab_helper.probes.host.services import parse_systemctl_units
from homelab_helper.probes.host.storage import parse_lsblk_json


def test_flatten_lscpu_json_recurses_into_children() -> None:
    payload = {
        "lscpu": [
            {"field": "Architecture:", "data": "x86_64"},
            {
                "field": "CPU(s):",
                "data": "8",
                "children": [
                    {"field": "Core(s) per socket:", "data": "4"},
                    {"field": "Socket(s):", "data": "1"},
                ],
            },
        ]
    }
    flat = flatten_lscpu_json(payload)
    assert flat["Architecture"] == "x86_64"
    assert flat["CPU(s)"] == "8"
    assert flat["Core(s) per socket"] == "4"


def test_parse_int_or_none() -> None:
    assert parse_int_or_none("4") == 4
    assert parse_int_or_none("1,024 something") == 1024
    assert parse_int_or_none(None) is None
    assert parse_int_or_none("garbage") is None


def test_parse_freq_mhz() -> None:
    assert parse_freq_mhz("2300.0000") == 2300
    assert parse_freq_mhz("3400") == 3400
    assert parse_freq_mhz("garbage") is None


def test_parse_cache_bytes_kib_mib_and_instances() -> None:
    assert parse_cache_bytes("256 KiB (4 instances)") == 256 * 1024 * 4
    assert parse_cache_bytes("12 MiB") == 12 * 1024 * 1024
    assert parse_cache_bytes("32K") == 32 * 1000
    assert parse_cache_bytes("") is None


def test_parse_proc_cpuinfo_first_block() -> None:
    raw = "processor\t: 0\nvendor_id\t: GenuineIntel\nmodel name\t: Example CPU\n\nprocessor\t: 1\n"
    parsed = parse_proc_cpuinfo(raw)
    assert parsed["model name"] == "Example CPU"
    assert parsed["vendor_id"] == "GenuineIntel"


def test_build_output_combines_lscpu_and_cpuinfo() -> None:
    lscpu = {
        "Architecture": "x86_64",
        "Vendor ID": "GenuineIntel",
        "Model name": "Example CPU @ 1.60GHz",
        "Socket(s)": "1",
        "Core(s) per socket": "4",
        "Thread(s) per core": "2",
        "CPU(s)": "8",
        "L3 cache": "6 MiB",
        "Flags": "fpu vme avx avx2 vmx aes",
    }
    out = build_output(lscpu, {})
    assert isinstance(out, HostCpuOutput)
    assert "Example CPU" in (out.model or "")
    assert out.cores == 4
    assert out.threads == 8
    assert out.threads_per_core == 2
    assert out.l3_bytes == 6 * 1024 * 1024
    assert set(out.interesting_flags) >= {"avx", "avx2", "vmx", "aes"}


def test_parse_meminfo_converts_kb_to_bytes() -> None:
    raw = "MemTotal:       16363716 kB\nMemFree:         5000000 kB\nHugePages_Total:       0\nHugepagesize:       2048 kB\n"
    info = parse_meminfo(raw)
    assert info["MemTotal"] == 16363716 * 1024
    assert info["MemFree"] == 5000000 * 1024
    assert info["HugePages_Total"] == 0
    assert info["Hugepagesize"] == 2048 * 1024


def test_parse_ip_link_show_drops_loopback_and_merges_addresses() -> None:
    link_json = json.dumps(
        [
            {
                "ifname": "lo",
                "link_type": "loopback",
                "address": "00:00:00:00:00:00",
                "mtu": 65536,
                "operstate": "UNKNOWN",
            },
            {
                "ifname": "eth0",
                "link_type": "ether",
                "address": "aa:bb:cc:dd:ee:ff",
                "mtu": 1500,
                "operstate": "UP",
            },
        ]
    )
    addr_json = json.dumps(
        [
            {
                "ifname": "eth0",
                "addr_info": [
                    {"family": "inet", "local": "10.0.6.20", "prefixlen": 23},
                    {"family": "inet6", "local": "fe80::1", "prefixlen": 64},
                ],
            }
        ]
    )
    interfaces = parse_ip_link_show(link_json, addr_json, {"eth0": 2500})
    assert len(interfaces) == 1
    eth0 = interfaces[0]
    assert eth0.name == "eth0"
    assert eth0.mac == "aa:bb:cc:dd:ee:ff"
    assert eth0.speed_mbps == 2500
    assert len(eth0.addresses) == 2


def test_parse_speed_lines_ignores_minus_one_and_invalid() -> None:
    raw = "/sys/class/net/eth0/speed 2500\n/sys/class/net/wlan0/speed -1\n/sys/class/net/eth1/speed garbage\n/sys/class/net/eno1/speed 1000\n"
    assert parse_speed_lines(raw) == {"eth0": 2500, "eno1": 1000}


def test_parse_lsblk_builds_tree_with_partitions() -> None:
    payload = {
        "blockdevices": [
            {
                "name": "nvme0n1",
                "type": "disk",
                "size": 512_110_190_592,
                "rota": False,
                "model": "Samsung SSD 980",
                "serial": "S5H7NJ0R600001",
                "tran": "nvme",
                "children": [
                    {
                        "name": "nvme0n1p1",
                        "type": "part",
                        "size": 536_870_912,
                        "fstype": "vfat",
                        "mountpoint": "/boot/efi",
                        "rota": False,
                    },
                    {
                        "name": "nvme0n1p2",
                        "type": "part",
                        "size": 511_572_267_008,
                        "fstype": "ext4",
                        "mountpoint": "/",
                        "rota": False,
                    },
                ],
            }
        ]
    }
    devices = parse_lsblk_json(json.dumps(payload))
    assert len(devices) == 1
    disk = devices[0]
    assert disk.type == "disk"
    assert disk.size_bytes == 512_110_190_592
    assert disk.rotational is False
    assert disk.transport == "nvme"
    assert len(disk.children) == 2
    assert disk.children[1].mountpoint == "/"
    assert disk.children[1].parent == "nvme0n1"


def test_parse_lsblk_handles_string_size() -> None:
    payload = {"blockdevices": [{"name": "sda", "type": "disk", "size": "humanized"}]}
    devices = parse_lsblk_json(json.dumps(payload))
    assert devices[0].size_bytes is None


def test_parse_lsblk_invalid_json_returns_empty() -> None:
    assert parse_lsblk_json("not json") == []


def test_parse_systemctl_units_extracts_service_lines() -> None:
    raw = (
        "  ssh.service                  loaded active running OpenBSD Secure Shell server\n"
        "  cron.service                 loaded active running Cron daemon\n"
        "  some-timer.timer             loaded active waiting Some Timer\n"
    )
    units = parse_systemctl_units(raw)
    names = [u.name for u in units]
    assert "ssh.service" in names
    assert "cron.service" in names
    assert "some-timer.timer" not in names


def test_parse_systemctl_units_handles_empty_input() -> None:
    assert parse_systemctl_units("") == []
    assert parse_systemctl_units("\n\n  \n") == []


@pytest.mark.parametrize(
    "probe_name",
    ["host.identity", "host.cpu", "host.memory", "host.network", "host.storage", "host.services"],
)
def test_probe_discovered_via_entry_points(probe_name: str) -> None:
    from homelab_helper.probes.registry import discover_probes

    probes = discover_probes()
    assert probe_name in probes
    cls = probes[probe_name]
    assert cls.name == probe_name
    assert "host" in cls.target_kinds
    assert cls.produces_keys
