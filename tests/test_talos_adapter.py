"""Tests for the Talos adapter — pure parsers + injected-runner command path.

No real ``talosctl`` or cluster: the adapter takes a ``runner`` callable, so we
feed canned ``(exit_code, stdout, stderr)`` triples and assert on the parsed
result and the exact argument vector built.
"""

from __future__ import annotations

import pytest

from homelab_helper.adapters.talos import (
    TalosAdapter,
    TalosError,
    parse_resource_stream,
    parse_version_server,
)

# Two concatenated, pretty-printed JSON docs exactly as `talosctl get -o json` emits.
_TWO_DISKS = """{
    "metadata": {"id": "loop0"},
    "node": "10.0.0.1",
    "spec": {"size": 67944448, "readonly": true, "bus_path": "/virtual"}
}
{
    "metadata": {"id": "sda"},
    "node": "10.0.0.1",
    "spec": {"size": 256060514304, "transport": "usb", "wwid": "naa.5000000000000099"}
}"""

_VERSION = """Client:
\tTag:         v1.12.6
\tOS/Arch:     linux/amd64
Server:
\tNODE:        10.0.0.1
\tTag:         v1.12.6
\tOS/Arch:     linux/arm64
"""


def test_parse_resource_stream_splits_concatenated_docs() -> None:
    docs = parse_resource_stream(_TWO_DISKS)
    assert [d["metadata"]["id"] for d in docs] == ["loop0", "sda"]
    assert docs[1]["spec"]["wwid"] == "naa.5000000000000099"


def test_parse_resource_stream_tolerates_empty_and_garbage() -> None:
    assert parse_resource_stream("") == []
    assert parse_resource_stream("not json at all") == []


def test_parse_version_server_reads_server_not_client() -> None:
    # The client is amd64 (the workstation); the node is arm64. Must not shadow.
    ver = parse_version_server(_VERSION)
    assert ver["arch"] == "arm64"
    assert ver["os_arch"] == "linux/arm64"
    assert ver["tag"] == "v1.12.6"


async def test_get_resources_builds_argv_and_parses() -> None:
    seen: list[list[str]] = []

    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        seen.append(list(args))
        return 0, _TWO_DISKS, ""

    adapter = TalosAdapter(talosconfig="/tmp/talosconfig", runner=runner)
    docs = await adapter.get_resources("10.0.0.1", "disks")

    assert [d["metadata"]["id"] for d in docs] == ["loop0", "sda"]
    assert seen == [
        ["--talosconfig", "/tmp/talosconfig", "-n", "10.0.0.1", "get", "disks", "-o", "json"]
    ]


async def test_nonzero_exit_raises_talos_error() -> None:
    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        return 1, "", "rpc error: node unreachable"

    adapter = TalosAdapter(runner=runner)
    with pytest.raises(TalosError, match="unreachable"):
        await adapter.get_resources("10.0.0.1", "disks")


async def test_health_check_ok_and_failure() -> None:
    async def good(args, timeout_s):  # noqa: ANN001, ARG001
        return 0, _VERSION, ""

    async def bad(args, timeout_s):  # noqa: ANN001, ARG001
        return 1, "", "certificate expired"

    assert await TalosAdapter(runner=good).health_check("n") == (True, None)
    ok, err = await TalosAdapter(runner=bad).health_check("n")
    assert ok is False
    assert err is not None
    assert "certificate expired" in err
