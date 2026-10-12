"""Talos adapter — read-only wrapper around the ``talosctl`` machine API.

Talos Linux has no SSH and no shell; every fact about a node comes from its
gRPC machine API. Rather than vendor a gRPC client + protobufs, this adapter
shells out to the ``talosctl`` binary the operator already has configured
(``~/.talos/config`` by default) and parses its JSON / text output. That keeps
the dependency surface identical to the network probes (no new wheels) and
reuses the operator's existing mTLS credentials.

Three call shapes are exposed:

- :meth:`get_resources` — ``talosctl -n <node> get <resource> -o json``; the
  COSI resource model (``disks``, ``links``, ``systeminformation``, …). Output
  is a stream of concatenated JSON documents, parsed by
  :func:`parse_resource_stream`.
- :meth:`read_file` — ``talosctl -n <node> read <path>``; reads a host file
  (``/proc/cpuinfo``, ``/proc/meminfo``) the resource model doesn't surface.
- :meth:`version` — ``talosctl -n <node> version``; the one fact that isn't a
  resource. Parsed for the *server* arch + tag (the client line is dropped).

Phase 11.1 adds the two writes the executor owns — :meth:`upgrade_node`
(``talosctl upgrade --image``) and :meth:`rollback_node` (``talosctl
rollback``, Talos's own inverse: boot the previous install again) — plus the
reads they lean on: :meth:`install_image` (the node's configured installer
image, the one field taken from its machine config; the rest, which holds the
cluster's secrets, is discarded unread), :meth:`machine_ready` and
:func:`wait_for_tag`, the return wait both writes share.

The binary invocation is injectable via ``runner`` so probe/adapter tests run
without a real ``talosctl`` or a live cluster.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from typing import TYPE_CHECKING, Any, Protocol

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence

RETURN_TIMEOUT_S = 900
"""How long an upgraded or rolled-back node may take to boot the other install and report ready."""
RETURN_POLL_S = 10
GOING_DOWN_S = 120
K8S_UPGRADE_TIMEOUT_S = 2400
"""``upgrade-k8s`` pre-pulls images and walks every control plane and kubelet; 40 minutes covers a small cluster."""
"""How long to wait for the node to actually leave before its old version is mistaken for its return."""


class TalosError(RuntimeError):
    """Raised when a ``talosctl`` invocation fails (non-zero exit or no binary)."""


class TalosCommandRunner(Protocol):
    """Executes a ``talosctl`` argument vector; returns ``(exit_code, stdout, stderr)``."""

    async def __call__(self, args: Sequence[str], timeout: int) -> tuple[int, str, str]: ...


def parse_resource_stream(stdout: str) -> list[dict[str, Any]]:
    """Parse ``talosctl get -o json`` output into a list of resource documents.

    ``talosctl`` emits one pretty-printed JSON object per resource with no
    array wrapper or separator, so a plain ``json.loads`` over the whole blob
    fails. Decode them one at a time with ``raw_decode``, skipping whitespace
    between documents. Malformed tails are tolerated (best-effort).
    """
    docs: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    text = stdout.strip()
    idx = 0
    length = len(text)
    while idx < length:
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            docs.append(obj)
        idx = end
        while idx < length and text[idx] in " \t\r\n":
            idx += 1
    return docs


def install_image_from_config(spec: str) -> str:
    """``machine.install.image`` out of a machine config document; nothing else is read."""
    try:
        doc = yaml.safe_load(spec)
    except yaml.YAMLError as exc:
        raise TalosError(f"machine config is not YAML: {exc}") from exc
    image = (((doc or {}).get("machine") or {}).get("install") or {}).get("image")
    if not image or not isinstance(image, str):
        raise TalosError("machine config names no machine.install.image")
    return image


def image_with_tag(image: str, tag: str) -> str:
    """The same installer image at another tag: the registry and schematic are the node's own."""
    base, sep, last = image.rpartition(":")
    if not sep or "/" in last:
        return f"{image}:{tag}"
    return f"{base}:{tag}"


def parse_version_server(stdout: str) -> dict[str, str]:
    """Extract the *server* arch + tag from ``talosctl version`` text output.

    The command prints a ``Client:`` block then a ``Server:`` block; the client
    arch is this workstation's, not the node's. We only read lines after
    ``Server:`` so a node's arch never gets shadowed by the client's.
    """
    out: dict[str, str] = {}
    in_server = False
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("Server:"):
            in_server = True
            continue
        if line.startswith("Client:"):
            in_server = False
            continue
        if not in_server:
            continue
        if line.startswith("Tag:"):
            out.setdefault("tag", line.split(":", 1)[1].strip())
        elif line.startswith("OS/Arch:"):
            os_arch = line.split(":", 1)[1].strip()
            out.setdefault("os_arch", os_arch)
            # "linux/arm64" -> "arm64"; normalize_arch maps it onward.
            out.setdefault("arch", os_arch.rsplit("/", 1)[-1])
    return out


class TalosAdapter:
    """Adapter that drives ``talosctl`` against the machine API.

    Read-only except for :meth:`upgrade_node` and :meth:`rollback_node`, which
    only the executor and the rollback orchestrator call.
    """

    name = "talos"

    def __init__(
        self,
        *,
        talosctl_path: str = "talosctl",
        talosconfig: str | None = None,
        default_timeout: int = 30,
        runner: TalosCommandRunner | None = None,
    ) -> None:
        self._bin = talosctl_path
        self._talosconfig = talosconfig
        self._default_timeout = default_timeout
        self._runner = runner or self._subprocess_runner

    async def _subprocess_runner(self, args: Sequence[str], timeout: int) -> tuple[int, str, str]:
        """Default runner: spawn ``talosctl`` and capture its output."""
        if shutil.which(self._bin) is None:
            raise TalosError(f"{self._bin!r} not found on PATH")
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise TalosError(f"could not launch {self._bin!r}: {exc}") from exc
        try:
            async with asyncio.timeout(timeout):
                stdout_b, stderr_b = await proc.communicate()
        except TimeoutError as exc:
            proc.kill()
            raise TalosError(f"talosctl timed out after {timeout}s: {' '.join(args)}") from exc
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout_b.decode(errors="replace"),
            stderr_b.decode(errors="replace"),
        )

    def _base_args(self, node: str) -> list[str]:
        args: list[str] = []
        if self._talosconfig:
            args += ["--talosconfig", self._talosconfig]
        args += ["-n", node]
        return args

    async def _run(self, node: str, *argv: str, timeout: int | None = None) -> str:
        """Run ``talosctl <base> <argv>``; return stdout, raise on failure."""
        t = timeout if timeout is not None else self._default_timeout
        args = [*self._base_args(node), *argv]
        code, stdout, stderr = await self._runner(args, t)
        if code != 0:
            raise TalosError(
                f"talosctl {' '.join(argv)} on {node} exited {code}: {stderr.strip()[:300]}"
            )
        return stdout

    async def get_resources(
        self, node: str, resource: str, *, timeout: int | None = None
    ) -> list[dict[str, Any]]:
        """Return the resource documents for ``talosctl get <resource>`` on ``node``."""
        stdout = await self._run(node, "get", resource, "-o", "json", timeout=timeout)
        return parse_resource_stream(stdout)

    async def read_file(self, node: str, path: str, *, timeout: int | None = None) -> str:
        """Return the contents of a host file via ``talosctl read <path>``."""
        return await self._run(node, "read", path, timeout=timeout)

    async def version(self, node: str, *, timeout: int | None = None) -> dict[str, str]:
        """Return ``{"arch", "os_arch", "tag"}`` for the node's Talos server."""
        stdout = await self._run(node, "version", timeout=timeout)
        return parse_version_server(stdout)

    async def health_check(self, node: str) -> tuple[bool, str | None]:
        """Quick reachability/auth probe. Returns ``(ok, error_message)``."""
        try:
            await self.version(node, timeout=10)
        except TalosError as exc:
            return False, str(exc)
        return True, None

    async def install_image(self, node: str, *, timeout: int | None = None) -> str:
        """The installer image the node's machine config names (``machine.install.image``)."""
        docs = await self.get_resources(node, "machineconfig", timeout=timeout)
        spec = next(
            (d.get("spec") for d in docs if d.get("metadata", {}).get("id") == "v1alpha1"), None
        )
        if spec is None:
            spec = next((d.get("spec") for d in docs if isinstance(d.get("spec"), str)), None)
        if not isinstance(spec, str):
            raise TalosError(f"{node} returned no machine config document")
        return install_image_from_config(spec)

    async def machine_ready(self, node: str, *, timeout: int | None = None) -> tuple[bool, str]:
        """``(ready, stage)`` from the node's ``machinestatus`` resource."""
        docs = await self.get_resources(node, "machinestatus", timeout=timeout)
        spec = docs[0].get("spec", {}) if docs else {}
        stage = str(spec.get("stage") or "unknown")
        ready = stage == "running" and bool((spec.get("status") or {}).get("ready"))
        return ready, stage

    async def kubernetes_version(self, node: str, *, timeout: int | None = None) -> str:
        """The Kubernetes version the node's kubelet runs, from its ``kubeletconfig`` image tag."""
        docs = await self.get_resources(node, "kubeletconfig", timeout=timeout)
        image = str((docs[0].get("spec") or {}).get("image") or "") if docs else ""
        _, sep, tag = image.rpartition(":")
        if not sep or "/" in tag:
            raise TalosError(f"{node} reports no kubelet image tag (got {image!r})")
        return tag.removeprefix("v")

    async def members(self, node: str, *, timeout: int | None = None) -> list[dict[str, Any]]:
        """The cluster's discovered members: ``{hostname, machine_type, address, talos}`` each."""
        out: list[dict[str, Any]] = []
        for doc in await self.get_resources(node, "members", timeout=timeout):
            spec = doc.get("spec") or {}
            addresses = [str(a) for a in spec.get("addresses") or []]
            os_version = str(spec.get("operatingSystem") or "")
            match = re.search(r"\((v[0-9][^)]*)\)", os_version)
            out.append(
                {
                    "hostname": str(
                        spec.get("hostname") or doc.get("metadata", {}).get("id") or ""
                    ),
                    "machine_type": str(spec.get("machineType") or ""),
                    "address": addresses[0] if addresses else "",
                    "talos": match.group(1) if match else os_version,
                }
            )
        return out

    async def cluster_health(self, node: str, *, timeout: int = 180) -> tuple[bool, str]:
        """``talosctl health`` from ``node``'s point of view: ``(ok, last lines)``. Read-only."""
        args = [*self._base_args(node), "health", "--wait-timeout", f"{max(timeout - 10, 30)}s"]
        try:
            code, stdout, stderr = await self._runner(args, timeout)
        except TalosError as exc:
            return False, str(exc)
        tail = "\n".join((stderr or stdout).strip().splitlines()[-4:])
        return code == 0, tail

    # --- writes --------------------------------------------------------------
    # Only ``engine/executor.py`` (and ``engine/rollback.py``, which it drives)
    # may call these; ``tests/test_write_isolation.py`` enforces it.

    async def upgrade_node(self, node: str, image: str, *, timeout: int | None = None) -> str:
        """``talosctl upgrade --image <image>`` without waiting: the caller watches the return."""
        return await self._run(
            node, "upgrade", "--image", image, "--wait=false", timeout=timeout or 120
        )

    async def rollback_node(self, node: str, *, timeout: int | None = None) -> str:
        """``talosctl rollback``: boot the previous install again. The caller watches the return."""
        return await self._run(node, "rollback", timeout=timeout or 120)

    async def upgrade_k8s(self, node: str, version: str, *, timeout: int | None = None) -> str:
        """``talosctl upgrade-k8s --to <version>``: synchronous, control planes then kubelets."""
        return await self._run(
            node, "upgrade-k8s", "--to", version, timeout=timeout or K8S_UPGRADE_TIMEOUT_S
        )


async def wait_for_tag(
    talos: TalosAdapter,
    node: str,
    tag: str,
    *,
    timeout_s: int = RETURN_TIMEOUT_S,
    poll_s: int = RETURN_POLL_S,
    going_down_s: int = GOING_DOWN_S,
) -> int:
    """Block until ``node`` answers with Talos ``tag`` and reports ready; return the seconds it took.

    First waits for the node to stop answering, so the version it ran before
    the reboot is not mistaken for the one it comes back with; a node that
    never leaves is still accepted once it reports the wanted tag. Raises
    :class:`TalosError` after ``timeout_s`` — the one failure that matters is a
    node that never comes back, and it must fail the receipt, not succeed it.
    """
    started = time.monotonic()
    while time.monotonic() - started < going_down_s:
        try:
            current = await talos.version(node, timeout=10)
        except TalosError:
            break
        if current.get("tag") == tag:
            break
        await asyncio.sleep(poll_s)
    while time.monotonic() - started < timeout_s:
        try:
            current = await talos.version(node, timeout=10)
            ready, _ = await talos.machine_ready(node, timeout=10)
        except TalosError:
            current, ready = {}, False
        if ready and current.get("tag") == tag:
            return int(time.monotonic() - started)
        await asyncio.sleep(poll_s)
    raise TalosError(
        f"{node} did not come back running {tag} within {timeout_s} s — check its console"
    )


__all__ = [
    "GOING_DOWN_S",
    "K8S_UPGRADE_TIMEOUT_S",
    "RETURN_POLL_S",
    "RETURN_TIMEOUT_S",
    "TalosAdapter",
    "TalosCommandRunner",
    "TalosError",
    "image_with_tag",
    "install_image_from_config",
    "parse_resource_stream",
    "parse_version_server",
    "wait_for_tag",
]
