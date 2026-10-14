"""HTTP routes for sources and calendars."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.access import json_error
from app.api.schemas.sources import SourceCreate, SourcePatch
from app.i18n import english
from app.services.sources import (
    SourceFailure,
    create_source,
    delete_source,
    get_source,
    list_sources,
    update_source,
)
from app.services.sources.present import list_calendars
from app.services.sources.sync_ops import start_sync
from app.services.sources.webhook import rotate_webhook_secret

router = APIRouter()


@router.get("/api/sources")
def read_sources(request: Request) -> dict:
    return {"sources": list_sources(request.app.state.engine)}


@router.post("/api/sources", status_code=201, response_model=None)
def post_source(body: SourceCreate, request: Request) -> dict | JSONResponse:
    result = create_source(
        request.app.state.engine,
        request.app.state.radicale,
        name=body.name,
        timezone_name=body.timezone,
        enabled=body.enabled,
        kind=body.kind,
        ics_url=body.ics_url,
        ics_allow_http=body.ics_allow_http,
        poll_seconds=body.poll_seconds,
        ics_authorization_header=body.ics_authorization_header,
        google_calendar_id=body.google_calendar_id,
    )
    return _json(result)


@router.get("/api/sources/{source_id}", response_model=None)
def read_source(source_id: str, request: Request) -> dict | JSONResponse:
    found = get_source(request.app.state.engine, source_id)
    if found is None:
        return _json(SourceFailure(404, "not_found", "sentence.not_found"))
    return found


@router.patch("/api/sources/{source_id}", response_model=None)
def patch_source(
    source_id: str, body: SourcePatch, request: Request
) -> dict | JSONResponse:
    result = update_source(
        request.app.state.engine,
        request.app.state.radicale,
        source_id,
        name=body.name,
        timezone_name=body.timezone,
        enabled=body.enabled,
        ics_url=body.ics_url,
        ics_allow_http=body.ics_allow_http,
        poll_seconds=body.poll_seconds,
        ics_authorization_header=body.ics_authorization_header,
        clear_ics_authorization=body.clear_ics_authorization == 1,
        google_calendar_id=body.google_calendar_id,
    )
    return _json(result)


@router.post("/api/sources/{source_id}/sync", response_model=None)
def post_sync(source_id: str, request: Request) -> dict | JSONResponse:
    result = start_sync(request.app.state.engine, request.app.state.radicale, source_id)
    if isinstance(result, SourceFailure):
        return json_error(result.status, result.error, english(result.detail))
    return JSONResponse(result, status_code=202)


@router.post("/api/sources/{source_id}/rotate-secret", response_model=None)
def rotate_secret(source_id: str, request: Request) -> dict | JSONResponse:
    result = rotate_webhook_secret(request.app.state.engine, source_id, reveal=False)
    return _json(result)


@router.delete("/api/sources/{source_id}", response_model=None)
def remove_source(
    source_id: str, request: Request, confirm: bool = False
) -> dict | JSONResponse:
    result = delete_source(
        request.app.state.engine,
        request.app.state.radicale,
        source_id,
        confirm=confirm,
    )
    if result is None:
        return {"deleted": True}
    return _json(result)


@router.get("/api/calendars")
def read_calendars(request: Request) -> dict:
    return {"calendars": list_calendars(request.app.state.engine)}


def _json(result: dict | SourceFailure) -> dict | JSONResponse:
    if isinstance(result, SourceFailure):
        return json_error(result.status, result.error, english(result.detail))
    return result
