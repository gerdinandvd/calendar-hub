"""Typed domain errors. No HTTP status codes and no i18n catalog keys."""


class DomainError(Exception):
    """Base for all domain failures."""


class NotFoundError(DomainError):
    """The requested entity does not exist."""


class ValidationError(DomainError):
    """Input failed a domain rule."""


class ConflictError(DomainError):
    """The change conflicts with current state."""


class CapacityError(DomainError):
    """A capacity limit would be exceeded."""


class ForbiddenError(DomainError):
    """The actor lacks permission for this action."""


class UnauthorizedError(DomainError):
    """Authentication is missing or invalid."""


class RateLimitedError(DomainError):
    """Too many requests for this actor or key."""


class CalendarParseError(DomainError):
    """The whole calendar document was rejected. Nothing was written."""


class TimeValueError(DomainError, ValueError):
    """Invalid calendar date, clock, status, or recurrence field."""


class FeedFetchError(DomainError):
    """An ICS feed could not be fetched or read."""


class CalendarStoreError(DomainError):
    """The calendar store could not complete a change."""


class SyncBusyError(DomainError):
    """Another sync holds this source and the caller chose not to wait."""


class SyncStoppedError(DomainError):
    """The process is stopping; the sync did not finish."""


class SecretKeyError(DomainError):
    """HUB_SECRET_KEY is missing or not a 32-byte url-safe base64 key."""
