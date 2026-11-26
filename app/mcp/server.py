"""In-process MCP surface over agent services."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from app.mcp.runtime import require
from app.services.agent import (
    AgentError,
    create_event,
    delete_event_flow,
    get_event,
    list_agent_calendars,
    list_or_search_events,
    update_event,
)

mcp = MCPServer(
    name="calendar-hub",
    instructions="Personal calendar hub agent tools. Use hub opaque event ids.",
)


def _unwrap(value: dict | AgentError) -> dict:
    if isinstance(value, AgentError):
        return {"error": value.error, "detail": value.detail, "status": value.status}
    return value


@mcp.tool(name="list_calendars", annotations=ToolAnnotations(read_only_hint=True))
def list_calendars() -> dict[str, Any]:
    engine, _radicale, _confirms = require()
    return {"calendars": list_agent_calendars(engine)}


@mcp.tool(name="list_events", annotations=ToolAnnotations(read_only_hint=True))
def list_events(
    q: str | None = None,
    window_from: str | None = None,
    window_to: str | None = None,
    calendars: list[str] | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    fields: str | None = None,
) -> dict[str, Any]:
    engine, radicale, _confirms = require()
    return _unwrap(
        list_or_search_events(
            engine,
            radicale,
            mode="list",
            q=q,
            window_from=window_from,
            window_to=window_to,
            calendars=calendars,
            limit=limit,
            cursor=cursor,
            fields=fields,
        )
    )


@mcp.tool(name="search_events", annotations=ToolAnnotations(read_only_hint=True))
def search_events(
    q: str,
    window_from: str | None = None,
    window_to: str | None = None,
    calendars: list[str] | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    fields: str | None = None,
) -> dict[str, Any]:
    engine, radicale, _confirms = require()
    return _unwrap(
        list_or_search_events(
            engine,
            radicale,
            mode="search",
            q=q,
            window_from=window_from,
            window_to=window_to,
            calendars=calendars,
            limit=limit,
            cursor=cursor,
            fields=fields,
        )
    )


@mcp.tool(name="get_event", annotations=ToolAnnotations(read_only_hint=True))
def get_event_tool(event_id: str, fields: str | None = None) -> dict[str, Any]:
    engine, radicale, _confirms = require()
    return _unwrap(get_event(engine, radicale, event_id, fields=fields))


@mcp.tool(name="create_event")
def create_event_tool(payload: dict[str, Any]) -> dict[str, Any]:
    engine, radicale, _confirms = require()
    return _unwrap(create_event(engine, radicale, payload))


@mcp.tool(name="update_event")
def update_event_tool(event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    engine, radicale, _confirms = require()
    return _unwrap(update_event(engine, radicale, event_id, payload))


@mcp.tool(
    name="delete_event",
    annotations=ToolAnnotations(destructive_hint=True),
)
def delete_event_tool(
    event_id: str, confirm_token: str | None = None
) -> dict[str, Any]:
    engine, radicale, confirms = require()
    return _unwrap(
        delete_event_flow(engine, radicale, confirms, event_id, confirm_token)
    )


def build_mcp_app() -> Any:
    return mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
