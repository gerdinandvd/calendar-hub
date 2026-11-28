"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from app.access import json_error
from app.services.agent.auth import (
    VerifiedToken,
    authenticate_agent,
    bind_current_token,
)
from app.services.agent.types import AgentError
from app.services.rate_limit import RateLimiter


def require_agent(
    request: Request, *, write: bool = False
) -> VerifiedToken | JSONResponse:
    limiter: RateLimiter = request.app.state.agent_rates
    verified = authenticate_agent(
        request.app.state.engine,
        request.headers.get("authorization"),
        limiter,
        write=write,
    )
    if isinstance(verified, AgentError):
        return json_error(verified.status, verified.error, verified.detail)
    bind_current_token(verified)
    return verified
