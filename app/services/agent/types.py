"""Shared agent API constants and errors."""

from dataclasses import dataclass

SCOPE_READ = "calendars:read"
SCOPE_WRITE = "calendars:write"
VALID_SCOPES = frozenset({SCOPE_READ, SCOPE_WRITE})
WRITABLE_KINDS = frozenset({"private", "webhook"})

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_WINDOW_DAYS = 366
CONFIRM_TTL_SECONDS = 120
MAX_BODY = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_LIST_DAYS = 7
DESCRIPTION_COMPACT_LEN = 280
SUMMARY_MAX_LEN = 512
TEXT_MAX_LEN = 8192

FIELD_COMPACT = "compact"
FIELD_STANDARD = "standard"
FIELD_FULL = "full"
VALID_FIELDS = frozenset({FIELD_COMPACT, FIELD_STANDARD, FIELD_FULL})


@dataclass(frozen=True)
class AgentError:
    status: int
    error: str
    detail: str


def unauthorized(detail: str = "Authentication required.") -> AgentError:
    return AgentError(401, "unauthorized", detail)


def forbidden(detail: str = "Missing required scope.") -> AgentError:
    return AgentError(403, "forbidden", detail)


def not_found(detail: str = "Not found.") -> AgentError:
    return AgentError(404, "not_found", detail)


def invalid(detail: str) -> AgentError:
    return AgentError(422, "invalid_request", detail)


def rate_limited() -> AgentError:
    return AgentError(429, "rate_limited", "Too many requests for this token.")


def internal(detail: str = "Something went wrong.") -> AgentError:
    return AgentError(500, "internal_error", detail)
