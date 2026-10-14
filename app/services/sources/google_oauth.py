"""Google OAuth connect for Google sources."""

import os

from sqlalchemy.engine import Engine

from app.log import log_mutation
from app.secrets import SecretKeyError
from app.services.sources.types import GoogleConnectError
from app.sync import google
from app.sync.google import store_tokens


def google_callback_uri() -> str:
    host = os.environ.get("HUB_HOST", "").strip()
    client = os.environ.get("HUB_GOOGLE_CLIENT_ID", "").strip()
    secret = os.environ.get("HUB_GOOGLE_CLIENT_SECRET", "").strip()
    if not host or not client or not secret:
        raise GoogleConnectError("config")
    return f"https://{host}/api/oauth/google/callback"


def google_authorization_url(*, state: str) -> str:
    try:
        return google.authorization_url(
            state=state,
            redirect_uri=google_callback_uri(),
        )
    except google.GoogleError as exc:
        if exc.message == google.NOT_CONFIGURED:
            raise GoogleConnectError("config") from None
        raise GoogleConnectError("token") from None


def connect_google_source(engine: Engine, source_id: str, *, code: str) -> None:
    try:
        redirect_uri = google_callback_uri()
        bundle = google.exchange_code(code=code, redirect_uri=redirect_uri)
        store_tokens(engine, source_id, bundle)
    except GoogleConnectError:
        raise
    except google.ScopeRejected:
        raise GoogleConnectError("scope") from None
    except (google.GoogleError, SecretKeyError):
        raise GoogleConnectError("token") from None
    log_mutation(actor="owner", source_id=source_id, message="connected google source")
