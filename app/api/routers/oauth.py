"""Google OAuth start and callback. The callback is public and uses a one-time state."""

import secrets
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app.access import json_error
from app.services.sources import (
    GoogleConnectError,
    connect_google_source,
    get_source,
    google_authorization_url,
)

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


@router.get("/api/oauth/google/start")
def start_google(source_id: str, request: Request) -> Response:
    source = get_source(request.app.state.engine, source_id)
    if source is None:
        return json_error(404, "not_found", "Not found.")
    if source["kind"] != "google":
        return json_error(
            422, "invalid_request", "Those fields are only for a Google source."
        )
    try:
        url = google_authorization_url(state=_states(request).issue(source_id))
    except GoogleConnectError:
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
    source = get_source(request.app.state.engine, source_id)
    if source is None:
        return RedirectResponse("/?google=state", status_code=302)
    if source["kind"] != "google":
        return RedirectResponse(f"/sources/{source_id}?google=state", status_code=302)
    try:
        connect_google_source(request.app.state.engine, source_id, code=code)
    except GoogleConnectError as exc:
        return RedirectResponse(
            f"/sources/{source_id}?google={exc.reason}", status_code=302
        )
    return RedirectResponse(f"/sources/{source_id}", status_code=302)
