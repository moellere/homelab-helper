"""K8sAdapter tests — pure parsers + injected-runner command path. No live cluster."""

from __future__ import annotations

import json

import pytest

from homelab_helper.adapters.kubernetes import (
    K8sAdapter,
    K8sConfig,
    KubeError,
    parse_node,
    parse_quantity,
)

_NODE = {
    "metadata": {
        "name": "worker-a",
        "labels": {"node-role.kubernetes.io/worker": "", "kubernetes.io/arch": "amd64"},
    },
    "status": {
        "nodeInfo": {
            "architecture": "amd64",
            "kernelVersion": "6.18.18-talos",
            "osImage": "Talos (v1.12.6)",
            "machineID": "188ff35f95214e72",
            "kubeletVersion": "v1.35.2",
            "containerRuntimeVersion": "containerd://2.1.6",
        },
        "capacity": {"cpu": "4", "memory": "16241620Ki", "pods": "110"},
        "addresses": [
            {"type": "InternalIP", "address": "192.0.2.24"},
            {"type": "Hostname", "address": "worker-a"},
        ],
    },
}


def test_parse_quantity_suffixes() -> None:
    assert parse_quantity("16241620Ki") == 16241620 * 1024
    assert parse_quantity("2Gi") == 2 * 1024**3
    assert parse_quantity("4") == 4
    assert parse_quantity(None) is None
    assert parse_quantity("garbage") is None


def test_parse_node_maps_fields() -> None:
    n = parse_node(_NODE)
    assert n["name"] == "worker-a"
    assert n["arch"] == "amd64"
    assert n["kernel"] == "6.18.18-talos"
    assert n["kubelet_version"] == "v1.35.2"
    assert n["container_runtime"] == "containerd://2.1.6"
    assert n["cpu_capacity"] == 4
    assert n["memory_bytes"] == 16241620 * 1024
    assert n["internal_ip"] == "192.0.2.24"
    assert n["roles"] == ["worker"]


async def test_list_nodes_via_injected_runner() -> None:
    seen: list[list[str]] = []

    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        seen.append(list(args))
        return 0, json.dumps({"items": [_NODE]}), ""

    adapter = K8sAdapter(K8sConfig(kubeconfig="/tmp/kc", context="lab"), runner=runner)
    nodes = await adapter.list_nodes()
    assert nodes[0]["name"] == "worker-a"
    assert seen == [["--kubeconfig", "/tmp/kc", "--context", "lab", "get", "nodes", "-o", "json"]]


async def test_nonzero_exit_raises() -> None:
    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        return 1, "", "Unable to connect to the server"

    with pytest.raises(KubeError, match="connect"):
        await K8sAdapter(runner=runner).list_nodes()


async def test_health_check_ok_and_failure() -> None:
    async def ok(args, timeout_s):  # noqa: ANN001, ARG001
        return 0, json.dumps({"serverVersion": {"gitVersion": "v1.35.2"}}), ""

    async def bad(args, timeout_s):  # noqa: ANN001, ARG001
        return 1, "", "forbidden"

    assert await K8sAdapter(runner=ok).health_check() == (True, None)
    good, err = await K8sAdapter(runner=bad).health_check()
    assert good is False
    assert err is not None
