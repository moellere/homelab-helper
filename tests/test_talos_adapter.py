"""Tests for the Talos adapter — pure parsers + injected-runner command path.

No real ``talosctl`` or cluster: the adapter takes a ``runner`` callable, so we
feed canned ``(exit_code, stdout, stderr)`` triples and assert on the parsed
result and the exact argument vector built.
"""

from __future__ import annotations

import pytest

from homelab_helper.adapters import talos as talos_mod
from homelab_helper.adapters.talos import (
    TalosAdapter,
    TalosError,
    image_with_tag,
    install_image_from_config,
    parse_resource_stream,
    parse_version_server,
    wait_for_tag,
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


# --- Phase 11.1: the writes and the return wait ---------------------------------

_MACHINECONFIG = """{
    "metadata": {"id": "v1alpha1", "namespace": "config"},
    "node": "10.0.0.1",
    "spec": "version: v1alpha1\\nmachine:\\n    type: controlplane\\n    token: secret\\n    install:\\n        disk: /dev/sda\\n        image: factory.talos.dev/installer/abc123:v1.14.1\\n"
}"""

_MACHINESTATUS = """{
    "metadata": {"id": "machine", "namespace": "runtime"},
    "node": "10.0.0.1",
    "spec": {"stage": "running", "status": {"ready": true, "unmetConditions": []}}
}"""


def test_install_image_is_the_only_field_read_from_the_machine_config() -> None:
    assert (
        install_image_from_config(
            "machine:\n  token: s3cret\n  install:\n    image: ghcr.io/siderolabs/installer:v1.13.0\n"
        )
        == "ghcr.io/siderolabs/installer:v1.13.0"
    )
    with pytest.raises(TalosError, match=r"no machine\.install\.image"):
        install_image_from_config("machine:\n  type: worker\n")
    with pytest.raises(TalosError, match="not YAML"):
        install_image_from_config("machine: [unclosed")


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("factory.talos.dev/installer/abc:v1.14.1", "factory.talos.dev/installer/abc:v1.14.2"),
        ("ghcr.io/siderolabs/installer:v1.13.0", "ghcr.io/siderolabs/installer:v1.14.2"),
        ("registry.lan:5000/installer", "registry.lan:5000/installer:v1.14.2"),
        ("ghcr.io/siderolabs/installer", "ghcr.io/siderolabs/installer:v1.14.2"),
    ],
)
def test_image_with_tag_swaps_only_the_tag(image: str, expected: str) -> None:
    assert image_with_tag(image, "v1.14.2") == expected


async def test_install_image_and_machine_ready_read_their_resources() -> None:
    seen: list[list[str]] = []

    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        seen.append(list(args))
        if "machineconfig" in args:
            return 0, _MACHINECONFIG, ""
        return 0, _MACHINESTATUS, ""

    adapter = TalosAdapter(runner=runner)
    assert await adapter.install_image("10.0.0.1") == "factory.talos.dev/installer/abc123:v1.14.1"
    assert await adapter.machine_ready("10.0.0.1") == (True, "running")
    assert seen[0] == ["-n", "10.0.0.1", "get", "machineconfig", "-o", "json"]
    assert seen[1] == ["-n", "10.0.0.1", "get", "machinestatus", "-o", "json"]


async def test_upgrade_and_rollback_build_the_exact_argv() -> None:
    seen: list[list[str]] = []

    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        seen.append(list(args))
        return 0, "", ""

    adapter = TalosAdapter(talosconfig="/t/cfg", runner=runner)
    await adapter.upgrade_node("10.0.0.1", "factory.talos.dev/installer/abc:v1.14.2")
    await adapter.rollback_node("10.0.0.1")
    assert seen == [
        [
            "--talosconfig",
            "/t/cfg",
            "-n",
            "10.0.0.1",
            "upgrade",
            "--image",
            "factory.talos.dev/installer/abc:v1.14.2",
            "--wait=false",
        ],
        ["--talosconfig", "/t/cfg", "-n", "10.0.0.1", "rollback"],
    ]


def _version_text(tag: str) -> str:
    return f"Client:\n\tTag: v1.14.2\nServer:\n\tNODE: 10.0.0.1\n\tTag: {tag}\n\tOS/Arch: linux/amd64\n"


@pytest.fixture
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [0.0]

    async def _sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(talos_mod.asyncio, "sleep", _sleep)
    monkeypatch.setattr(talos_mod.time, "monotonic", lambda: clock[0])
    return clock


async def test_wait_for_tag_waits_for_the_node_to_leave_then_return_ready(fast_clock) -> None:
    # Old tag twice (still up), then down twice, then the new tag.
    script: list[str | None] = ["v1.14.1", "v1.14.1", None, None, "v1.14.2"]

    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        if "version" in args:
            tag = script.pop(0) if len(script) > 1 else script[0]
            return (1, "", "connection refused") if tag is None else (0, _version_text(tag), "")
        return 0, _MACHINESTATUS, ""

    took = await wait_for_tag(TalosAdapter(runner=runner), "10.0.0.1", "v1.14.2")
    assert took >= talos_mod.RETURN_POLL_S * 3
    assert script == ["v1.14.2"]


async def test_wait_for_tag_gives_up_after_the_timeout(fast_clock) -> None:
    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        return 1, "", "connection refused"

    with pytest.raises(TalosError, match="did not come back"):
        await wait_for_tag(TalosAdapter(runner=runner), "10.0.0.1", "v1.14.2")
    assert fast_clock[0] >= talos_mod.RETURN_TIMEOUT_S


async def test_wait_for_tag_is_not_fooled_by_a_node_that_never_left(fast_clock) -> None:
    """A node still on the old tag past the going-down window is not 'back'."""

    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        if "version" in args:
            return 0, _version_text("v1.14.1"), ""
        return 0, _MACHINESTATUS, ""

    with pytest.raises(TalosError, match="did not come back"):
        await wait_for_tag(TalosAdapter(runner=runner), "10.0.0.1", "v1.14.2", timeout_s=300)


# --- Phase 11.2: the Kubernetes-side reads and the upgrade-k8s write -------------

_KUBELETCONFIG = """{
    "metadata": {"id": "kubelet", "namespace": "k8s"},
    "node": "10.0.0.1",
    "spec": {"image": "ghcr.io/siderolabs/kubelet:v1.37.1", "clusterDNS": ["10.96.0.10"]}
}"""

_MEMBERS = """{
    "metadata": {"id": "talos-cp1", "namespace": "cluster"},
    "node": "10.0.0.1",
    "spec": {"nodeId": "a", "addresses": ["10.0.6.13", "10.0.6.16"], "hostname": "talos-cp1",
             "machineType": "controlplane", "operatingSystem": "Talos (v1.14.2)"}
}
{
    "metadata": {"id": "bmax4", "namespace": "cluster"},
    "node": "10.0.0.1",
    "spec": {"nodeId": "b", "addresses": ["10.0.6.24"], "hostname": "bmax4",
             "machineType": "worker", "operatingSystem": "Talos (v1.13.5)"}
}"""


async def test_kubernetes_version_is_the_kubelet_image_tag() -> None:
    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        return 0, _KUBELETCONFIG, ""

    assert await TalosAdapter(runner=runner).kubernetes_version("10.0.0.1") == "1.37.1"

    async def untagged(args, timeout_s):  # noqa: ANN001, ARG001
        return 0, _KUBELETCONFIG.replace(":v1.37.1", ""), ""

    with pytest.raises(TalosError, match="no kubelet image tag"):
        await TalosAdapter(runner=untagged).kubernetes_version("10.0.0.1")


async def test_members_carry_type_address_and_talos_release() -> None:
    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        return 0, _MEMBERS, ""

    members = await TalosAdapter(runner=runner).members("10.0.0.1")
    assert members == [
        {
            "hostname": "talos-cp1",
            "machine_type": "controlplane",
            "address": "10.0.6.13",
            "talos": "v1.14.2",
        },
        {"hostname": "bmax4", "machine_type": "worker", "address": "10.0.6.24", "talos": "v1.13.5"},
    ]


async def test_cluster_health_reports_the_exit_code_and_the_tail() -> None:
    seen: list[list[str]] = []

    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        seen.append(list(args))
        return (
            1,
            "",
            "waiting for etcd to be healthy: OK\nwaiting for all nodes to be ready: timeout",
        )

    ok, tail = await TalosAdapter(runner=runner).cluster_health("10.0.0.1", timeout=60)
    assert ok is False
    assert tail.endswith("ready: timeout")
    assert seen == [["-n", "10.0.0.1", "health", "--wait-timeout", "50s"]]


async def test_upgrade_k8s_builds_the_exact_argv() -> None:
    seen: list[tuple[list[str], int]] = []

    async def runner(args, timeout_s):  # noqa: ANN001
        seen.append((list(args), timeout_s))
        return 0, "", ""

    await TalosAdapter(runner=runner).upgrade_k8s("10.0.0.1", "1.38.0")
    assert seen == [
        (["-n", "10.0.0.1", "upgrade-k8s", "--to", "1.38.0"], talos_mod.K8S_UPGRADE_TIMEOUT_S)
    ]
