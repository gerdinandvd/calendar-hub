"""Agent HTTP API v1."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.access import json_error
from app.api.deps import require_agent
from app.services.agent import (
    AgentError,
    create_event,
    delete_event_flow,
    get_event,
    list_agent_calendars,
    list_or_search_events,
    update_event,
)
from app.services.agent.auth import VerifiedToken
from app.services.agent.idempotency import IdempotencyStore
from app.services.agent.types import MAX_BODY

router = APIRouter(prefix="/api/agent/v1")


@router.get("/calendars", response_model=None)
def calendars(request: Request) -> dict | JSONResponse:
    auth = require_agent(request)
    if isinstance(auth, JSONResponse):
        return auth
    return {"calendars": list_agent_calendars(request.app.state.engine)}


@router.get("/events", response_model=None)
def events(
    request: Request,
    q: str | None = None,
    to: str | None = None,
    calendars: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    fields: str | None = None,
) -> dict | JSONResponse:
    auth = require_agent(request)
    if isinstance(auth, JSONResponse):
        return auth
    calendar_ids = (
        [part for part in calendars.split(",") if part.strip()] if calendars else None
    )
    # HTTP list shape: optional in-window q; week default. MCP search uses mode=search.
    window_from = request.query_params.get("from")
    result = list_or_search_events(
        request.app.state.engine,
        request.app.state.radicale,
        mode="list",
        q=q,
        window_from=window_from,
        window_to=to,
        calendars=calendar_ids,
        limit=limit,
        cursor=cursor,
        fields=fields,
    )
    return _result(result)


@router.get("/events/{event_id}", response_model=None)
def read_event(
    request: Request, event_id: str, fields: str | None = None
) -> dict | JSONResponse:
    auth = require_agent(request)
    if isinstance(auth, JSONResponse):
        return auth
    return _result(
        get_event(
            request.app.state.engine,
            request.app.state.radicale,
            event_id,
            fields=fields,
        )
    )


@router.post("/events", response_model=None)
async def post_event(request: Request) -> dict | JSONResponse:
    auth = require_agent(request, write=True)
    if isinstance(auth, JSONResponse):
        return auth
    assert isinstance(auth, VerifiedToken)
    payload = await _json_body(request)
    if isinstance(payload, JSONResponse):
        return payload
    idem_key = (request.headers.get("idempotency-key") or "").strip()
    store: IdempotencyStore = request.app.state.agent_idempotency
    if idem_key:
        existing = store.lookup(auth.id, idem_key)
        if existing is not None:
            return _result(
                get_event(
                    request.app.state.engine,
                    request.app.state.radicale,
                    existing,
                    fields="compact",
                )
            )
    result = create_event(request.app.state.engine, request.app.state.radicale, payload)
    if idem_key and isinstance(result, dict) and isinstance(result.get("id"), str):
        store.remember(auth.id, idem_key, result["id"])
    return _result(result)


@router.patch("/events/{event_id}", response_model=None)
async def patch_event(request: Request, event_id: str) -> dict | JSONResponse:
    auth = require_agent(request, write=True)
    if isinstance(auth, JSONResponse):
        return auth
    payload = await _json_body(request)
    if isinstance(payload, JSONResponse):
        return payload
    return _result(
        update_event(
            request.app.state.engine, request.app.state.radicale, event_id, payload
        )
    )


@router.delete("/events/{event_id}", response_model=None)
def remove_event(
    request: Request, event_id: str, confirm_token: str | None = None
) -> dict | JSONResponse:
    auth = require_agent(request, write=True)
    if isinstance(auth, JSONResponse):
        return auth
    return _result(
        delete_event_flow(
            request.app.state.engine,
            request.app.state.radicale,
            request.app.state.agent_confirms,
            event_id,
            confirm_token,
        )
    )


def _result(value: dict | AgentError) -> dict | JSONResponse:
    if isinstance(value, AgentError):
        return json_error(value.status, value.error, value.detail)
    return value


async def _json_body(request: Request) -> object | JSONResponse:
    header = request.headers.get("content-length")
    if header is not None:
        try:
            if int(header) > MAX_BODY:
                return json_error(413, "payload_too_large", "Body is too large.")
        except ValueError:
            return json_error(400, "bad_request", "The request is invalid.")
    body = await request.body()
    if len(body) > MAX_BODY:
        return json_error(413, "payload_too_large", "Body is too large.")
    import json

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return json_error(422, "invalid_request", "The input is invalid.")
