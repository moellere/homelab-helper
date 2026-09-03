"""Secret references — one resolver for every credential the harness reads.

Every secret-valued setting (``HOMELAB_HELPER_*_TOKEN``, ``*_SECRET``,
``*_API_KEY``, ``*_PASSWORD``) accepts either the literal value or a
*reference* naming where the value lives, so a plaintext ``.env`` is a choice
rather than the only option:

- ``env:OTHER_VAR`` — indirection to another environment variable.
- ``file:<path>#<key>`` — a key in a YAML/JSON mapping. A ``*.age`` file is
  decrypted with the operator's ``age`` binary (identity from
  ``HOMELAB_HELPER_AGE_IDENTITY``, default ``<config dir>/age.key``); a file
  named ``*.sops.*`` or carrying a top-level ``sops`` key is decrypted with
  ``sops -d``. Nothing is vendored: the operator's own tooling and keys do the
  work, and the decrypted text never touches disk.
- ``keyring:<service>/<username>`` — the OS keyring via the optional
  ``keyring`` package (``uv tool install 'homelab-helper[keyring]'``).

A value that matches no scheme is the literal secret, so an existing ``.env``
behaves exactly as before. Each reference resolves at most once per process.

Every value that passes through here is remembered so :func:`redact` can
scrub it from error text — an adapter's HTTP error may echo a header, and the
MCP surface hands error strings to a model. This module imports nothing from
the rest of the package, so ``config`` and the LLM backends can both use it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

AGE_IDENTITY_VAR = "HOMELAB_HELPER_AGE_IDENTITY"
SCHEMES: tuple[str, ...] = ("env", "file", "keyring")
_MIN_REDACT_LEN = 4  # never scrub "1" or "no" out of an error message


class SecretError(RuntimeError):
    """A reference could not be resolved; the message says which and why."""


_cache: dict[str, str] = {}
_known: set[str] = set()


def is_reference(value: str) -> bool:
    scheme, sep, rest = value.partition(":")
    return bool(sep) and scheme in SCHEMES and bool(rest)


def _run(argv: Sequence[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise SecretError(f"{argv[0]!r} is not installed or not on PATH") from None
    if proc.returncode != 0:
        raise SecretError(f"{argv[0]} failed: {proc.stderr.strip()[:300] or proc.returncode}")
    return proc.stdout


def _identity_path() -> Path:
    explicit = os.environ.get(AGE_IDENTITY_VAR)
    if explicit:
        return Path(explicit).expanduser()
    # Mirrors config.config_dir() without importing config (no cycle).
    home = os.environ.get("HOMELAB_HELPER_HOME")
    if home:
        return Path(home).expanduser() / "age.key"
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "homelab-helper" / "age.key"


def _load_mapping(path: Path, runner: Callable[[Sequence[str]], str]) -> dict[str, Any]:
    if not path.is_file():
        raise SecretError(f"secrets file {path} does not exist")
    if path.suffix == ".age":
        identity = _identity_path()
        if not identity.is_file():
            raise SecretError(
                f"age identity {identity} not found; set {AGE_IDENTITY_VAR} to your key file"
            )
        text = runner(["age", "-d", "-i", str(identity), str(path)])
    elif ".sops." in path.name:
        text = runner(["sops", "-d", str(path)])
    else:
        text = path.read_text()
        probe = yaml.safe_load(text)
        if isinstance(probe, dict) and "sops" in probe:
            text = runner(["sops", "-d", str(path)])
    loaded = json.loads(text) if path.suffix == ".json" and ".sops." not in path.name else None
    if loaded is None:
        loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise SecretError(f"secrets file {path} is not a mapping")
    return loaded


def _dig(mapping: dict[str, Any], key: str, path: Path) -> str:
    node: Any = mapping
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise SecretError(f"key {key!r} not found in {path}")
        node = node[part]
    if isinstance(node, dict | list):
        raise SecretError(f"key {key!r} in {path} is not a scalar")
    return str(node)


def _resolve_file(spec: str, runner: Callable[[Sequence[str]], str]) -> str:
    raw_path, sep, key = spec.partition("#")
    if not sep or not key:
        raise SecretError(f"file reference needs '<path>#<key>', got {spec!r}")
    path = Path(raw_path).expanduser()
    return _dig(_load_mapping(path, runner), key, path)


def _resolve_keyring(spec: str) -> str:
    service, sep, username = spec.partition("/")
    if not sep or not service or not username:
        raise SecretError(f"keyring reference needs '<service>/<username>', got {spec!r}")
    try:
        import keyring  # noqa: PLC0415 — optional dependency
    except ImportError:
        raise SecretError(
            "the keyring backend needs the optional dependency: "
            "uv tool install 'homelab-helper[keyring]'"
        ) from None
    value = keyring.get_password(service, username)
    if value is None:
        raise SecretError(f"no keyring entry for service {service!r}, user {username!r}")
    return str(value)


def _resolve_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SecretError(f"env reference names {name}, which is unset")
    if is_reference(value):
        raise SecretError(f"env reference {name} points at another reference; one level only")
    return value


def _remember(value: str) -> None:
    if len(value) >= _MIN_REDACT_LEN:
        _known.add(value)


def resolve_secret(
    value: str | None, *, runner: Callable[[Sequence[str]], str] | None = None
) -> str | None:
    """A literal passes through; a reference resolves (once) to its value."""
    if not value:
        return value
    if not is_reference(value):
        _remember(value)
        return value
    if value in _cache:
        return _cache[value]
    scheme, _, rest = value.partition(":")
    run = runner or _run
    if scheme == "env":
        resolved = _resolve_env(rest)
    elif scheme == "file":
        resolved = _resolve_file(rest, run)
    else:
        resolved = _resolve_keyring(rest)
    _cache[value] = resolved
    _remember(resolved)
    return resolved


def secret_from_env(var: str) -> str | None:
    """``os.environ.get(var)`` with reference resolution and redaction memory."""
    return resolve_secret(os.environ.get(var))


def reference_scheme(value: str | None) -> str | None:
    """``"file"`` / ``"keyring"`` / ``"env"`` for a reference, else None."""
    if not value or not is_reference(value):
        return None
    return value.partition(":")[0]


def redact(text: str) -> str:
    """Replace every secret value seen this process with ``***``."""
    for secret in sorted(_known, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def forget_secrets() -> None:
    """Drop the cache and the redaction memory (tests)."""
    _cache.clear()
    _known.clear()


__all__ = [
    "AGE_IDENTITY_VAR",
    "SCHEMES",
    "SecretError",
    "forget_secrets",
    "is_reference",
    "redact",
    "reference_scheme",
    "resolve_secret",
    "secret_from_env",
]
