"""Incoming webhooks."""

import json
import threading
import time
from collections import deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.access import json_error
from app.api.sources import get_source, webhook_secret_matches
from app.secrets import SecretKeyError
from app.sync.runner import SyncFailed, run_webhook
from app.sync.webhook import ParseError, parse

router = APIRouter()

MAX_BODY = 256 * 1024
_LIMIT = 60
_WINDOW_SECONDS = 60.0


class RateLimiter:
    """Sixty requests per minute for one source, in this process."""

    def __init__(self, limit: int = _LIMIT, window: float = _WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and moment - hits[0] >= self.window:
                hits.popleft()
            hits.append(moment)
            return len(hits) <= self.limit


@router.post("/api/webhooks/{source_id}", response_model=None)
async def receive(source_id: str, request: Request) -> dict | JSONResponse:
    body = await _limited_body(request)
    if isinstance(body, JSONResponse):
        return body
    limiter: RateLimiter = request.app.state.webhook_rates
    if not limiter.allow(source_id):
        return json_error(429, "rate_limited", "Too many requests for this source.")
    source = get_source(request.app.state.engine, source_id)
    if source is None or source["kind"] != "webhook":
        return json_error(404, "not_found", "Not found.")
    provided = request.headers.get("x-webhook-secret", "")
    try:
        matches = webhook_secret_matches(request.app.state.engine, source_id, provided)
    except SecretKeyError as exc:
        return json_error(500, "internal_error", str(exc))
    if not matches:
        return json_error(401, "invalid_secret", "The webhook secret is not valid.")
    if source["enabled"] != 1:
        return json_error(422, "sync_disabled", "Sync is turned off for this source.")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return json_error(422, "invalid_request", "The input is invalid.")
    try:
        command = parse(payload, source["timezone"])
    except ParseError as exc:
        return json_error(422, "invalid_request", exc.detail)
    try:
        run_webhook(request.app.state.engine, request.app.state.radicale, source, command)
    except SyncFailed as exc:
        return json_error(500, "internal_error", exc.message)
    return {"ok": True}


async def _limited_body(request: Request) -> bytes | JSONResponse:
    header = request.headers.get("content-length")
    if header is not None:
        try:
            declared = int(header)
        except ValueError:
            return json_error(400, "bad_request", "The request is invalid.")
        if declared > MAX_BODY:
            return json_error(413, "payload_too_large", "The body is larger than 256 KB.")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY:
            return json_error(413, "payload_too_large", "The body is larger than 256 KB.")
        chunks.append(chunk)
    return b"".join(chunks)
