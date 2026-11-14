"""Approval channels — a human gesture, on another device, standing in for the CLI prompt.

Phase 7. When ``decide()`` returns CONFIRM, the executor needs one human's
"yes" for this one action. At the CLI that is a prompt. Through the MCP surface
there is no terminal and no human in the loop by default, so the executor
consults an :class:`ApprovalChannel` instead: it delivers the question
somewhere the operator is (first implementation: a Home Assistant actionable
notification with Approve / Deny buttons) and waits for the answer to come
back over HA's websocket event bus.

That bus is writable by anything holding a Home Assistant token, so a button
id alone proves nothing. An event is the operator's answer only when it names
a device on ``HOMELAB_HELPER_APPROVAL_DEVICE_IDS`` and, when
``HOMELAB_HELPER_APPROVAL_USER_IDS`` is set, was fired as one of those Home
Assistant users. The user comes from the event's context, which HA sets from
the credential that fired it; the device id is event data, which a token
holder who has learned it can copy. Everything else on the bus is ignored, and
an approval nobody is named for is not an approval.

What a channel is **not**: it never grants, elevates, overrides, rolls back or
opens a window. It answers exactly the question the CLI prompt would have
asked, for exactly one proposal, and the answer is recorded on the audit spine
with the channel name and the responder. A timeout is a "no".

Nothing here may import ``homelab_helper.llm`` — this module sits on the
executor's path and the mechanical LLM-import tests cover it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import httpx
import websockets

from homelab_helper.secrets import secret_from_env

if TYPE_CHECKING:
    from homelab_helper.engine.executor import ActionManifest
    from homelab_helper.engine.trust import Decision

log = logging.getLogger(__name__)

APPROVE_PREFIX = "HELPER_APPROVE_"
DENY_PREFIX = "HELPER_DENY_"
DEFAULT_TIMEOUT_S = 900
DEVICE_IDS_VAR = "HOMELAB_HELPER_APPROVAL_DEVICE_IDS"
USER_IDS_VAR = "HOMELAB_HELPER_APPROVAL_USER_IDS"
CONTEXT_USER_KEY = "context_user_id"
"""Where the listener puts the event context's ``user_id`` beside the event data."""
APPROVAL_CHANNEL = "homelab-helper approvals"
"""A dedicated Android channel: high importance is fixed when a channel is first
created, so approvals get their own instead of sharing the run-notice channel."""
_HTTP_ERROR_THRESHOLD = 300


class ApprovalError(RuntimeError):
    """The channel could not deliver the question or read the answer."""


class ApprovalConfigError(RuntimeError):
    """Required approval-channel configuration is missing."""


@dataclass(frozen=True)
class ApprovalResult:
    """One human's answer to one proposal, with enough to audit it."""

    approved: bool
    channel: str
    responder: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.approved and not self.responder:
            object.__setattr__(self, "approved", False)
            object.__setattr__(
                self, "detail", {**self.detail, "reason": "approval named no responder"}
            )

    @property
    def summary(self) -> str:
        who = f" by {self.responder}" if self.responder else ""
        return f"{'approved' if self.approved else 'declined'} via {self.channel}{who}"


class ApprovalChannel(Protocol):
    name: str

    async def request(
        self,
        manifest: ActionManifest,
        decision: Decision,
        *,
        proposal_id: str,
        title: str | None = None,
        why: str | None = None,
    ) -> ApprovalResult: ...


Sender = Callable[[dict[str, Any]], Awaitable[None]]
Listener = Callable[[str, str, float], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class HomeAssistantApprovalConfig:
    url: str
    token: str
    notify_service: str
    verify_ssl: bool = True
    timeout_s: int = DEFAULT_TIMEOUT_S
    device_ids: frozenset[str] = frozenset()
    """Mobile-app device ids whose taps count; empty means no tap does."""
    user_ids: frozenset[str] = frozenset()
    """Home Assistant user ids an answer must be fired as; empty skips the check."""

    @classmethod
    def from_env(cls) -> HomeAssistantApprovalConfig:
        url = os.environ.get("HOMELAB_HELPER_HASS_URL", "").strip()
        token = secret_from_env("HOMELAB_HELPER_HASS_TOKEN") or ""
        service = os.environ.get("HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE", "").strip()
        missing = [
            name
            for name, value in (
                ("HOMELAB_HELPER_HASS_URL", url),
                ("HOMELAB_HELPER_HASS_TOKEN", token),
                ("HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE", service),
            )
            if not value
        ]
        if missing:
            raise ApprovalConfigError(
                "approval channel needs " + ", ".join(missing) + " (e.g. notify.mobile_app_<phone>)"
            )
        return cls(
            url=url.rstrip("/"),
            token=token,
            notify_service=service,
            verify_ssl=os.environ.get("HOMELAB_HELPER_HASS_VERIFY_SSL", "true").lower()
            not in {"0", "false", "no"},
            timeout_s=int(os.environ.get("HOMELAB_HELPER_APPROVAL_TIMEOUT_S", DEFAULT_TIMEOUT_S)),
            device_ids=_id_set(DEVICE_IDS_VAR),
            user_ids=_id_set(USER_IDS_VAR),
        )


def _id_set(var: str) -> frozenset[str]:
    return frozenset(v.strip() for v in os.environ.get(var, "").split(",") if v.strip())


class HomeAssistantApprovalChannel:
    """Approve / Deny buttons on the operator's phone, read back over HA's event bus.

    ``sender`` and ``listener`` are injectable so tests never need a Home
    Assistant: the sender posts the notification, the listener returns the
    ``mobile_app_notification_action`` event data whose ``action`` matches
    one of the two button ids (or ``None`` on timeout). Whatever the listener
    returns is held to the allowlists again in :meth:`request`.
    """

    name = "home-assistant"

    def __init__(
        self,
        config: HomeAssistantApprovalConfig,
        *,
        sender: Sender | None = None,
        listener: Listener | None = None,
    ) -> None:
        self.config = config
        self._sender = sender or self._send_notification
        self._listener = listener or self._await_action

    @staticmethod
    def action_ids(proposal_id: str) -> tuple[str, str]:
        return f"{APPROVE_PREFIX}{proposal_id}", f"{DENY_PREFIX}{proposal_id}"

    async def request(
        self,
        manifest: ActionManifest,
        decision: Decision,
        *,
        proposal_id: str,
        title: str | None = None,
        why: str | None = None,
    ) -> ApprovalResult:
        approve_id, deny_id = self.action_ids(proposal_id)
        tag = f"homelab_helper_{proposal_id}"
        await self._sender(
            {
                "title": title or f"Approve: {manifest.action_kind} {manifest.target_label}?",
                "message": approval_message(manifest, why),
                "data": {
                    "tag": tag,
                    "group": "homelab-helper-approvals",
                    "channel": APPROVAL_CHANNEL,
                    "importance": "high",
                    "priority": "high",
                    "ttl": 0,
                    # Stays until answered: an approval swiped away by accident is lost.
                    "sticky": True,
                    # A plain tap would open the dashboard, which is not an answer;
                    # the buttons are the only way to respond.
                    "clickAction": "noAction",
                    "push": {"interruption-level": "time-sensitive"},
                    "actions": [
                        {"action": approve_id, "title": "Approve"},
                        {"action": deny_id, "title": "Deny"},
                    ],
                },
            }
        )
        try:
            event = await self._listener(approve_id, deny_id, float(self.config.timeout_s))
        finally:
            await self._clear(tag)
        if event is None:
            return ApprovalResult(
                approved=False,
                channel=self.name,
                detail={"reason": f"no answer within {self.config.timeout_s}s"},
            )
        refusal = self.refusal(event)
        if refusal is not None:
            return ApprovalResult(approved=False, channel=self.name, detail={"reason": refusal})
        return ApprovalResult(
            approved=event.get("action") == approve_id,
            channel=self.name,
            responder=str(event["device_id"]),
            detail={
                k: v
                for k, v in event.items()
                if k in ("action", "device_id", "device_name", CONTEXT_USER_KEY)
            },
        )

    def refusal(self, event: dict[str, Any]) -> str | None:
        """Why this event is not the operator's answer, or ``None`` when it is."""
        device = event.get("device_id")
        if not device or str(device) not in self.config.device_ids:
            return f"answer from device {device or '(none)'}, which is not in {DEVICE_IDS_VAR}"
        if self.config.user_ids and event.get(CONTEXT_USER_KEY) not in self.config.user_ids:
            return f"answer fired as a Home Assistant user that is not in {USER_IDS_VAR}"
        return None

    async def _clear(self, tag: str) -> None:
        """Remove an answered or expired prompt from the phone; best effort."""
        with contextlib.suppress(ApprovalError, ApprovalConfigError, OSError):
            await self._sender({"message": "clear_notification", "data": {"tag": tag}})

    async def _send_notification(self, payload: dict[str, Any]) -> None:
        domain, _, service = self.config.notify_service.partition(".")
        if domain != "notify" or not service:
            raise ApprovalConfigError(
                f"notify service must look like notify.<name>, not {self.config.notify_service!r}"
            )
        async with httpx.AsyncClient(
            base_url=self.config.url,
            verify=self.config.verify_ssl,
            timeout=15,
            headers={"Authorization": f"Bearer {self.config.token}"},
        ) as client:
            try:
                response = await client.post(f"/api/services/notify/{service}", json=payload)
            except httpx.HTTPError as exc:
                raise ApprovalError(f"could not reach Home Assistant: {exc}") from exc
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            raise ApprovalError(
                f"Home Assistant refused the notification: {response.status_code} "
                f"{response.text[:200]}"
            )

    async def _await_action(
        self, approve_id: str, deny_id: str, timeout_s: float
    ) -> dict[str, Any] | None:
        ws_url = self.config.url.replace("https://", "wss://").replace("http://", "ws://")
        ssl_ctx: ssl.SSLContext | None = None
        if ws_url.startswith("wss://") and not self.config.verify_ssl:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        async def _listen() -> dict[str, Any] | None:
            async with websockets.connect(f"{ws_url}/api/websocket", ssl=ssl_ctx) as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "auth", "access_token": self.config.token}))
                if json.loads(await ws.recv()).get("type") != "auth_ok":
                    raise ApprovalError("Home Assistant websocket authentication failed")
                await ws.send(
                    json.dumps(
                        {
                            "id": 1,
                            "type": "subscribe_events",
                            "event_type": "mobile_app_notification_action",
                        }
                    )
                )
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "event":
                        continue
                    event = msg.get("event") or {}
                    data = event.get("data") or {}
                    if data.get("action") not in (approve_id, deny_id):
                        continue
                    user = (event.get("context") or {}).get("user_id")
                    answer = {**data, CONTEXT_USER_KEY: user}
                    refusal = self.refusal(answer)
                    if refusal is None:
                        return answer
                    # Not the operator: keep waiting, so a forged event cannot end the ask.
                    log.warning("ignored %s: %s", data.get("action"), refusal)
            return None

        try:
            return await asyncio.wait_for(_listen(), timeout=timeout_s)
        except TimeoutError:
            return None
        except (OSError, websockets.WebSocketException) as exc:
            raise ApprovalError(f"Home Assistant websocket failed: {exc}") from exc


_NEXT_START_KINDS = {"cpu-type", "resize"}
_WHY_LIMIT = 200


def approval_message(manifest: ActionManifest, why: str | None) -> str:
    """One readable line: why, when it takes effect, and where it sits in policy."""
    parts = []
    if why:
        parts.append(why.strip().rstrip(".")[:_WHY_LIMIT] + ".")
    if manifest.action_kind in _NEXT_START_KINDS and manifest.vm_kind == "qemu":
        parts.append("Takes effect at the VM's next restart.")
    elif manifest.action_kind == "resize":
        parts.append("Applies immediately.")
    parts.append(f"({manifest.target_label}; {manifest.domain.value}/{manifest.action_kind})")
    # Android shows the buttons only on an expanded notification.
    parts.append("Expand for Approve / Deny.")
    return " ".join(parts)


def approval_channel_from_env() -> HomeAssistantApprovalChannel:
    """The configured channel, or :class:`ApprovalConfigError` naming what is missing."""
    config = HomeAssistantApprovalConfig.from_env()
    if not config.device_ids:
        raise ApprovalConfigError(
            f"approval channel needs {DEVICE_IDS_VAR}: the mobile-app device id(s) "
            "whose Approve counts (see docs/trust-gradient.md, 'Asking on your phone')"
        )
    return HomeAssistantApprovalChannel(config)


__all__ = [
    "APPROVAL_CHANNEL",
    "APPROVE_PREFIX",
    "DEFAULT_TIMEOUT_S",
    "DENY_PREFIX",
    "DEVICE_IDS_VAR",
    "USER_IDS_VAR",
    "ApprovalChannel",
    "ApprovalConfigError",
    "ApprovalError",
    "ApprovalResult",
    "HomeAssistantApprovalChannel",
    "HomeAssistantApprovalConfig",
    "approval_channel_from_env",
    "approval_message",
]
