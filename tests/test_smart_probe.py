"""Tests for the host.smart probe — pure parsers + a run against a fake adapter."""

from __future__ import annotations

import json
from typing import Any

from homelab_helper.probes.base import AdapterRegistry, ProbeContext, ProbeTarget
from homelab_helper.probes.host.smart import (
    HostSmartProbe,
    _is_unhealthy,
    parse_smart_device,
    parse_smart_scan,
)

_SCAN = json.dumps(
    {"devices": [{"name": "/dev/sda", "type": "scsi"}, {"name": "/dev/sde", "type": "scsi"}]}
)

_SSD = json.dumps(
    {
        "device": {"name": "/dev/sda", "protocol": "ATA"},
        "model_name": "ExampleSSD-256G",
        "serial_number": "SNDISK01",
        "wwn": {"naa": 5, "oui": 291, "id": 1110},
        "smart_status": {"passed": True},
        "power_on_time": {"hours": 16019},
        "temperature": {"current": 29},
        "rotation_rate": 0,
        "ata_smart_attributes": {
            "table": [
                {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 0}},
                {"id": 199, "name": "UDMA_CRC_Error_Count", "raw": {"value": 0}},
            ]
        },
    }
)

# A failing drive: SMART overall failed + reallocated sectors.
_FAILING = json.dumps(
    {
        "device": {"name": "/dev/sde", "protocol": "ATA"},
        "model_name": "ExampleHDD-3T",
        "serial_number": "MK0000",
        "smart_status": {"passed": False},
        "rotation_rate": 7200,
        "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 8}}]},
    }
)


def test_parse_scan_extracts_names() -> None:
    assert parse_smart_scan(_SCAN) == ["/dev/sda", "/dev/sde"]
    assert parse_smart_scan("not json") == []


def test_parse_device_ssd_fields_and_wwn() -> None:
    d = parse_smart_device(_SSD)
    assert d is not None
    assert d["model"] == "ExampleSSD-256G"
    assert d["health_passed"] is True
    assert d["power_on_hours"] == 16019
    assert d["rotation_rate"] == 0
    assert d["reallocated_sectors"] == 0
    # WWN formatted to lsblk-style 0x… so it cross-references storage parts.
    assert d["wwn"] == "0x5000123000000456"


def test_is_unhealthy_logic() -> None:
    assert _is_unhealthy(parse_smart_device(_SSD)) is False
    assert _is_unhealthy(parse_smart_device(_FAILING)) is True
    # CRC errors alone are a cable signal, not drive failure.
    assert _is_unhealthy({"health_passed": True, "crc_errors": 58}) is False
    assert _is_unhealthy({"health_passed": True, "pending_sectors": 3}) is True


class _FakeSSH:
    """Returns canned smartctl output keyed by command substring."""

    def __init__(self, *, scan: str, devices: dict[str, str]) -> None:
        self._scan = scan
        self._devices = devices

    async def run(self, command: str, **_: Any):  # noqa: ANN003
        from homelab_helper.adapters.kernel_ssh import CommandResult

        if "--scan" in command:
            out = self._scan
        else:
            out = next((v for k, v in self._devices.items() if k in command), "")
        # smartctl uses a bitmask exit; non-zero even when stdout is valid.
        return CommandResult(command=command, stdout=out, stderr="", exit_code=4)


class _FakeAdapter:
    def __init__(self, ssh: _FakeSSH) -> None:
        self._ssh = ssh

    def session(self, *_: Any, **__: Any):
        ssh = self._ssh

        class _Ctx:
            async def __aenter__(self):
                return ssh

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


def _ctx(adapter: _FakeAdapter) -> ProbeContext:
    return ProbeContext(
        target=ProbeTarget(kind="host", host_id="h1", hostname="nas0", ssh_user="root"),
        adapters=AdapterRegistry({"kernel-ssh": adapter}),
        run_id="r1",
    )


async def test_probe_emits_smart_observations_and_flags_unhealthy() -> None:
    adapter = _FakeAdapter(_FakeSSH(scan=_SCAN, devices={"/dev/sda": _SSD, "/dev/sde": _FAILING}))
    result = await HostSmartProbe().run(_ctx(adapter))
    assert result.success, result.error
    obs = {o.key: o.value for o in result.observations}
    assert obs["host.smart.device_count"] == 2
    assert obs["host.smart.health_all_passed"] is False
    assert obs["host.smart.unhealthy_devices"] == ["/dev/sde"]
    assert obs["host.smart.devices"][0]["wwn"] == "0x5000123000000456"


async def test_probe_skips_cleanly_when_smartctl_absent() -> None:
    # Empty scan output (smartctl not installed) -> success, no observations.
    adapter = _FakeAdapter(_FakeSSH(scan="", devices={}))
    result = await HostSmartProbe().run(_ctx(adapter))
    assert result.success
    assert result.observations == []
