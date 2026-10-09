"""HTTP status endpoint — ``engine.status.snapshot`` over plain GET (Phase 9.7a).

Two routes and nothing else: ``GET /status`` returns the snapshot, ``GET
/healthz`` says the process is up. There is no route that reads a secret,
changes a finding, or touches the trust gradient, and ``tests/test_status.py``
holds the app to GET-only. Meant to sit on a LAN behind a dashboard widget
(Homepage ``customapi``, a Home Assistant ``rest`` sensor); set
``HOMELAB_HELPER_STATUS_TOKEN`` to require ``Authorization: Bearer <token>``
(or ``X-API-Token``) on ``/status`` when the LAN is not the boundary.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from homelab_helper.config import database_url
from homelab_helper.db.session import make_engine, make_sessionmaker
from homelab_helper.engine.status import snapshot
from homelab_helper.secrets import secret_from_env

TOKEN_VAR = "HOMELAB_HELPER_STATUS_TOKEN"


def _presented(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-token")


def build_app(*, token: str | None = None, url: str | None = None) -> FastAPI:
    """The ASGI app. ``token`` and ``url`` default to the environment at call time."""
    required = token if token is not None else secret_from_env(TOKEN_VAR)
    db_url = url or database_url()
    app = FastAPI(title="homelab-helper status", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        if required:
            given = _presented(request) or ""
            if not hmac.compare_digest(given.encode(), required.encode()):
                raise HTTPException(status_code=401, detail="token required")
        engine = make_engine(db_url)
        try:
            async with make_sessionmaker(engine)() as session:
                return await snapshot(session)
        finally:
            await engine.dispose()

    return app


def serve(host: str, port: int) -> None:
    """Run the endpoint under uvicorn (blocking)."""
    import uvicorn  # noqa: PLC0415 - the server stack only loads when serving

    uvicorn.run(build_app(), host=host, port=port, log_level="info")


__all__ = ["TOKEN_VAR", "build_app", "serve"]
