"""Conversational Discovery Agent — interview-style host onboarding (P4-AC3).

The first agent that leads to a harness-DB write, so the trust split is the
whole design: the **LLM interviews and proposes; deterministic Python
validates; the operator confirms; only then does anything get written.** The
model cannot touch the DB — it emits a structured proposal that
:func:`validate_proposal` checks (syntax, duplicates, no key material) and
:func:`apply_proposal` applies after an explicit yes.

Protocol: every model turn must be one JSON object::

    {"say": "<question or statement for the operator>",
     "proposal": null | {"hostname": ..., "primary_ip": ..., "role": ...,
                          "arch": ..., "ssh_user": ..., "ssh_key_path": ...,
                          "notes": ...},
     "done": false}

Small local models drift, so :func:`parse_agent_reply` is tolerant (fenced
blocks, prose around the object) and a parse failure becomes a corrective
message back to the model rather than a crash.

Credentials: the agent may collect an SSH username and a key *path* — a
filesystem reference, stored on ``Host.credentials_ref``. Key material and
passwords are never accepted; a proposal containing anything that looks like
a private key is rejected in validation, not just discouraged in the prompt.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from homelab_helper.db.enums import Architecture, DiscoverySource
from homelab_helper.db.models import Host

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?$")
_KEY_MATERIAL_RE = re.compile(r"PRIVATE KEY|ssh-(rsa|ed25519|dss)\s+[A-Za-z0-9+/=]{40,}")

DISCOVERY_SYSTEM_TEMPLATE = (
    "You are homelab-helper's discovery interviewer. The operator wants to add "
    "a host to their inventory. Interview them — one short question at a time — "
    "to collect: hostname, primary IP address, role/purpose (e.g. nas, "
    "hypervisor, k8s-node), CPU architecture if known (amd64/arm64/arm), and "
    "optionally an SSH username plus the filesystem PATH to an SSH key for "
    "deep discovery. Never ask for passwords or key contents — only a key "
    "path. If the operator names a host that already exists (list below), "
    "point it out and ask whether they meant a different machine.\n\n"
    "Reply with EXACTLY one JSON object per turn, no other text:\n"
    '{"say": "<your question or statement>", "proposal": null, "done": false}\n'
    'When you have enough to register the host, put it in "proposal" instead '
    "of null, using keys: hostname, primary_ip, role, arch, ssh_user, "
    "ssh_key_path, notes (unknown optional fields -> null). After the harness "
    'confirms the registration, reply with done true and a closing "say".\n\n'
    "EXISTING HOSTS:\n{hosts}"
)


@dataclass(frozen=True)
class HostProposal:
    hostname: str
    primary_ip: str | None = None
    role: str | None = None
    arch: str | None = None
    ssh_user: str | None = None
    ssh_key_path: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class AgentTurn:
    say: str
    proposal: HostProposal | None = None
    done: bool = False
    parse_error: str | None = None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_agent_reply(text: str) -> AgentTurn:
    """Extract the protocol JSON from a model reply, tolerantly."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            candidate = candidate[start : end + 1]
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return AgentTurn(say=text.strip(), parse_error=f"not valid JSON: {exc}")
    if not isinstance(payload, dict) or "say" not in payload:
        return AgentTurn(say=text.strip(), parse_error='missing "say" key')

    proposal = None
    raw = payload.get("proposal")
    if isinstance(raw, dict):
        hostname = _clean(raw.get("hostname"))
        if not hostname:
            return AgentTurn(say=str(payload["say"]), parse_error="proposal is missing a hostname")
        proposal = HostProposal(
            hostname=hostname,
            primary_ip=_clean(raw.get("primary_ip")),
            role=_clean(raw.get("role")),
            arch=_clean(raw.get("arch")),
            ssh_user=_clean(raw.get("ssh_user")),
            ssh_key_path=_clean(raw.get("ssh_key_path")),
            notes=_clean(raw.get("notes")),
        )
    return AgentTurn(
        say=str(payload["say"]), proposal=proposal, done=bool(payload.get("done", False))
    )


async def validate_proposal(session: AsyncSession, proposal: HostProposal) -> list[str]:
    """Deterministic checks the LLM cannot waive. Empty list = valid."""
    errors: list[str] = []
    if not _HOSTNAME_RE.match(proposal.hostname):
        errors.append(f"hostname {proposal.hostname!r} is not a valid hostname")
    if proposal.primary_ip:
        try:
            ipaddress.ip_address(proposal.primary_ip)
        except ValueError:
            errors.append(f"primary_ip {proposal.primary_ip!r} is not a valid IP address")
    if proposal.arch:
        try:
            Architecture(proposal.arch.lower())
        except ValueError:
            allowed = ", ".join(a.value for a in Architecture)
            errors.append(f"arch {proposal.arch!r} is not one of: {allowed}")
    for field_name in ("ssh_key_path", "notes", "ssh_user"):
        value = getattr(proposal, field_name)
        if value and _KEY_MATERIAL_RE.search(value):
            errors.append(
                f"{field_name} appears to contain key material — provide a file path, "
                "never the key itself"
            )
    conditions = [Host.hostname == proposal.hostname]
    if proposal.primary_ip:
        conditions.append(Host.primary_ip == proposal.primary_ip)
    existing = (await session.execute(select(Host).where(or_(*conditions)))).scalars().first()
    if existing is not None:
        errors.append(
            f"a host already exists with that name or IP: {existing.hostname} "
            f"({existing.primary_ip or 'no ip'})"
        )
    return errors


async def apply_proposal(session: AsyncSession, proposal: HostProposal) -> Host:
    """Write the confirmed proposal as a Host row. Caller confirms first."""
    capabilities: dict[str, Any] = {}
    if proposal.role:
        capabilities["role"] = proposal.role
    host = Host(
        hostname=proposal.hostname,
        primary_ip=proposal.primary_ip,
        arch=Architecture(proposal.arch.lower()) if proposal.arch else Architecture.OTHER,
        discovery_source=DiscoverySource.MANUAL,
        capabilities=capabilities,
        credentials_ref=(
            f"ssh:{proposal.ssh_user}:{proposal.ssh_key_path}"
            if proposal.ssh_user and proposal.ssh_key_path
            else None
        ),
        notes=proposal.notes,
    )
    session.add(host)
    await session.flush()
    return host


def render_hosts_for_prompt(hosts: list[Host]) -> str:
    if not hosts:
        return "(none yet)"
    return "\n".join(f"  - {h.hostname} ip={h.primary_ip or '?'}" for h in hosts)


__all__ = [
    "DISCOVERY_SYSTEM_TEMPLATE",
    "AgentTurn",
    "HostProposal",
    "apply_proposal",
    "parse_agent_reply",
    "render_hosts_for_prompt",
    "validate_proposal",
]
