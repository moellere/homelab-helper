"""NetworkPath — typed link graph with worst-link inheritance (P5-AC6).

The planner's model of "how far apart are these two hosts, really." Sites hold
hosts; typed links (cable/fiber/wireless/vpn/wan) connect sites with declared
bandwidth, latency, and reliability. A path's characteristics inherit from its
links the only honest way: **bandwidth is the minimum, latency is the sum,
reliability is the worst** — one VPN hop makes the whole path a VPN-grade
path, which is exactly why a Ceph-replicated workload must not span it.

The topology is **operator-declared YAML** — no probe can measure that the
route to the other site rides a WireGuard tunnel over consumer cable. Absent a
topology file, every host is assumed co-located on one LAN (the single-site
homelab default), so nothing degrades for operators who never declare one.
``fixtures/network-topology.example.yaml`` documents the format; point
``HOMELAB_HELPER_NETWORK_TOPOLOGY`` at your real one.

Path search is Dijkstra by latency over the site graph. Homelab scale — a
handful of sites — so clarity beats asymptotics everywhere.
"""

from __future__ import annotations

import heapq
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

TOPOLOGY_ENV_VAR = "HOMELAB_HELPER_NETWORK_TOPOLOGY"

_LINK_KINDS = {"cable", "fiber", "lan", "wireless", "vpn", "wan"}
_RELIABILITIES = ("high", "normal", "best-effort")  # ordered best → worst

# Intra-site default: wired gigabit LAN.
_LAN_BANDWIDTH_MBPS = 1000.0
_LAN_LATENCY_MS = 0.5

_LAN_GRADE_KINDS = {"cable", "fiber", "lan"}
_LAN_GRADE_MAX_LATENCY_MS = 5.0


class TopologyError(ValueError):
    """The topology file is malformed — message names the entry and problem."""


@dataclass(frozen=True)
class Link:
    a: str
    b: str
    kind: str
    bandwidth_mbps: float
    latency_ms: float
    reliability: str = "high"


@dataclass(frozen=True)
class PathCharacteristics:
    """What a workload experiences end-to-end: inherited from the worst links."""

    hops: tuple[str, ...]
    links: tuple[Link, ...]
    bandwidth_mbps: float
    latency_ms: float
    reliability: str

    @property
    def same_site(self) -> bool:
        return not self.links

    @property
    def lan_grade(self) -> bool:
        """LAN-grade end to end: wired kinds only, low latency, no best-effort."""
        return all(link.kind in _LAN_GRADE_KINDS for link in self.links) and (
            self.latency_ms <= _LAN_GRADE_MAX_LATENCY_MS and self.reliability != "best-effort"
        )

    @property
    def worst_link(self) -> Link | None:
        """The link that set the path's reliability (ties: highest latency)."""
        if not self.links:
            return None
        return max(
            self.links,
            key=lambda x: (_RELIABILITIES.index(x.reliability), x.latency_ms),
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view for the MCP surface."""
        return {
            "hops": list(self.hops),
            "links": [asdict(link) for link in self.links],
            "bandwidth_mbps": self.bandwidth_mbps,
            "latency_ms": self.latency_ms,
            "reliability": self.reliability,
            "same_site": self.same_site,
            "lan_grade": self.lan_grade,
            "summary": self.describe(),
        }

    def describe(self) -> str:
        if self.same_site:
            return "same site (LAN)"
        route = " → ".join(self.hops)
        return (
            f"{route}: {self.bandwidth_mbps:.0f} Mbps min, "
            f"{self.latency_ms:.1f} ms total, reliability {self.reliability}"
        )


@dataclass(frozen=True)
class Topology:
    host_sites: dict[str, str]
    links: tuple[Link, ...]

    def site_of(self, hostname: str) -> str | None:
        return self.host_sites.get(hostname)

    def path(self, host_a: str, host_b: str) -> PathCharacteristics | None:
        """Characteristics between two hosts; None when no route exists.

        Unknown hosts are treated as on-LAN with every co-sited host — the
        single-site default degrades gracefully for undeclared machines.
        """
        site_a, site_b = self.site_of(host_a), self.site_of(host_b)
        if site_a is None or site_b is None or site_a == site_b:
            return PathCharacteristics(
                hops=(site_a or site_b or "local",),
                links=(),
                bandwidth_mbps=_LAN_BANDWIDTH_MBPS,
                latency_ms=_LAN_LATENCY_MS,
                reliability="high",
            )

        adjacency: dict[str, list[Link]] = {}
        for link in self.links:
            adjacency.setdefault(link.a, []).append(link)
            adjacency.setdefault(link.b, []).append(
                Link(
                    link.b,
                    link.a,
                    link.kind,
                    link.bandwidth_mbps,
                    link.latency_ms,
                    link.reliability,
                )
            )

        # Dijkstra by cumulative latency. The counter breaks heap ties so the
        # comparison never reaches the (unorderable) Link trail.
        counter = 0
        queue: list[tuple[float, int, str, tuple[Link, ...]]] = [(0.0, counter, site_a, ())]
        seen: set[str] = set()
        while queue:
            latency, _, site, trail = heapq.heappop(queue)
            if site in seen:
                continue
            seen.add(site)
            if site == site_b:
                return PathCharacteristics(
                    hops=(site_a, *(link.b for link in trail)),
                    links=trail,
                    bandwidth_mbps=min(link.bandwidth_mbps for link in trail),
                    latency_ms=sum(link.latency_ms for link in trail),
                    reliability=max(
                        (link.reliability for link in trail),
                        key=_RELIABILITIES.index,
                    ),
                )
            for link in adjacency.get(site, []):
                if link.b not in seen:
                    counter += 1
                    heapq.heappush(
                        queue, (latency + link.latency_ms, counter, link.b, (*trail, link))
                    )
        return None


def _parse_link(index: int, raw: dict[str, Any]) -> Link:
    def fail(problem: str) -> TopologyError:
        return TopologyError(f"link #{index}: {problem}")

    for required in ("a", "b", "kind"):
        if not raw.get(required):
            raise fail(f"missing {required!r}")
    kind = str(raw["kind"]).lower()
    if kind not in _LINK_KINDS:
        raise fail(f"kind {kind!r} must be one of {sorted(_LINK_KINDS)}")
    reliability = str(raw.get("reliability", "high")).lower()
    if reliability not in _RELIABILITIES:
        raise fail(f"reliability {reliability!r} must be one of {list(_RELIABILITIES)}")
    try:
        bandwidth = float(raw.get("bandwidth_mbps", _LAN_BANDWIDTH_MBPS))
        latency = float(raw.get("latency_ms", 1.0))
    except (TypeError, ValueError) as exc:
        raise fail(f"non-numeric bandwidth/latency: {exc}") from exc
    return Link(
        a=str(raw["a"]),
        b=str(raw["b"]),
        kind=kind,
        bandwidth_mbps=bandwidth,
        latency_ms=latency,
        reliability=reliability,
    )


def load_topology(path: Path | None = None) -> Topology | None:
    """The declared topology, or None (single-site assumption).

    ``path`` overrides (tests); otherwise ``HOMELAB_HELPER_NETWORK_TOPOLOGY``
    names the file, and no variable means no topology.
    """
    if path is None:
        env = os.environ.get(TOPOLOGY_ENV_VAR)
        if not env:
            return None
        path = Path(env)
    payload = yaml.safe_load(path.read_text())
    if not isinstance(payload, dict) or not isinstance(payload.get("sites"), dict):
        raise TopologyError(f"{path}: expected a top-level 'sites' mapping")

    host_sites: dict[str, str] = {}
    for site, spec in payload["sites"].items():
        hosts = (spec or {}).get("hosts") or []
        for hostname in hosts:
            existing = host_sites.get(str(hostname))
            if existing is not None and existing != site:
                raise TopologyError(
                    f"host {hostname!r} is declared in two sites: {existing!r} and {site!r}"
                )
            host_sites[str(hostname)] = str(site)

    links = tuple(
        _parse_link(i, raw if isinstance(raw, dict) else {})
        for i, raw in enumerate(payload.get("links") or [], start=1)
    )
    known_sites = set(payload["sites"])
    for link in links:
        for end in (link.a, link.b):
            if end not in known_sites:
                raise TopologyError(f"link {link.a}↔{link.b} references unknown site {end!r}")
    return Topology(host_sites=host_sites, links=links)


__all__ = [
    "TOPOLOGY_ENV_VAR",
    "Link",
    "PathCharacteristics",
    "Topology",
    "TopologyError",
    "load_topology",
]
