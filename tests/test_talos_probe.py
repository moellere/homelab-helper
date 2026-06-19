"""Tests for the talos.host probe — parsers + a full run against a fake adapter.

The fake adapter returns canned COSI resource docs (modeled on a real Pi 4
Talos node) so the probe is exercised end-to-end without a cluster. Assertions
focus on the canonical ``host.*`` keys the reconciler downstream depends on,
plus the forged-WWID / symlink-serial and physical-NIC-filter behavior.
"""

from __future__ import annotations

from typing import Any

from homelab_helper.probes.base import AdapterRegistry, ProbeContext, ProbeTarget
from homelab_helper.probes.talos.host import (
    TalosHostProbe,
    _build_interfaces,
    _disk_doc_to_device,
    _link_is_physical,
    parse_cpuinfo,
    parse_meminfo_total_bytes,
)

_CPUINFO_ARM = """processor\t: 0
Features\t: fp asimd evtstrm crc32 cpuid
processor\t: 1
Features\t: fp asimd evtstrm crc32 cpuid
processor\t: 2
Features\t: fp asimd evtstrm crc32 cpuid
processor\t: 3
Features\t: fp asimd evtstrm crc32 cpuid
"""

_MEMINFO = "MemTotal:        3870184 kB\nMemFree:          264620 kB\n"


def test_parse_cpuinfo_counts_cores_and_reads_arm_features() -> None:
    cpu = parse_cpuinfo(_CPUINFO_ARM)
    assert cpu["cores"] == 4
    assert "asimd" in cpu["flags"]
    assert "aes" not in cpu["flags"]  # Pi 4 has no AES — drives a real finding


def test_parse_meminfo_total_bytes() -> None:
    assert parse_meminfo_total_bytes(_MEMINFO) == 3870184 * 1024
    assert parse_meminfo_total_bytes("nothing here") is None


def test_disk_doc_skips_virtual_and_extracts_symlink_serial() -> None:
    loop = {"metadata": {"id": "loop0"}, "spec": {"bus_path": "/virtual", "size": 1}}
    assert _disk_doc_to_device(loop) is None

    sda = {
        "metadata": {"id": "sda"},
        "spec": {
            "bus_path": "/platform/.../usb4/4-2",
            "model": "ExampleSSD-256G",
            "transport": "usb",
            "wwid": "naa.5000000000000099",
            "size": 256060514304,
            "symlinks": [
                "/dev/disk/by-id/ata-EXAMPLEMODEL256G_SN0001",
                "/dev/disk/by-id/usb-EXAMPLEMODEL256G_SN0001-0:0",
            ],
        },
    }
    dev = _disk_doc_to_device(sda)
    assert dev is not None
    assert dev["type"] == "disk"
    assert dev["wwn"] == "naa.5000000000000099"  # forged, but reported faithfully
    assert dev["serial"] == "SN0001"  # real per-drive id from symlink
    assert dev["transport"] == "usb"


def test_link_is_physical_positive_filter() -> None:
    physical = {"type": "ether", "kind": "", "busPath": "fd580000.ethernet"}
    bridge = {"type": "ether", "kind": "bridge", "busPath": "N/A"}
    dummy = {"type": "ether", "kind": "dummy"}
    tunnel = {"type": "tunnel6", "kind": "", "busPath": "x"}
    assert _link_is_physical(physical) is True
    assert _link_is_physical(bridge) is False
    assert _link_is_physical(dummy) is False
    assert _link_is_physical(tunnel) is False


def test_build_interfaces_keeps_only_physical_with_addresses() -> None:
    links = [
        {
            "metadata": {"id": "end0"},
            "spec": {
                "type": "ether",
                "kind": "",
                "busPath": "fd580000.ethernet",
                "hardwareAddr": "02:00:01:42:fd:24",
                "mtu": 1500,
                "operationalState": "up",
                "speedMbit": 1000,
            },
        },
        {"metadata": {"id": "cni0"}, "spec": {"type": "ether", "kind": "bridge", "busPath": "N/A"}},
        {"metadata": {"id": "dummy0"}, "spec": {"type": "ether", "kind": "dummy"}},
    ]
    addresses = [
        {"spec": {"linkName": "end0", "address": "10.0.6.17/23", "family": "inet4"}},
        {"spec": {"linkName": "cni0", "address": "10.244.1.1/24", "family": "inet4"}},
    ]
    ifaces = _build_interfaces(links, addresses)
    assert [i["name"] for i in ifaces] == ["end0"]  # cni0/dummy0 filtered out
    assert ifaces[0]["mac"] == "02:00:01:42:fd:24"
    assert ifaces[0]["link_type"] == "ether"
    assert ifaces[0]["addresses"] == [{"family": "inet", "ip": "10.0.6.17", "prefix": 23}]


class _FakeTalosAdapter:
    """Returns canned resource docs / file contents for one node."""

    def __init__(self) -> None:
        self._resources: dict[str, list[dict[str, Any]]] = {
            "nodename": [{"metadata": {"id": "nodename"}, "spec": {"nodename": "cp1"}}],
            "systeminformation": [
                {
                    "metadata": {"id": "systeminformation"},
                    "spec": {
                        "manufacturer": "examplevendor",
                        "productName": "Example ARM SBC",
                        "uuid": "30303031-3030-3030-6436-616363383600",
                    },
                }
            ],
            "disks": [
                {"metadata": {"id": "loop0"}, "spec": {"bus_path": "/virtual", "size": 1}},
                {
                    "metadata": {"id": "sda"},
                    "spec": {
                        "bus_path": "/platform/usb4/4-2",
                        "model": "ExampleSSD-256G",
                        "transport": "usb",
                        "wwid": "naa.5000000000000099",
                        "size": 256060514304,
                        "symlinks": ["/dev/disk/by-id/ata-EXAMPLEMODEL256G_SN0001"],
                    },
                },
            ],
            "links": [
                {
                    "metadata": {"id": "end0"},
                    "spec": {
                        "type": "ether",
                        "kind": "",
                        "busPath": "fd580000.ethernet",
                        "hardwareAddr": "02:00:01:42:fd:24",
                        "mtu": 1500,
                        "operationalState": "up",
                        "speedMbit": 1000,
                    },
                },
                {"metadata": {"id": "cni0"}, "spec": {"type": "ether", "kind": "bridge"}},
            ],
            "addresses": [
                {"spec": {"linkName": "end0", "address": "10.0.6.17/23", "family": "inet4"}}
            ],
        }
        self._files = {"/proc/cpuinfo": _CPUINFO_ARM, "/proc/meminfo": _MEMINFO}

    async def get_resources(self, node: str, resource: str) -> list[dict[str, Any]]:  # noqa: ARG002
        return self._resources.get(resource, [])

    async def read_file(self, node: str, path: str) -> str:  # noqa: ARG002
        return self._files[path]

    async def version(self, node: str) -> dict[str, str]:  # noqa: ARG002
        return {"arch": "arm64", "os_arch": "linux/arm64", "tag": "v1.12.6"}


def _ctx() -> ProbeContext:
    return ProbeContext(
        target=ProbeTarget(kind="talos", host_id="h1", hostname="cp1", primary_ip="10.0.6.17"),
        adapters=AdapterRegistry({"talos": _FakeTalosAdapter()}),
        run_id="run-1",
    )


async def test_probe_emits_canonical_host_keys() -> None:
    result = await TalosHostProbe().run(_ctx())
    assert result.success, result.error
    obs = {o.key: o.value for o in result.observations}

    assert obs["host.identity.hostname"] == "cp1"
    assert obs["host.identity.os_id"] == "talos"
    assert obs["host.identity.machine_id"] == "30303031-3030-3030-6436-616363383600"
    assert obs["host.cpu.architecture"] == "arm64"
    assert obs["host.cpu.cores"] == 4
    assert obs["host.cpu.vendor"] == "examplevendor"
    assert "aes" not in obs["host.cpu.interesting_flags"]
    assert obs["host.memory.mem_total_bytes"] == 3870184 * 1024

    # Storage lands on the exact key/shape the reconciler reads.
    devices = obs["host.storage.devices"]
    assert obs["host.storage.disk_count"] == 1
    assert devices[0]["type"] == "disk"
    assert devices[0]["wwn"] == "naa.5000000000000099"

    # Only the physical NIC survives.
    assert obs["host.network.interface_count"] == 1
    assert obs["host.network.interfaces"][0]["name"] == "end0"


async def test_probe_wrong_target_kind_fails() -> None:
    ctx = ProbeContext(
        target=ProbeTarget(kind="host", hostname="x"),
        adapters=AdapterRegistry({"talos": _FakeTalosAdapter()}),
        run_id="r",
    )
    result = await TalosHostProbe().run(ctx)
    assert not result.success
    assert "unsupported target kind" in (result.error or "")
