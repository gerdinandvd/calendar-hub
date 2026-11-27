"""ASGI wrapper that requires a valid agent Bearer PAT before MCP."""

from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.agent.auth import (
    authenticate_agent,
    bind_current_token,
    reset_current_token,
)
from app.services.agent.types import AgentError
from app.services.rate_limit import RateLimiter


class AgentBearerMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        engine: Any,
        rates: RateLimiter,
    ) -> None:
        self.app = app
        self.engine = engine
        self.rates = rates

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope.get("headers", [])
        }
        verified = authenticate_agent(
            self.engine, headers.get("authorization"), self.rates, write=False
        )
        if isinstance(verified, AgentError):
            await _json(send, verified.status, verified.error, verified.detail)
            return
        scope.setdefault("state", {})
        scope["state"]["agent_token"] = verified
        handle = bind_current_token(verified)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_token(handle)


async def _json(send: Send, status: int, error: str, detail: str) -> None:
    body = json.dumps({"error": error, "detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
