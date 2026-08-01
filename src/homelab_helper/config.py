"""Effective configuration: ``.env`` loading and per-source credential status.

One place answers "what is the harness actually wired to talk to?" so the CLI
(``helper config``) and the MCP server (``config_status``) can't drift.

``.env`` is loaded from the nearest ancestor of the working directory, and never
overrides a real environment variable — an explicit export always wins. The file
is gitignored; secrets belong there, not in shell history or an MCP client's
config block.

Secret-valued variables are reported as set/unset. Their values are never
returned by anything in this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./homelab.db"

_ENV_FILENAME = ".env"

NO_DOTENV_VAR = "HOMELAB_HELPER_NO_DOTENV"
"""Set truthy to skip ``.env`` loading entirely.

The test suite sets it: without it, a run on an operator's workstation reads
that operator's real credentials and behaves differently from CI — and any test
that monkeypatches an adapter factory is silently bypassed when the ambient
config names something the factory then builds for real.
"""


def _named_unifi_controllers() -> list[str]:
    """Controller names that carry a complete per-controller credential set.

    The multi-controller form supersedes the unsuffixed UniFi variables, so a
    lab using it isn't missing anything.
    """
    raw = os.environ.get("HOMELAB_HELPER_UNIFI_CONTROLLERS") or ""
    ready = []
    for name in (n.strip() for n in raw.split(",")):
        if not name:
            continue
        prefix = f"HOMELAB_HELPER_UNIFI_{name.upper().replace('-', '_')}_"
        if os.environ.get(prefix + "URL") and os.environ.get(prefix + "API_KEY"):
            ready.append(name)
    return ready


@dataclass(frozen=True)
class SourceConfig:
    """The environment contract for one discovery source."""

    name: str
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    secret: frozenset[str] = frozenset()
    note: str | None = None
    alt_check: Callable[[], bool] | None = None
    """Optional second way this source can be satisfied, checked before the
    required list."""

    def missing(self) -> tuple[str, ...]:
        """Required variables that are unset or empty.

        A source that declares ``alt_check`` may satisfy itself another way —
        UniFi's multi-controller form replaces the unsuffixed variables with a
        per-controller set, and reporting those as missing would be wrong.
        """
        if self.alt_check is not None and self.alt_check():
            return ()
        return tuple(var for var in self.required if not os.environ.get(var))

    def configured(self) -> bool:
        return not self.missing()


# Ordered for display. ``k8s`` has no required vars — the adapter shells out to
# kubectl, which falls back to the ambient kubeconfig.
SOURCES: tuple[SourceConfig, ...] = (
    SourceConfig(
        name="netbox",
        required=("HOMELAB_HELPER_NETBOX_URL", "HOMELAB_HELPER_NETBOX_TOKEN"),
        optional=("HOMELAB_HELPER_NETBOX_VERIFY_SSL",),
        secret=frozenset({"HOMELAB_HELPER_NETBOX_TOKEN"}),
        note="sync target, not a discovery source",
    ),
    SourceConfig(
        name="proxmox",
        required=(
            "HOMELAB_HELPER_PROXMOX_URL",
            "HOMELAB_HELPER_PROXMOX_TOKEN_ID",
            "HOMELAB_HELPER_PROXMOX_TOKEN_SECRET",
        ),
        optional=("HOMELAB_HELPER_PROXMOX_VERIFY_SSL",),
        secret=frozenset({"HOMELAB_HELPER_PROXMOX_TOKEN_SECRET"}),
    ),
    SourceConfig(
        name="unifi",
        required=("HOMELAB_HELPER_UNIFI_URL", "HOMELAB_HELPER_UNIFI_API_KEY"),
        optional=(
            "HOMELAB_HELPER_UNIFI_SITE",
            "HOMELAB_HELPER_UNIFI_VERIFY_SSL",
            "HOMELAB_HELPER_UNIFI_CONTROLLERS",
        ),
        secret=frozenset({"HOMELAB_HELPER_UNIFI_API_KEY"}),
        alt_check=lambda: bool(_named_unifi_controllers()),
    ),
    SourceConfig(
        name="cloudflare",
        required=("HOMELAB_HELPER_CLOUDFLARE_API_TOKEN",),
        optional=("HOMELAB_HELPER_CLOUDFLARE_ZONE", "HOMELAB_HELPER_CLOUDFLARE_ZONE_ID"),
        secret=frozenset({"HOMELAB_HELPER_CLOUDFLARE_API_TOKEN"}),
    ),
    SourceConfig(
        name="argocd",
        required=("HOMELAB_HELPER_ARGOCD_URL", "HOMELAB_HELPER_ARGOCD_API_TOKEN"),
        optional=("HOMELAB_HELPER_ARGOCD_VERIFY_SSL",),
        secret=frozenset({"HOMELAB_HELPER_ARGOCD_API_TOKEN"}),
    ),
    SourceConfig(
        name="omv",
        required=(
            "HOMELAB_HELPER_OMV_URL",
            "HOMELAB_HELPER_OMV_USERNAME",
            "HOMELAB_HELPER_OMV_PASSWORD",
        ),
        optional=("HOMELAB_HELPER_OMV_VERIFY_SSL",),
        secret=frozenset({"HOMELAB_HELPER_OMV_PASSWORD"}),
    ),
    SourceConfig(
        name="k8s",
        required=(),
        optional=("HOMELAB_HELPER_KUBECONFIG", "HOMELAB_HELPER_KUBE_CONTEXT"),
        note="falls back to the ambient kubeconfig",
    ),
)


def find_env_files(start: Path | None = None) -> list[Path]:
    """Env files to load, highest precedence first.

    Two locations, both deliberate: a project ``.env`` found by walking up from
    ``start`` and stopping at the directory holding ``.git``, then ``~/.env``.
    The walk is bounded so an unrelated ``.env`` somewhere up the tree is never
    pulled into a process that talks to live infrastructure — the home-directory
    file is included because it's named explicitly, not because the walk reached
    it. A project file wins over the home file key-by-key.
    """
    found: list[Path] = []
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / _ENV_FILENAME
        if candidate.is_file():
            found.append(candidate)
            break
        if (directory / ".git").exists():
            break
    home_env = Path.home() / _ENV_FILENAME
    if home_env.is_file() and home_env not in found:
        found.append(home_env)
    return found


def find_env_file(start: Path | None = None) -> Path | None:
    """Highest-precedence env file, or None."""
    files = find_env_files(start)
    return files[0] if files else None


def load_env(start: Path | None = None) -> list[Path]:
    """Load the env files without clobbering the real environment.

    Returns the files loaded, in precedence order. Call once at process entry —
    the CLI and the MCP server both do, since an MCP client launches the server
    with whatever environment it happens to have. ``override=False`` means an
    explicit export always wins, and the first file to define a key keeps it.
    """
    if os.environ.get(NO_DOTENV_VAR):
        return []
    files = find_env_files(start)
    for env_file in files:
        load_dotenv(env_file, override=False)
    return files


def database_url() -> str:
    return os.environ.get("HOMELAB_HELPER_DATABASE_URL") or DEFAULT_DATABASE_URL


def variable_status(var: str, *, secret: bool) -> dict[str, Any]:
    """Report one variable: set/unset, plus the value when it isn't a secret."""
    raw = os.environ.get(var)
    present = bool(raw)
    return {
        "variable": var,
        "set": present,
        "secret": secret,
        "value": None if (secret or not present) else raw,
    }


def source_status(source: SourceConfig) -> dict[str, Any]:
    """Credential status for one source — never includes a secret value."""
    extra: dict[str, Any] = {}
    if source.name == "unifi":
        controllers = _named_unifi_controllers()
        if controllers:
            extra["controllers"] = controllers
    return {
        "source": source.name,
        "configured": source.configured(),
        "missing": list(source.missing()),
        **extra,
        "note": source.note,
        "variables": [
            variable_status(var, secret=var in source.secret)
            for var in (*source.required, *source.optional)
        ],
    }


def config_status() -> dict[str, Any]:
    """Whole-harness configuration snapshot for the CLI and the MCP surface."""
    env_files = find_env_files()
    url = database_url()
    sqlite_prefix = "sqlite+aiosqlite:///"
    db_exists: bool | None = None
    if url.startswith(sqlite_prefix):
        db_exists = Path(url[len(sqlite_prefix) :]).exists()
    sources = [source_status(s) for s in SOURCES]
    return {
        "database_url": url,
        "database_exists": db_exists,
        "env_files": [str(p) for p in env_files],
        "ssh_key": os.environ.get("HOMELAB_HELPER_SSH_KEY"),
        "sources": sources,
        "configured_sources": [s["source"] for s in sources if s["configured"]],
        "unconfigured_sources": [s["source"] for s in sources if not s["configured"]],
    }


__all__ = [
    "DEFAULT_DATABASE_URL",
    "SOURCES",
    "SourceConfig",
    "config_status",
    "database_url",
    "find_env_file",
    "load_env",
    "source_status",
    "variable_status",
]
