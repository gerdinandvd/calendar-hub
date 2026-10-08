"""HTML pages. Mutations go through the functions in api/."""

from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.caldav_info import build_caldav_info
from app.api.sources import (
    SourceFailure,
    create_source,
    delete_source,
    get_source,
    list_calendars,
    list_sources,
    update_source,
)

router = APIRouter()
_ADMIN = Path(__file__).resolve().parents[1] / "admin"
templates = Jinja2Templates(directory=_ADMIN)


@router.get("/")
def home(request: Request) -> HTMLResponse:
    engine = request.app.state.engine
    return _page(
        request,
        "home.html",
        {
            "title": "Calendar hub",
            "sources": list_sources(engine),
            "caldav": build_caldav_info(engine),
        },
    )


@router.get("/sources")
def sources_page(request: Request) -> HTMLResponse:
    return _page(
        request,
        "sources.html",
        {
            "title": "Sources",
            "sources": list_sources(request.app.state.engine),
            "error": "",
        },
    )


@router.get("/sources/new")
def new_source_page(request: Request) -> HTMLResponse:
    return _form_page(request, mode="create", draft=_draft("", "Europe/Amsterdam"), errors=_blank_errors())


@router.post("/sources")
async def create_source_page(request: Request) -> HTMLResponse:
    form = await _form(request)
    result = create_source(
        request.app.state.engine,
        request.app.state.radicale,
        name=form.get("name", ""),
        timezone_name=None,
        enabled=None,
        kind="private",
    )
    if isinstance(result, SourceFailure):
        return _form_fragment(
            request,
            mode="create",
            draft=_draft(form.get("name", ""), "Europe/Amsterdam"),
            errors=_errors(result),
        )
    return _redirect("/sources")


@router.get("/sources/{source_id}")
def edit_source_page(source_id: str, request: Request) -> HTMLResponse:
    found = get_source(request.app.state.engine, source_id)
    if found is None:
        raise HTTPException(status_code=404)
    return _form_page(
        request,
        mode="edit",
        draft=_draft(found["name"], found["timezone"], found["id"]),
        errors=_blank_errors(),
    )


@router.post("/sources/{source_id}")
async def update_source_page(source_id: str, request: Request) -> HTMLResponse:
    form = await _form(request)
    result = update_source(
        request.app.state.engine,
        request.app.state.radicale,
        source_id,
        name=form.get("name", ""),
        timezone_name=form.get("timezone", ""),
    )
    if isinstance(result, SourceFailure):
        if result.status == 404:
            raise HTTPException(status_code=404)
        return _form_fragment(
            request,
            mode="edit",
            draft=_draft(form.get("name", ""), form.get("timezone", ""), source_id),
            errors=_errors(result),
        )
    return _redirect("/sources")


@router.post("/sources/{source_id}/enabled")
async def set_enabled(source_id: str, request: Request) -> HTMLResponse:
    form = await _form(request)
    raw = form.get("enabled", "")
    if raw not in {"0", "1"}:
        return _sources_panel(request, "Enabled must be 0 or 1.")
    result = update_source(
        request.app.state.engine,
        request.app.state.radicale,
        source_id,
        enabled=int(raw),
    )
    if isinstance(result, SourceFailure):
        if result.status == 404:
            raise HTTPException(status_code=404)
        return _sources_panel(request, result.detail)
    return _sources_panel(request, "")


@router.post("/sources/{source_id}/delete")
async def delete_source_page(source_id: str, request: Request) -> HTMLResponse:
    form = await _form(request)
    result = delete_source(
        request.app.state.engine,
        request.app.state.radicale,
        source_id,
        confirm=form.get("confirm") == "true",
    )
    if isinstance(result, SourceFailure):
        if result.status == 404:
            raise HTTPException(status_code=404)
        return _sources_panel(request, result.detail)
    return _sources_panel(request, "")


@router.get("/calendars")
def calendars_page(request: Request) -> HTMLResponse:
    return _page(
        request,
        "calendars.html",
        {
            "title": "Calendars",
            "calendars": list_calendars(request.app.state.engine),
        },
    )


@router.get("/app.css")
def stylesheet() -> FileResponse:
    return FileResponse(_ADMIN / "app.css", media_type="text/css")


@router.get("/htmx.min.js")
def htmx_script() -> FileResponse:
    return FileResponse(_ADMIN / "htmx.min.js", media_type="text/javascript")


def _page(request: Request, name: str, context: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context)


def _form_page(request: Request, *, mode: str, draft: dict, errors: dict[str, str]) -> HTMLResponse:
    return _page(request, "source_page.html", _form_context(mode, draft, errors))


def _form_fragment(request: Request, *, mode: str, draft: dict, errors: dict[str, str]) -> HTMLResponse:
    return templates.TemplateResponse(request, "source_form.html", _form_context(mode, draft, errors))


def _form_context(mode: str, draft: dict, errors: dict[str, str]) -> dict:
    editing = mode == "edit"
    return {
        "title": "Edit source" if editing else "Add a source",
        "heading": "Edit source" if editing else "Add a source",
        "submit_label": "Save" if editing else "Add a source",
        "action": f"/sources/{draft['id']}" if editing else "/sources",
        "mode": mode,
        "draft": draft,
        "errors": errors,
    }


def _sources_panel(request: Request, error: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "sources_panel.html",
        {
            "sources": list_sources(request.app.state.engine),
            "error": error,
        },
    )


def _draft(name: str, timezone_name: str, source_id: str = "") -> dict:
    return {"id": source_id, "name": name, "timezone": timezone_name}


def _blank_errors() -> dict[str, str]:
    return {"top": "", "name": "", "timezone": ""}


def _errors(failure: SourceFailure) -> dict[str, str]:
    return {
        "top": failure.detail,
        "name": failure.fields.get("name", ""),
        "timezone": failure.fields.get("timezone", ""),
    }


def _redirect(url: str) -> HTMLResponse:
    return HTMLResponse("", status_code=200, headers={"HX-Redirect": url})


async def _form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}
