"""Shared vocabulary for source services."""

from dataclasses import dataclass, field

MAX_SOURCES = 20
DEFAULT_TIMEZONE = "Europe/Amsterdam"
DEFAULT_POLL_SECONDS = 900
CREATABLE_KINDS = frozenset({"private", "webhook", "ics"})
STATUS_LABELS = {
    "idle": "not yet started",
    "running": "busy",
    "ok": "succeeded",
    "error": "error",
    "na": "not applicable",
}


@dataclass(frozen=True)
class KindInfo:
    color: str
    color_name: str
    label: str


KINDS: dict[str, KindInfo] = {
    "private": KindInfo("#1a73e8", "blue", "Private"),
    "webhook": KindInfo("#9334e6", "purple", "Webhook"),
    "ics": KindInfo("#f9ab00", "yellow", "External ICS"),
}


@dataclass
class SourceFailure:
    status: int
    error: str
    detail: str
    fields: dict[str, str] = field(default_factory=dict)


class WebhookPayloadError(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail


class WebhookApplyError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class WebhookSecretError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class ConfigError(Exception):
    pass


class _LimitReached(Exception):
    pass


class _Missing(Exception):
    pass


def _failure(
    status: int,
    detail: str,
    fields: dict[str, str] | None = None,
    *,
    error: str | None = None,
) -> SourceFailure:
    found = fields or {}
    if error is not None:
        code = error
    elif status == 404:
        code = "not_found"
    elif status >= 500:
        code = "internal_error"
    else:
        code = "invalid_request"
    return SourceFailure(status=status, error=code, detail=detail, fields=found)


def _field(name: str, message: str) -> SourceFailure:
    return _failure(422, message, {name: message})
