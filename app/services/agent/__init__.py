"""Agent HTTP/MCP domain facade."""

from app.services.agent.auth import (
    create_token,
    list_tokens,
    require_scope,
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
from app.services.agent.types import SCOPE_READ, SCOPE_WRITE, AgentError

__all__ = [
    "AgentError",
    "ConfirmStore",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "create_event",
    "create_token",
    "delete_event_flow",
    "get_event",
    "list_agent_calendars",
    "list_or_search_events",
    "list_tokens",
    "require_scope",
    "revoke_token",
    "update_event",
    "verify_bearer",
]
