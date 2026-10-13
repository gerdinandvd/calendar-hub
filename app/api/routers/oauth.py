"""Google OAuth start and callback. The callback is public and uses a one-time state."""

import os
import secrets
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.access import json_error
from app.db.models import Source
from app.log import log_mutation
from app.secrets import SecretKeyError
from app.sync import google
from app.sync.google import store_tokens

router = APIRouter()

STATE_SECONDS = 600


class OAuthStates:
    """One-time state values for the life of this process."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def issue(self, source_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._items[token] = (source_id, time.monotonic())
        return token

    def consume(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            found = self._items.pop(token, None)
        if found is None:
            return None
        source_id, issued = found
        if time.monotonic() - issued > STATE_SECONDS:
            return None
        return source_id


def _states(request: Request) -> OAuthStates:
    states = getattr(request.app.state, "oauth_states", None)
    if states is None:
        states = OAuthStates()
        request.app.state.oauth_states = states
    return states


def _callback_uri() -> str:
    host = os.environ.get("HUB_HOST", "").strip()
    client = os.environ.get("HUB_GOOGLE_CLIENT_ID", "").strip()
    secret = os.environ.get("HUB_GOOGLE_CLIENT_SECRET", "").strip()
    if not host or not client or not secret:
        raise google.GoogleError(google.NOT_CONFIGURED)
    return f"https://{host}/api/oauth/google/callback"


@router.get("/api/oauth/google/start")
def start_google(source_id: str, request: Request) -> Response:
    engine = request.app.state.engine
    with Session(engine) as session:
        row = session.get(Source, source_id)
        kind = None if row is None else row.kind
    if kind is None:
        return json_error(404, "not_found", "Not found.")
    if kind != "google":
        return json_error(
            422, "invalid_request", "Those fields are only for a Google source."
        )
    try:
        redirect_uri = _callback_uri()
        url = google.authorization_url(
            state=_states(request).issue(source_id),
            redirect_uri=redirect_uri,
        )
    except google.GoogleError:
        return RedirectResponse(f"/sources/{source_id}?google=config", status_code=302)
    return RedirectResponse(url, status_code=302)


@router.get("/api/oauth/google/callback")
def google_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    source_id = _states(request).consume(state)
    if source_id is None:
        return RedirectResponse("/?google=state", status_code=302)
    if error or not code:
        return RedirectResponse(f"/sources/{source_id}?google=state", status_code=302)
    engine = request.app.state.engine
    with Session(engine) as session:
        row = session.get(Source, source_id)
        kind = None if row is None else row.kind
    if kind is None:
        return RedirectResponse("/?google=state", status_code=302)
    if kind != "google":
        return RedirectResponse(f"/sources/{source_id}?google=state", status_code=302)
    try:
        bundle = google.exchange_code(code=code, redirect_uri=_callback_uri())
    except google.ScopeRejected:
        return RedirectResponse(f"/sources/{source_id}?google=scope", status_code=302)
    except google.GoogleError:
        return RedirectResponse(f"/sources/{source_id}?google=token", status_code=302)
    try:
        store_tokens(engine, source_id, bundle)
    except SecretKeyError:
        return RedirectResponse(f"/sources/{source_id}?google=token", status_code=302)
    log_mutation(actor="owner", source_id=source_id, message="connected google source")
    return RedirectResponse(f"/sources/{source_id}", status_code=302)
