"""Skill Inferer — passive per-domain proficiency from chat (P4-AC6).

Every chat message the operator sends is a free signal about what they work
with and at what depth. This module turns that signal into a per-domain
:class:`SkillProfile` — deliberately **deterministic**: a curated lexicon maps
terms to domains, with basic terms scoring 1 and advanced terms 3. No LLM call
sits in this path, which keeps the signal free, reproducible, and — since the
profile later feeds the Phase-6 trust gradient's per-domain *hints* — free of
model judgment in anything trust-adjacent. (An LLM-assessed refinement can
layer on top later; the accumulation model doesn't change.)

Levels are score thresholds over accumulated weighted evidence. Two rules:

- ``source=manual`` rows are operator-pinned — inference updates their score
  bookkeeping but **never their level**.
- Levels only ratchet up from inference; talking about basics again doesn't
  demote anyone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from homelab_helper.db.enums import SkillLevel, SkillSource
from homelab_helper.db.models import SkillProfile

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_BASIC_WEIGHT = 1.0
_ADVANCED_WEIGHT = 3.0

# Score thresholds — ordered high→low; first match wins.
_LEVEL_FLOORS: tuple[tuple[float, SkillLevel], ...] = (
    (12.0, SkillLevel.ADVANCED),
    (6.0, SkillLevel.INTERMEDIATE),
    (2.0, SkillLevel.BASIC),
    (0.0, SkillLevel.NOVICE),
)

_LEVEL_ORDER = {
    SkillLevel.NOVICE: 0,
    SkillLevel.BASIC: 1,
    SkillLevel.INTERMEDIATE: 2,
    SkillLevel.ADVANCED: 3,
}

# domain -> (basic terms, advanced terms). Terms are matched as whole words,
# case-insensitively. Curated for homelab vocabulary; extending a domain is a
# one-line change and safe (scores only ever accumulate).
LEXICON: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "storage": (
        frozenset({"disk", "ssd", "nvme", "raid", "nas", "smart", "backup", "volume"}),
        frozenset(
            {
                "zfs",
                "ceph",
                "zpool",
                "vdev",
                "resilver",
                "scrub",
                "osd",
                "erasure",
                "iscsi",
                "mdadm",
                "wwn",
            }
        ),
    ),
    "container-orchestration": (
        frozenset({"docker", "container", "pod", "image", "compose"}),
        frozenset(
            {
                "kubernetes",
                "k8s",
                "kubelet",
                "helm",
                "argocd",
                "gitops",
                "talos",
                "etcd",
                "ingress",
                "statefulset",
                "operator",
            }
        ),
    ),
    "networking": (
        frozenset({"dns", "ip", "router", "wifi", "subnet", "firewall", "port"}),
        frozenset(
            {
                "vlan",
                "bgp",
                "vxlan",
                "wireguard",
                "mtu",
                "cidr",
                "nat",
                "dhcp",
                "unifi",
                "pihole",
                "reverse-proxy",
            }
        ),
    ),
    "virtualization": (
        frozenset({"vm", "virtual", "hypervisor", "guest", "snapshot"}),
        frozenset(
            {"proxmox", "qemu", "kvm", "lxc", "cloud-init", "virtio", "passthrough", "sriov"}
        ),
    ),
    "linux-admin": (
        frozenset({"linux", "ssh", "bash", "cron", "log", "reboot", "sudo"}),
        frozenset(
            {"systemd", "journalctl", "selinux", "cgroup", "initramfs", "udev", "grub", "kernel"}
        ),
    ),
}


@dataclass
class SkillObserveResult:
    """What one text observation did to the profile."""

    matched: dict[str, float] = field(default_factory=dict)
    promoted: dict[str, str] = field(default_factory=dict)

    @property
    def touched(self) -> bool:
        return bool(self.matched)


def _score_text(text: str) -> dict[str, float]:
    """Weighted per-domain score for one message (pure)."""
    words = set(re.findall(r"[a-z0-9-]+", text.lower()))
    scores: dict[str, float] = {}
    for domain, (basic, advanced) in LEXICON.items():
        score = len(words & basic) * _BASIC_WEIGHT + len(words & advanced) * _ADVANCED_WEIGHT
        if score > 0:
            scores[domain] = score
    return scores


def level_for_score(score: float) -> SkillLevel:
    for floor, level in _LEVEL_FLOORS:
        if score >= floor:
            return level
    return SkillLevel.NOVICE


async def observe_text(session: AsyncSession, text: str) -> SkillObserveResult:
    """Accumulate one message's evidence into the profile (passive path)."""
    result = SkillObserveResult(matched=_score_text(text))
    for domain, gained in result.matched.items():
        row = (
            await session.execute(select(SkillProfile).where(SkillProfile.domain == domain))
        ).scalar_one_or_none()
        if row is None:
            # Column defaults land at flush; set them now so the ratchet
            # comparison below sees real values on a fresh row.
            row = SkillProfile(
                domain=domain,
                level=SkillLevel.NOVICE,
                source=SkillSource.INFERRED,
                score=0.0,
                evidence_count=0,
            )
            session.add(row)
        row.score += gained
        row.evidence_count += 1
        if row.source == SkillSource.MANUAL:
            continue  # operator-pinned level; keep bookkeeping only
        inferred = level_for_score(row.score)
        if _LEVEL_ORDER[inferred] > _LEVEL_ORDER[row.level]:
            result.promoted[domain] = inferred.value
            row.level = inferred
    await session.flush()
    return result


async def set_skill(session: AsyncSession, domain: str, level: SkillLevel) -> SkillProfile:
    """Operator override — pins the level; inference can no longer change it."""
    row = (
        await session.execute(select(SkillProfile).where(SkillProfile.domain == domain))
    ).scalar_one_or_none()
    if row is None:
        row = SkillProfile(domain=domain, score=0.0, evidence_count=0)
        session.add(row)
    row.level = level
    row.source = SkillSource.MANUAL
    await session.flush()
    return row


async def get_profile(session: AsyncSession) -> list[SkillProfile]:
    return list(
        (await session.execute(select(SkillProfile).order_by(SkillProfile.domain))).scalars().all()
    )


def render_profile_for_prompt(rows: list[SkillProfile]) -> str:
    """One line for the chat system prompt, so answers match the operator."""
    if not rows:
        return ""
    parts = ", ".join(f"{r.domain}={r.level.value}" for r in rows)
    return (
        f"Operator skill profile: {parts}. Match explanation depth to it — "
        "don't over-explain domains they know well."
    )


__all__ = [
    "LEXICON",
    "SkillObserveResult",
    "get_profile",
    "level_for_score",
    "observe_text",
    "render_profile_for_prompt",
    "set_skill",
]
