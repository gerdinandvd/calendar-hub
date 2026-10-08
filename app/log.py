"""Logregels voor mutaties. Actor en source_id zijn verplicht; geheimen gaan eruit."""

import logging
import re

ACTORS = frozenset({"owner", "scheduler", "webhook"})

_VALUE = r"(?:\"[^\"]*\"|'[^']*'|\S+)"
_AUTHORIZATION = re.compile(
    rf"(?i)\bauthorization\b\s*[:=]\s*(?:basic\s+)?{_VALUE}"
)
_ASSIGNED = re.compile(
    rf"(?i)\b(?:password|token|secret|webhook_secret|refresh_token|"
    rf"access_token|summary|description|location|title|titel)\b\s*[:=]\s*{_VALUE}"
)
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_BASIC = re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]+")

_configured = False


def redact(text: str) -> str:
    cleaned = _AUTHORIZATION.sub("[geredigeerd]", text)
    cleaned = _ASSIGNED.sub("[geredigeerd]", cleaned)
    cleaned = _BEARER.sub("[geredigeerd]", cleaned)
    cleaned = _BASIC.sub("[geredigeerd]", cleaned)
    return cleaned


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        record.msg = redact(rendered)
        record.args = None
        record.message = record.msg
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    logger = logging.getLogger("app")
    logger.setLevel(logging.INFO)
    logger.addFilter(RedactionFilter())
    _configured = True


def log_mutation(*, actor: str, source_id: str, message: str) -> None:
    if actor not in ACTORS:
        raise ValueError("actor moet owner, scheduler of webhook zijn")
    if not source_id.strip():
        raise ValueError("source_id is verplicht")
    configure_logging()
    logging.getLogger("app").info(
        "actor=%s source_id=%s %s",
        actor,
        source_id.strip(),
        message,
    )
