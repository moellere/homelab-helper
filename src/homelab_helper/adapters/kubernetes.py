"""Kubernetes adapter — read-only management-plane source via ``kubectl``.

Second management-plane source (after Proxmox). Like the Talos adapter it shells
out to a CLI the operator already has configured (``kubectl``) rather than
vendoring a client + the apiserver auth dance — kubeconfig handles auth, and the
dependency surface stays flat. Read-only at L1.

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


__all__ = [
    "K8sAdapter",
    "K8sConfig",
    "KubeCommandRunner",
    "KubeConfigError",
    "KubeError",
    "parse_node",
    "parse_quantity",
]
