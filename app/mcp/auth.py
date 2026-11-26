"""ASGI wrapper that requires a valid agent Bearer PAT before MCP."""

from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.agent import SCOPE_READ, SCOPE_WRITE, AgentError, verify_bearer
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
        verified = verify_bearer(self.engine, headers.get("authorization"))
        if isinstance(verified, AgentError):
            await _json(send, verified.status, verified.error, verified.detail)
            return
        if SCOPE_READ not in verified.scopes and SCOPE_WRITE not in verified.scopes:
            await _json(send, 403, "forbidden", "Missing required scope.")
            return
        if not self.rates.allow(verified.id):
            await _json(send, 429, "rate_limited", "Too many requests for this token.")
            return
        scope.setdefault("state", {})
        scope["state"]["agent_token"] = verified
        await self.app(scope, receive, send)


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
