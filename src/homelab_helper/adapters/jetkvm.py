"""JetKVM adapter — read-only KVM-over-IP source.

A JetKVM sits on one machine's HDMI and USB and serves that machine's console over WebRTC. This adapter reads two
things from it and writes nothing:

- ``status()`` / ``device()``: the open ``GET /device/status`` (is the device set up) and, after a local-password
  login, ``GET /device`` (auth mode, device id).
- ``console_snapshot()``: one frame of the attached machine's screen as PNG, taken by joining the device's WebRTC
  session (``/webrtc/signaling/client``) as a receive-only viewer. This needs the optional ``jetkvm`` extra
  (``aiortc`` + ``pillow``); without it the call raises ``JetKVMSnapshotUnavailable`` and nothing else changes.

There is deliberately no keyboard, mouse, power or virtual-media method. Console input can run anything on the
attached machine, which is unbounded remote code execution; it does not fit the executor's node actions, whose
manifests carry a node name and nothing else.

Configuration: ``HOMELAB_HELPER_JETKVM_URL``, ``HOMELAB_HELPER_JETKVM_PASSWORD`` (a secret reference; omit it for a
device in no-password mode) and ``HOMELAB_HELPER_JETKVM_VERIFY_SSL``. The password is exchanged for the session
cookie ``POST /auth/login-local`` sets. Tests inject an ``httpx.AsyncClient`` on a ``MockTransport`` and a fake
frame grabber in place of WebRTC.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import ssl
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx

from homelab_helper.secrets import secret_from_env

AUTH_COOKIE = "authToken"
SIGNALING_PATH = "/webrtc/signaling/client"
REFUSED = (httpx.codes.BAD_REQUEST, httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN)

FrameGrabber = Callable[[str, dict[str, str], bool, float], Awaitable[bytes]]
"""``(signaling_url, headers, verify_ssl, timeout) -> PNG bytes``."""


class JetKVMConfigError(RuntimeError):
    """Raised when required configuration is missing."""


class JetKVMAPIError(RuntimeError):
    """Raised when the device refuses a read or answers with something unexpected."""


class JetKVMSnapshotUnavailable(RuntimeError):
    """Raised when a console snapshot cannot be taken (extra missing, no video, timeout)."""


class JetKVMAdapter:
    def __init__(
        self,
        url: str,
        *,
        password: str | None = None,
        verify_ssl: bool = True,
        client: httpx.AsyncClient | None = None,
        frame_grabber: FrameGrabber | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.verify_ssl = verify_ssl
        self._password = password
        self._logged_in = False
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.url, verify=verify_ssl, timeout=10.0
        )
        self._grab = frame_grabber or grab_frame_webrtc

    @classmethod
    def from_env(cls) -> JetKVMAdapter:
        url = os.environ.get("HOMELAB_HELPER_JETKVM_URL")
        if not url:
            raise JetKVMConfigError("HOMELAB_HELPER_JETKVM_URL is required")
        verify = os.environ.get("HOMELAB_HELPER_JETKVM_VERIFY_SSL", "true").lower() != "false"
        return cls(
            url, password=secret_from_env("HOMELAB_HELPER_JETKVM_PASSWORD"), verify_ssl=verify
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def health_check(self) -> tuple[bool, str | None]:
        try:
            await self.status()
        except (httpx.HTTPError, JetKVMAPIError) as exc:
            return False, str(exc)
        return True, None

    async def status(self) -> dict[str, Any]:
        """``GET /device/status``: open, says whether the device has been set up."""
        resp = await self._client.get("/device/status")
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise JetKVMAPIError("JetKVM /device/status did not return an object")
        return cast("dict[str, Any]", body)

    async def _login(self) -> None:
        if self._logged_in or not self._password:
            return
        resp = await self._client.post("/auth/login-local", json={"password": self._password})
        if resp.status_code in REFUSED:
            raise JetKVMAPIError(
                "JetKVM refused the local password (HOMELAB_HELPER_JETKVM_PASSWORD)"
            )
        resp.raise_for_status()
        self._logged_in = True

    async def device(self) -> dict[str, Any]:
        """``GET /device`` after login: auth mode and device id."""
        await self._login()
        resp = await self._client.get("/device")
        if resp.status_code == httpx.codes.UNAUTHORIZED:
            raise JetKVMAPIError("JetKVM /device needs a login; set HOMELAB_HELPER_JETKVM_PASSWORD")
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise JetKVMAPIError("JetKVM /device did not return an object")
        return cast("dict[str, Any]", body)

    def signaling_url(self) -> str:
        scheme, _, rest = self.url.partition("://")
        return f"{'wss' if scheme == 'https' else 'ws'}://{rest}{SIGNALING_PATH}"

    async def console_snapshot(self, *, timeout: float = 30.0) -> bytes:  # noqa: ASYNC109
        """One PNG frame of the attached machine's console, read as a receive-only WebRTC viewer."""
        await self._login()
        headers: dict[str, str] = {}
        if token := self._client.cookies.get(AUTH_COOKIE):
            headers["Cookie"] = f"{AUTH_COOKIE}={token}"
        return await self._grab(self.signaling_url(), headers, self.verify_ssl, timeout)


def _ssl_context(url: str, verify_ssl: bool) -> ssl.SSLContext | None:
    if not url.startswith("wss://"):
        return None
    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _signaling(ws: Any, pc: Any, session_description: Any, candidate_from_sdp: Any) -> None:
    """Apply the device's answer and trickled ICE candidates to the peer connection."""
    async for raw in ws:
        msg = json.loads(raw)
        if msg.get("type") == "answer":
            answer = json.loads(base64.b64decode(msg["data"]))
            await pc.setRemoteDescription(session_description(answer["sdp"], answer["type"]))
        elif msg.get("type") == "new-ice-candidate" and (c := msg.get("data")):
            if c.get("candidate"):
                cand = candidate_from_sdp(c["candidate"].split(":", 1)[1])
                cand.sdpMid = c.get("sdpMid")
                cand.sdpMLineIndex = c.get("sdpMLineIndex")
                await pc.addIceCandidate(cand)


async def grab_frame_webrtc(
    url: str,
    headers: dict[str, str],
    verify_ssl: bool,
    timeout: float,  # noqa: ASYNC109
    *,
    skip_frames: int = 8,
) -> bytes:
    """Join the device's WebRTC session receive-only and return one decoded frame as PNG."""
    try:
        from aiortc import (  # noqa: PLC0415 — optional dependency
            RTCPeerConnection,
            RTCSessionDescription,
        )
        from aiortc.sdp import candidate_from_sdp  # noqa: PLC0415
    except ImportError as exc:
        raise JetKVMSnapshotUnavailable(
            "console snapshots need the optional dependency: "
            "uv tool install 'homelab-helper[jetkvm]'"
        ) from exc
    import websockets  # noqa: PLC0415

    pc = RTCPeerConnection()
    pc.createDataChannel("rpc")
    pc.addTransceiver("video", direction="recvonly")
    frame_ready: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    pulls: list[asyncio.Task[None]] = []

    @pc.on("track")
    def _on_track(track: Any) -> None:
        async def pull() -> None:
            for n in range(1, skip_frames + 1):
                frame = await track.recv()
                if n == skip_frames and not frame_ready.done():
                    frame_ready.set_result(frame)

        pulls.append(asyncio.ensure_future(pull()))

    await pc.setLocalDescription(await pc.createOffer())
    sslctx = _ssl_context(url, verify_ssl)
    try:
        async with websockets.connect(url, additional_headers=headers, ssl=sslctx) as ws:
            offer = {"type": "offer", "sdp": pc.localDescription.sdp}
            sd = base64.b64encode(json.dumps(offer).encode()).decode()
            await ws.send(json.dumps({"type": "offer", "data": {"sd": sd}}))
            reader = asyncio.ensure_future(
                _signaling(ws, pc, RTCSessionDescription, candidate_from_sdp)
            )
            try:
                frame = await asyncio.wait_for(frame_ready, timeout)
            except TimeoutError as exc:
                raise JetKVMSnapshotUnavailable(
                    f"no video frame from the JetKVM within {timeout:.0f}s"
                ) from exc
            finally:
                reader.cancel()
    finally:
        for task in pulls:
            task.cancel()
        await pc.close()
    buf = io.BytesIO()
    frame.to_image().save(buf, format="PNG")
    return buf.getvalue()


__all__ = [
    "FrameGrabber",
    "JetKVMAPIError",
    "JetKVMAdapter",
    "JetKVMConfigError",
    "JetKVMSnapshotUnavailable",
    "grab_frame_webrtc",
]
