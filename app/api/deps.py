"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from app.access import json_error
from app.services.agent import (
    SCOPE_READ,
    SCOPE_WRITE,
    AgentError,
    require_scope,
    verify_bearer,
)
from app.services.agent.auth import VerifiedToken
from app.services.rate_limit import RateLimiter


def require_agent(
    request: Request, *, write: bool = False
) -> VerifiedToken | JSONResponse:
    limiter: RateLimiter = request.app.state.agent_rates
    verified = verify_bearer(
        request.app.state.engine, request.headers.get("authorization")
    )
    if isinstance(verified, AgentError):
        return json_error(verified.status, verified.error, verified.detail)
    if not limiter.allow(verified.id):
        return json_error(429, "rate_limited", "Too many requests for this token.")
    if write:
        missing = require_scope(verified, SCOPE_WRITE)
    elif SCOPE_READ in verified.scopes or SCOPE_WRITE in verified.scopes:
        missing = None
    else:
        missing = require_scope(verified, SCOPE_READ)
    if missing is not None:
        return json_error(missing.status, missing.error, missing.detail)
    return verified
