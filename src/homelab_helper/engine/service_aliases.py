"""Explicit service alias map — the operator's override for cross-resolver identity.

``dns_reconcile.service_key`` names a service by the leftmost DNS label, so
``ha.lan`` (UniFi) and ``ha.example.com`` (Cloudflare) attach to one ``Service``
called ``ha``. That heuristic is right often enough for a homelab and wrong in
two ways: two distinct services can share a short name (``grafana.lan`` at one
site, ``grafana.wyola.lan`` at another), and one service can span unrelated
names (``ha.lan`` and ``homeassistant.example.com``). This file lets the
operator say which is which.

``HOMELAB_HELPER_SERVICE_ALIASES`` names a YAML file::

    services:
      home-assistant:
        hostnames: [ha.lan, homeassistant.example.com]
      grafana-wyola:
        hostnames: ["grafana.wyola.lan", "*.grafana.wyola.lan"]

A hostname maps to exactly one service; a glob (``*``/``?``) is matched with
``fnmatch`` after exact names. Anything not listed keeps the leftmost-label
default, so the file only ever has to name the exceptions. See
``fixtures/service-aliases.example.yaml``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

ALIASES_ENV_VAR = "HOMELAB_HELPER_SERVICE_ALIASES"
_GLOB_CHARS = ("*", "?", "[")


class AliasError(ValueError):
    """The alias file is malformed — the message names the entry and problem."""


@dataclass(frozen=True)
class ServiceAliasMap:
    exact: dict[str, str]
    patterns: tuple[tuple[str, str], ...]
    source: Path | None = None

    @property
    def empty(self) -> bool:
        return not self.exact and not self.patterns

    @property
    def services(self) -> list[str]:
        return sorted({*self.exact.values(), *(s for _, s in self.patterns)})

    def resolve(self, hostname: str) -> str | None:
        """The aliased service name for ``hostname``, or None to use the default."""
        h = hostname.strip().lower()
        hit = self.exact.get(h)
        if hit is not None:
            return hit
        for pattern, service in self.patterns:
            if fnmatchcase(h, pattern):
                return service
        return None


def parse_aliases(payload: Any, *, source: Path | None = None) -> ServiceAliasMap:
    """Validate the YAML payload into a :class:`ServiceAliasMap`."""
    if not isinstance(payload, dict) or not isinstance(payload.get("services"), dict):
        raise AliasError("expected a top-level 'services' mapping")
    exact: dict[str, str] = {}
    patterns: list[tuple[str, str]] = []
    owner: dict[str, str] = {}
    for raw_name, spec in payload["services"].items():
        name = str(raw_name).strip().lower()
        if not name:
            raise AliasError("a service name is empty")
        hostnames = spec.get("hostnames") if isinstance(spec, dict) else spec
        if not isinstance(hostnames, list) or not hostnames:
            raise AliasError(f"service {name!r}: 'hostnames' must be a non-empty list")
        for raw_host in hostnames:
            host = str(raw_host).strip().lower()
            if not host:
                raise AliasError(f"service {name!r}: empty hostname")
            if host in owner and owner[host] != name:
                raise AliasError(
                    f"hostname {host!r} is claimed by both {owner[host]!r} and {name!r}"
                )
            owner[host] = name
            if any(c in host for c in _GLOB_CHARS):
                patterns.append((host, name))
            else:
                exact[host] = name
    return ServiceAliasMap(exact=exact, patterns=tuple(patterns), source=source)


def load_service_aliases(path: Path | None = None) -> ServiceAliasMap | None:
    """The operator's alias map, or None when no file is configured.

    ``path`` overrides (tests); otherwise ``HOMELAB_HELPER_SERVICE_ALIASES``
    names the file, and no variable means no aliases.
    """
    if path is None:
        env = os.environ.get(ALIASES_ENV_VAR)
        if not env:
            return None
        path = Path(env).expanduser()
    return parse_aliases(yaml.safe_load(path.read_text()), source=path)


def service_key(hostname: str, aliases: ServiceAliasMap | None = None) -> str:
    """Canonical service name for a hostname: the alias map first, else the leftmost label.

    Internal and external DNS suffix the same service differently (``ha.lan``
    inside, ``ha.example.com`` outside); both should attach to one ``Service``,
    so the default key is the part that actually names the service.
    """
    if aliases is not None:
        mapped = aliases.resolve(hostname)
        if mapped is not None:
            return mapped
    return hostname.strip().lower().split(".", 1)[0]


__all__ = [
    "ALIASES_ENV_VAR",
    "AliasError",
    "ServiceAliasMap",
    "load_service_aliases",
    "parse_aliases",
    "service_key",
]
