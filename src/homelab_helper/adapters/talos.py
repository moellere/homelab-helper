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

The binary invocation is injectable via ``runner`` so probe/adapter tests run
without a real ``talosctl`` or a live cluster.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


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
    """Read-only adapter that drives ``talosctl`` against the machine API."""

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


__all__ = [
    "TalosAdapter",
    "TalosCommandRunner",
    "TalosError",
    "parse_resource_stream",
    "parse_version_server",
]
