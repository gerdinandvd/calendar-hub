"""Mutation requirements and error responses. No stack trace to the client."""

import logging
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_STATUS = {
    400: ("bad_request", "The request is invalid."),
    404: ("not_found", "Not found."),
    405: ("method_not_allowed", "This method is not allowed."),
    415: ("unsupported_media_type", "Content-Type must be application/json."),
    422: ("invalid_request", "The input is invalid."),
    500: ("internal_error", "Something went wrong."),
}


def json_error(status: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail})


def _status_body(status: int) -> tuple[str, str]:
    if status in _STATUS:
        return _STATUS[status]
    return ("http_error", "The request failed.")


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _is_webhook(path: str) -> bool:
    return path == "/api/webhooks" or path.startswith("/api/webhooks/")


def _json_content_type(value: str) -> bool:
    media = value.split(";", 1)[0].strip().lower()
    return media == "application/json"


def _header_map(scope: Scope) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, value in scope.get("headers", []):
        key = name.decode("latin-1").lower()
        if key not in found:
            found[key] = value.decode("latin-1")
    return found


class MutationGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] in UNSAFE_METHODS:
            rejection = _reject_mutation(scope["path"], _header_map(scope))
            if rejection is not None:
                await rejection(scope, receive, send)
                return

        started = False

        async def send_wrapper(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if started:
                raise
            path = scope.get("path", "")
            logging.getLogger("app").error("internal_error path=%s", path)
            error, detail = _STATUS[500]
            if _is_api(path):
                response: Response = json_error(500, error, detail)
            else:
                response = PlainTextResponse(detail, status_code=500)
            await response(scope, receive, send)


def _reject_mutation(path: str, headers: Mapping[str, str]) -> Response | None:
    if _is_api(path):
        if _is_webhook(path):
            return None
        if not _json_content_type(headers.get("content-type", "")):
            return json_error(415, "unsupported_media_type", _STATUS[415][1])
        return None
    if not headers.get("hx-request", "").strip():
        return PlainTextResponse(_hx_detail(), status_code=400)
    return None


def _hx_detail() -> str:
    return "This action requires the HX-Request header."


def install_access(app: FastAPI) -> None:
    app.add_middleware(MutationGuard)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        error, detail = _status_body(exc.status_code)
        if _is_api(request.url.path):
            return json_error(exc.status_code, error, detail)
        return PlainTextResponse(detail, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        request: Request, _exc: RequestValidationError
    ) -> Response:
        error, detail = _STATUS[422]
        if _is_api(request.url.path):
            return json_error(422, error, detail)
        return PlainTextResponse(detail, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, _exc: Exception) -> Response:
        logging.getLogger("app").error("internal_error path=%s", request.url.path)
        error, detail = _STATUS[500]
        if _is_api(request.url.path):
            return json_error(500, error, detail)
        return PlainTextResponse(detail, status_code=500)
