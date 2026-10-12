"""Kubernetes adapter — read-only management-plane source via ``kubectl``.

Second management-plane source (after Proxmox). Like the Talos adapter it shells
out to a CLI the operator already has configured (``kubectl``) rather than
vendoring a client + the apiserver auth dance — kubeconfig handles auth, and the
dependency surface stays flat. Read-only at L1, with the Phase-7 exception
below: three workload writes (rollout restart, scale, rollout undo) that only
``engine/executor.py`` and ``engine/rollback.py`` may call.

The high-value read is **nodes**: a K8s node carries the same host facts the
kernel probes report (kernel, arch, cpu, memory) plus K8s-only ones (kubelet /
runtime versions, roles). Recorded at ``INFERRED`` confidence, these fill the
gaps the kernel never reported and — by the reconciler's precedence — never
override a kernel-``VERIFIED`` fact. That's multi-source coordination on the
same hosts.

Configuration (optional — falls back to kubectl's own defaults)::

    HOMELAB_HELPER_KUBECONFIG     path to a kubeconfig
    HOMELAB_HELPER_KUBE_CONTEXT   context name

Tests inject a ``runner`` so no live cluster / kubectl is needed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

# K8s quantity binary/decimal suffixes → multiplier.
_QUANTITY_SUFFIX = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


class KubeError(RuntimeError):
    """A ``kubectl`` invocation failed (non-zero exit or missing binary)."""


class KubeConfigError(RuntimeError):
    """Required Kubernetes configuration is missing/invalid."""


class KubeCommandRunner(Protocol):
    """Executes a ``kubectl`` argument vector; returns ``(exit_code, stdout, stderr)``."""

    async def __call__(self, args: Sequence[str], timeout: int) -> tuple[int, str, str]: ...


@dataclass(frozen=True)
class K8sConfig:
    kubeconfig: str | None = None
    context: str | None = None
    timeout_s: int = 20

    @classmethod
    def from_env(cls) -> K8sConfig:
        return cls(
            kubeconfig=os.environ.get("HOMELAB_HELPER_KUBECONFIG") or None,
            context=os.environ.get("HOMELAB_HELPER_KUBE_CONTEXT") or None,
        )


def parse_quantity(value: Any) -> int | None:
    """Parse a K8s resource quantity (``"16241620Ki"``, ``"4"``, ``"2Gi"``) to bytes/units."""
    if value is None:
        return None
    s = str(value).strip()
    for suffix, mult in _QUANTITY_SUFFIX.items():
        if s.endswith(suffix):
            num = s[: -len(suffix)]
            return int(float(num) * mult) if _is_number(num) else None
    return int(float(s)) if _is_number(s) else None


_CPU_SUFFIX = {"n": 1e-9, "u": 1e-6, "m": 1e-3}


def parse_cpu_cores(value: Any) -> float | None:
    """Parse a CPU quantity (``"250m"``, ``"3456789n"``, ``"2"``) to cores."""
    if value is None:
        return None
    s = str(value).strip()
    for suffix, mult in _CPU_SUFFIX.items():
        if s.endswith(suffix) and _is_number(s[:-1]):
            return float(s[:-1]) * mult
    return float(s) if _is_number(s) else None


def _is_number(s: str) -> bool:
    try:
        float(s)
    except ValueError:
        return False
    return True


def parse_node(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape a Kubernetes Node object into a flat dict of the facts we care about."""
    meta = raw.get("metadata") or {}
    status = raw.get("status") or {}
    info = status.get("nodeInfo") or {}
    capacity = status.get("capacity") or {}
    addresses = status.get("addresses") or []

    internal_ip = next((a.get("address") for a in addresses if a.get("type") == "InternalIP"), None)
    hostname = next(
        (a.get("address") for a in addresses if a.get("type") == "Hostname"), meta.get("name")
    )
    roles = sorted(
        label.split("/", 1)[1]
        for label in (meta.get("labels") or {})
        if label.startswith("node-role.kubernetes.io/") and "/" in label
    )
    return {
        "name": meta.get("name"),
        "hostname": hostname,
        "internal_ip": internal_ip,
        "arch": info.get("architecture"),
        "kernel": info.get("kernelVersion"),
        "os_image": info.get("osImage"),
        "machine_id": info.get("machineID"),
        "kubelet_version": info.get("kubeletVersion"),
        "container_runtime": info.get("containerRuntimeVersion"),
        "cpu_capacity": int(capacity["cpu"]) if str(capacity.get("cpu", "")).isdigit() else None,
        "memory_bytes": parse_quantity(capacity.get("memory")),
        "roles": roles,
    }


class K8sAdapter:
    """Read-only async client that drives ``kubectl``."""

    name = "kubernetes"

    def __init__(
        self,
        config: K8sConfig | None = None,
        *,
        kubectl_path: str = "kubectl",
        runner: KubeCommandRunner | None = None,
    ) -> None:
        self.config = config or K8sConfig()
        self._bin = kubectl_path
        self._runner = runner or self._subprocess_runner

    @classmethod
    def from_env(cls) -> K8sAdapter:
        return cls(K8sConfig.from_env())

    async def _subprocess_runner(self, args: Sequence[str], timeout: int) -> tuple[int, str, str]:
        if shutil.which(self._bin) is None:
            raise KubeError(f"{self._bin!r} not found on PATH")
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(timeout):
                out, err = await proc.communicate()
        except TimeoutError as exc:
            raise KubeError(f"kubectl timed out after {timeout}s: {' '.join(args)}") from exc
        except OSError as exc:
            raise KubeError(f"could not launch {self._bin!r}: {exc}") from exc
        code = proc.returncode if proc.returncode is not None else -1
        return code, out.decode(errors="replace"), err.decode(errors="replace")

    def _base_args(self) -> list[str]:
        args: list[str] = []
        if self.config.kubeconfig:
            args += ["--kubeconfig", self.config.kubeconfig]
        if self.config.context:
            args += ["--context", self.config.context]
        return args

    async def _run(self, *argv: str) -> str:
        args = [*self._base_args(), *argv]
        code, stdout, stderr = await self._runner(args, self.config.timeout_s)
        if code != 0:
            raise KubeError(f"kubectl {' '.join(argv)} exited {code}: {stderr.strip()[:300]}")
        return stdout

    async def get_resource(self, kind: str) -> list[dict[str, Any]]:
        """Return ``.items`` from ``kubectl get <kind> -o json``."""
        out = await self._run("get", kind, "-o", "json")
        payload = json.loads(out) if out.strip() else {}
        items = payload.get("items") if isinstance(payload, dict) else None
        return items if isinstance(items, list) else []

    async def list_nodes(self) -> list[dict[str, Any]]:
        return [parse_node(n) for n in await self.get_resource("nodes")]

    async def list_pods(self) -> list[dict[str, Any]]:
        """Every pod in the cluster, raw."""
        out = await self._run("get", "pods", "-A", "-o", "json")
        payload = json.loads(out) if out.strip() else {}
        items = payload.get("items") if isinstance(payload, dict) else None
        return items if isinstance(items, list) else []

    async def pod_metrics(self) -> list[dict[str, Any]]:
        """metrics-server's current pod usage (``PodMetrics`` items); raw."""
        out = await self._run("get", "--raw", "/apis/metrics.k8s.io/v1beta1/pods")
        payload = json.loads(out) if out.strip() else {}
        items = payload.get("items") if isinstance(payload, dict) else None
        return items if isinstance(items, list) else []

    async def server_version(self) -> str | None:
        out = await self._run("version", "-o", "json")
        payload = json.loads(out) if out.strip() else {}
        return (payload.get("serverVersion") or {}).get("gitVersion")

    async def health_check(self) -> tuple[bool, str | None]:
        try:
            await self.server_version()
        except (KubeError, json.JSONDecodeError) as exc:
            return False, str(exc)
        return True, None

    # --------------------------------------------------------------- workloads

    async def get_workload(self, namespace: str, kind: str, name: str) -> dict[str, Any]:
        """One Deployment / StatefulSet / DaemonSet as ``{replicas, ready, generation, ...}``.

        Read-only — the rollback orchestrator's probe for ``prior-replicas``.
        """
        _check_workload_kind(kind)
        out = await self._run("get", f"{kind}/{name}", "-n", namespace, "-o", "json")
        payload = json.loads(out) if out.strip() else {}
        spec = payload.get("spec") or {}
        status = payload.get("status") or {}
        return {
            "namespace": namespace,
            "kind": kind,
            "name": name,
            "replicas": spec.get("replicas"),
            "ready_replicas": status.get("readyReplicas"),
            "generation": (payload.get("metadata") or {}).get("generation"),
            "observed_generation": status.get("observedGeneration"),
        }

    async def list_workloads(self) -> list[dict[str, Any]]:
        """Every Deployment / StatefulSet / DaemonSet in the cluster, shaped by :func:`parse_workload`."""
        out = await self._run("get", "deployments,statefulsets,daemonsets", "-A", "-o", "json")
        payload = json.loads(out) if out.strip() else {}
        items = payload.get("items") if isinstance(payload, dict) else None
        return [parse_workload(i) for i in (items or []) if isinstance(i, dict)]

    async def rollout_history(self, namespace: str, kind: str, name: str) -> list[int]:
        """Revision numbers ``kubectl rollout history`` knows, oldest first. Read-only."""
        _check_workload_kind(kind)
        out = await self._run("rollout", "history", f"{kind}/{name}", "-n", namespace)
        revisions: list[int] = []
        for line in out.splitlines():
            head = line.strip().split(None, 1)[0] if line.strip() else ""
            if head.isdigit():
                revisions.append(int(head))
        return sorted(revisions)

    # ----------------------------------------------------------------- writes
    #
    # The Phase-7 write surface, and it exists solely for the executor: every
    # call site must have routed through engine.trust.decide() first. Nothing
    # else in the codebase may call these (tests/test_write_isolation.py).

    async def rollout_restart(self, namespace: str, kind: str, name: str) -> str:
        """``kubectl rollout restart`` one workload. Executor-only."""
        _check_workload_kind(kind)
        return (await self._run("rollout", "restart", f"{kind}/{name}", "-n", namespace)).strip()

    async def scale_workload(self, namespace: str, kind: str, name: str, replicas: int) -> str:
        """``kubectl scale`` one workload to ``replicas``. Executor-only."""
        _check_workload_kind(kind)
        if kind == "daemonset":
            raise KubeError("a DaemonSet has no replica count to scale")
        if replicas < 0:
            raise ValueError("replicas must be >= 0")
        return (
            await self._run("scale", f"{kind}/{name}", "-n", namespace, f"--replicas={replicas}")
        ).strip()

    async def rollout_undo(
        self, namespace: str, kind: str, name: str, *, to_revision: int | None = None
    ) -> str:
        """``kubectl rollout undo`` one workload. Rollback use only (engine/rollback.py)."""
        _check_workload_kind(kind)
        argv = ["rollout", "undo", f"{kind}/{name}", "-n", namespace]
        if to_revision is not None:
            argv.append(f"--to-revision={to_revision}")
        return (await self._run(*argv)).strip()


WORKLOAD_KINDS: tuple[str, ...] = ("deployment", "statefulset", "daemonset")


def parse_workload(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one workload into ``{namespace, kind, name, desired, ready, settled, ...}``.

    ``desired``/``ready`` come from ``spec.replicas``/``status.readyReplicas`` for a
    Deployment or StatefulSet and from ``desiredNumberScheduled``/``numberReady``
    for a DaemonSet. ``settled`` is true once the controller has observed the
    current generation and every replica is on the current template, so a
    rollout in progress is not mistaken for ill health.
    """
    meta = raw.get("metadata") or {}
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    kind = str(raw.get("kind") or "").lower()
    if kind == "daemonset":
        desired = status.get("desiredNumberScheduled")
        ready = status.get("numberReady")
        updated = status.get("updatedNumberScheduled")
    else:
        desired = spec.get("replicas")
        ready = status.get("readyReplicas")
        updated = status.get("updatedReplicas")
    desired = int(desired or 0)
    ready = int(ready or 0)
    generation = meta.get("generation")
    observed = status.get("observedGeneration")
    settled = (generation is None or observed is None or int(observed) >= int(generation)) and (
        updated is None or int(updated or 0) >= desired
    )
    conditions = {
        str(c.get("type")): str(c.get("status"))
        for c in (status.get("conditions") or [])
        if isinstance(c, dict)
    }
    return {
        "namespace": meta.get("namespace"),
        "kind": kind,
        "name": meta.get("name"),
        "desired": desired,
        "ready": ready,
        "settled": settled,
        "available": conditions.get("Available"),
        "progressing": conditions.get("Progressing"),
    }


_OWNER_KINDS = {"StatefulSet": "statefulset", "DaemonSet": "daemonset", "ReplicaSet": "deployment"}


def pod_workload(pod: dict[str, Any]) -> tuple[str, str, str] | None:
    """``(namespace, kind, name)`` of the workload a pod belongs to, or None for a
    bare pod, a Job's pod or a static pod. A ReplicaSet's pod is attributed to
    its Deployment by stripping the ``pod-template-hash`` the set is named with."""
    meta = pod.get("metadata") or {}
    for owner in meta.get("ownerReferences") or []:
        kind = _OWNER_KINDS.get(str(owner.get("kind")))
        if kind is None:
            continue
        name = str(owner.get("name") or "")
        if kind == "deployment":
            suffix = "-" + str((meta.get("labels") or {}).get("pod-template-hash") or "")
            name = name.removesuffix(suffix) if len(suffix) > 1 and name.endswith(suffix) else name
        if name:
            return str(meta.get("namespace") or "default"), kind, name
    return None


def workload_usage(
    pods: list[dict[str, Any]], metrics: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Sum metrics-server usage and declared resources per workload.

    Per workload: ``cpu_cores`` used, ``mem_bytes`` used, ``cpu_limit`` /
    ``cpu_request`` and ``mem_limit`` summed over the pods' containers (None
    when any container leaves it unset), and ``pods`` counted. Pods without a
    metrics entry (just scheduled, or a node whose kubelet is not answering)
    contribute their requests but no usage.
    """
    usage: dict[tuple[str, str], tuple[float, int]] = {}
    for item in metrics:
        meta = item.get("metadata") or {}
        cpu = mem = 0.0
        for c in item.get("containers") or []:
            u = c.get("usage") or {}
            cpu += parse_cpu_cores(u.get("cpu")) or 0.0
            mem += parse_quantity(u.get("memory")) or 0
        usage[(str(meta.get("namespace")), str(meta.get("name")))] = (cpu, int(mem))
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for pod in pods:
        owner = pod_workload(pod)
        if owner is None or (pod.get("status") or {}).get("phase") not in (None, "Running"):
            continue
        meta = pod.get("metadata") or {}
        row = out.setdefault(
            owner,
            {
                "namespace": owner[0],
                "kind": owner[1],
                "name": owner[2],
                "pods": 0,
                "cpu_cores": 0.0,
                "mem_bytes": 0,
                "cpu_limit": 0.0,
                "cpu_request": 0.0,
                "mem_limit": 0,
                "_unset": set(),
            },
        )
        row["pods"] += 1
        cpu, mem = usage.get((str(meta.get("namespace")), str(meta.get("name"))), (0.0, 0))
        row["cpu_cores"] += cpu
        row["mem_bytes"] += mem
        for c in (pod.get("spec") or {}).get("containers") or []:
            res = c.get("resources") or {}
            for field, source, parse in (
                ("cpu_limit", (res.get("limits") or {}).get("cpu"), parse_cpu_cores),
                ("cpu_request", (res.get("requests") or {}).get("cpu"), parse_cpu_cores),
                ("mem_limit", (res.get("limits") or {}).get("memory"), parse_quantity),
            ):
                value = parse(source)
                if value is None:
                    row["_unset"].add(field)
                else:
                    row[field] += value
    rows = []
    for row in out.values():
        for field in row.pop("_unset"):
            row[field] = None
        rows.append(row)
    return sorted(rows, key=lambda r: (r["namespace"], r["kind"], r["name"]))


def workload_is_unhealthy(w: dict[str, Any]) -> bool:
    """Fewer ready than desired with the rollout settled — the restart playbook's trigger."""
    return w["desired"] > 0 and w["ready"] < w["desired"] and bool(w["settled"])


def _check_workload_kind(kind: str) -> None:
    if kind not in WORKLOAD_KINDS:
        raise ValueError(f"kind must be one of {', '.join(WORKLOAD_KINDS)}, not {kind!r}")


__all__ = [
    "WORKLOAD_KINDS",
    "K8sAdapter",
    "K8sConfig",
    "KubeCommandRunner",
    "KubeConfigError",
    "KubeError",
    "parse_node",
    "parse_quantity",
    "parse_workload",
    "workload_is_unhealthy",
]
