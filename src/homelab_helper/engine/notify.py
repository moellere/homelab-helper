"""After-the-fact notification for executed actions (Phase 7 slice 4).

The approval channel asks *before* a CONFIRM action; this tells the operator
*after* an action they were not asked about. Policy for when a run is worth a
notification lives in :func:`should_notify` and is deliberately small:

- the decision was ``AUTONOMOUS`` — nobody tapped anything, so the run must
  announce itself (roadmap P7-AC4);
- the dispatch failed, at any level — a tap that led to a failure deserves a
  follow-up, and a failed autonomous run doubly so;
- the outcome moved a cell's floor (promotion, demotion, probation) — the
  gradient changed shape and the operator should know without reading
  ``helper trust history``.

A CONFIRM run that succeeded is not notified: the operator just approved it.

The notifier is best-effort and runs *after* the receipt is written: a phone
that cannot be reached never changes what happened or whether it is recorded.
:class:`HomeAssistantNotifier` reuses the approval channel's configuration
(same HA instance, token and ``notify.<phone>`` service) so one set of
environment variables covers both directions; the notification is plain —
no action buttons — and carries the rollback one-liner when the receipt
holds enough state to undo the action.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from homelab_helper.db.enums import AutonomyLevel
from homelab_helper.engine.approval import (
    ApprovalConfigError,
    HomeAssistantApprovalConfig,
)

if TYPE_CHECKING:
    import uuid

    from homelab_helper.engine.escalation import EscalationResult

log = logging.getLogger(__name__)

_HTTP_ERROR_THRESHOLD = 400
_FLOOR_EVENTS = {"auto-promote", "demote"}


@dataclass(frozen=True)
class ExecutionNotice:
    """Everything the operator needs to read one executed action off a phone."""

    receipt_id: uuid.UUID
    proposal_id: uuid.UUID
    title: str
    cell: str
    target: str
    level: AutonomyLevel
    outcome: str
    error: str | None
    duration_ms: int
    actor: str
    rollback_available: bool
    escalation: EscalationResult | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome == "succeeded"


def should_notify(notice: ExecutionNotice) -> bool:
    if notice.level is AutonomyLevel.AUTONOMOUS:
        return True
    if not notice.succeeded:
        return True
    return notice.escalation is not None and notice.escalation.event in _FLOOR_EVENTS


class Notifier(Protocol):
    name: str

    async def send(self, notice: ExecutionNotice) -> None: ...


def render(notice: ExecutionNotice) -> tuple[str, str]:
    """``(title, message)`` for a notification, shared by every notifier."""
    mark = "✓" if notice.succeeded else "✗"
    how = "unattended" if notice.level is AutonomyLevel.AUTONOMOUS else notice.level.value
    title = f"{mark} homelab-helper: {notice.title}"
    lines = [
        f"{notice.cell} → {notice.target} ran {how} by {notice.actor} "
        f"({notice.duration_ms} ms): {notice.outcome}."
    ]
    if notice.error:
        lines.append(f"Error: {notice.error[:300]}")
    if notice.escalation is not None and notice.escalation.event in _FLOOR_EVENTS:
        e = notice.escalation
        lines.append(
            f"Cell {e.event}: {e.previous_level.value} → {e.level.value}"
            + (" (on probation)" if e.on_probation else "")
            + "."
        )
    short = str(notice.receipt_id)[:8]
    if notice.rollback_available and notice.succeeded:
        lines.append(f"Undo: helper exec rollback {short}")
    lines.append(f"Receipt {short} · helper exec receipts")
    return title, "\n".join(lines)


class HomeAssistantNotifier:
    """A plain mobile_app notification through the approval channel's service."""

    name = "home-assistant"

    def __init__(self, config: HomeAssistantApprovalConfig) -> None:
        self.config = config

    async def send(self, notice: ExecutionNotice) -> None:
        domain, _, service = self.config.notify_service.partition(".")
        if domain != "notify" or not service:
            raise ApprovalConfigError(
                f"notify service must look like notify.<name>, not {self.config.notify_service!r}"
            )
        title, message = render(notice)
        payload: dict[str, Any] = {
            "title": title,
            "message": message,
            "data": {
                "tag": f"helper-run-{notice.receipt_id}",
                "group": "homelab-helper",
                "channel": "homelab-helper runs",
                "notification_icon": "mdi:robot-outline" if notice.succeeded else "mdi:robot-dead",
                "clickAction": "noAction",
            },
        }
        async with httpx.AsyncClient(
            base_url=self.config.url,
            verify=self.config.verify_ssl,
            timeout=15,
            headers={"Authorization": f"Bearer {self.config.token}"},
        ) as client:
            response = await client.post(f"/api/services/notify/{service}", json=payload)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            raise RuntimeError(
                f"Home Assistant refused the notification: {response.status_code} "
                f"{response.text[:200]}"
            )


async def send_digest(config: HomeAssistantApprovalConfig, title: str, message: str) -> None:
    """One plain notification carrying a digest. Same channel as a run notice,
    its own tag so a digest never replaces a run's notification."""
    domain, _, service = config.notify_service.partition(".")
    if domain != "notify" or not service:
        raise ApprovalConfigError(
            f"notify service must look like notify.<name>, not {config.notify_service!r}"
        )
    payload: dict[str, Any] = {
        "title": title,
        "message": message,
        "data": {
            "tag": "helper-digest",
            "group": "homelab-helper",
            "channel": "homelab-helper digest",
            "notification_icon": "mdi:calendar-text",
            "clickAction": "noAction",
        },
    }
    async with httpx.AsyncClient(
        base_url=config.url,
        verify=config.verify_ssl,
        timeout=15,
        headers={"Authorization": f"Bearer {config.token}"},
    ) as client:
        response = await client.post(f"/api/services/notify/{service}", json=payload)
    if response.status_code >= _HTTP_ERROR_THRESHOLD:
        raise RuntimeError(
            f"Home Assistant refused the digest: {response.status_code} {response.text[:200]}"
        )


def notifier_from_env() -> HomeAssistantNotifier | None:
    """The configured notifier, or ``None`` when the HA approval variables are unset."""
    try:
        return HomeAssistantNotifier(HomeAssistantApprovalConfig.from_env())
    except ApprovalConfigError:
        return None


async def notify_after_run(notifier: Notifier | None, notice: ExecutionNotice) -> str | None:
    """Send if warranted; never raise. Returns what happened, ``None`` when not warranted."""
    if not should_notify(notice):
        return None
    if notifier is None:
        return "unconfigured"
    try:
        await notifier.send(notice)
    except Exception as exc:  # a lost notification must not undo a receipt
        log.warning("post-run notification via %s failed: %s", notifier.name, exc)
        return f"failed: {exc}"
    return f"sent via {notifier.name}"


__all__ = [
    "ExecutionNotice",
    "HomeAssistantNotifier",
    "Notifier",
    "notifier_from_env",
    "notify_after_run",
    "render",
    "send_digest",
    "should_notify",
]
