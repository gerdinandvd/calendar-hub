"""Agent HTTP/MCP domain facade."""

from app.services.agent.auth import (
    authenticate_agent,
    bind_current_token,
    create_token,
    current_token,
    list_tokens,
    require_current_write,
    require_scope,
    reset_current_token,
    revoke_token,
    verify_bearer,
)
from app.services.agent.calendars import list_agent_calendars
from app.services.agent.confirm import ConfirmStore
from app.services.agent.events import (
    create_event,
    delete_event_flow,
    get_event,
    list_or_search_events,
    update_event,
)
from app.services.agent.idempotency import IdempotencyStore
from app.services.agent.types import SCOPE_READ, SCOPE_WRITE, AgentError

__all__ = [
    "AgentError",
    "ConfirmStore",
    "IdempotencyStore",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "authenticate_agent",
    "bind_current_token",
    "create_event",
    "create_token",
    "current_token",
    "delete_event_flow",
    "get_event",
    "list_agent_calendars",
    "list_or_search_events",
    "list_tokens",
    "require_current_write",
    "require_scope",
    "reset_current_token",
    "revoke_token",
    "update_event",
    "verify_bearer",
]
