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
    endpoint_url,
    get_source,
    list_calendars,
    list_sources,
    list_sync_status,
    rotate_webhook_secret,
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
def new_source_page(request: Request, kind: str = "private") -> HTMLResponse:
    mode = "webhook" if kind == "webhook" else "create"
    return _form_page(request, mode=mode, draft=_draft("", "Europe/Amsterdam"), errors=_blank_errors())


@router.post("/sources")
async def create_source_page(request: Request, kind: str = "private") -> HTMLResponse:
    form = await _form(request)
    chosen = "webhook" if kind == "webhook" else "private"
    result = create_source(
        request.app.state.engine,
        request.app.state.radicale,
        name=form.get("name", ""),
        timezone_name=None,
        enabled=None,
        kind=chosen,
        reveal_secret=chosen == "webhook",
    )
    if isinstance(result, SourceFailure):
        return _form_fragment(
            request,
            mode="webhook" if chosen == "webhook" else "create",
            draft=_draft(form.get("name", ""), "Europe/Amsterdam"),
            errors=_errors(result),
        )
    if chosen == "webhook":
        return _secret_fragment(request, result["id"], result.get("webhook_secret", ""))
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
        kind_label=found["kind_label"],
        webhook=_webhook_view(found),
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
        found = get_source(request.app.state.engine, source_id)
        label = found["kind_label"] if found is not None else "Private"
        return _form_fragment(
            request,
            mode="edit",
            draft=_draft(form.get("name", ""), form.get("timezone", ""), source_id),
            errors=_errors(result),
            kind_label=label,
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


@router.post("/sources/{source_id}/rotate-secret")
def rotate_secret_page(source_id: str, request: Request) -> HTMLResponse:
    result = rotate_webhook_secret(request.app.state.engine, source_id, reveal=True)
    if isinstance(result, SourceFailure):
        if result.status == 404:
            raise HTTPException(status_code=404)
        return _panel(request, source_id, "", result.detail)
    return _secret_panel(request, source_id, result.get("webhook_secret", ""))


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


@router.get("/sync")
def sync_page(request: Request) -> HTMLResponse:
    return _page(
        request,
        "sync.html",
        {
            "title": "Sync",
            "sources": list_sync_status(request.app.state.engine),
        },
    )


@router.get("/sync/panel")
def sync_panel(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "sync_panel.html",
        {"sources": list_sync_status(request.app.state.engine)},
    )


@router.get("/app.css")
def stylesheet() -> FileResponse:
    return FileResponse(_ADMIN / "app.css", media_type="text/css")


@router.get("/htmx.min.js")
def htmx_script() -> FileResponse:
    return FileResponse(_ADMIN / "htmx.min.js", media_type="text/javascript")


def _page(request: Request, name: str, context: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context)


def _form_page(
    request: Request,
    *,
    mode: str,
    draft: dict,
    errors: dict[str, str],
    kind_label: str = "Private",
    webhook: dict | None = None,
) -> HTMLResponse:
    return _page(
        request,
        "source_page.html",
        _form_context(mode, draft, errors, kind_label=kind_label, webhook=webhook),
    )


def _form_fragment(
    request: Request,
    *,
    mode: str,
    draft: dict,
    errors: dict[str, str],
    kind_label: str = "Private",
) -> HTMLResponse:
    name = "webhook_form.html" if mode == "webhook" else "source_form.html"
    return templates.TemplateResponse(
        request,
        name,
        _form_context(mode, draft, errors, kind_label=kind_label),
    )


def _form_context(
    mode: str,
    draft: dict,
    errors: dict[str, str],
    *,
    kind_label: str = "Private",
    webhook: dict | None = None,
) -> dict:
    editing = mode == "edit"
    webhook_mode = mode == "webhook"
    if webhook_mode:
        title = "Add a webhook"
        heading = "Add a webhook"
        submit_label = "Add a webhook"
        action = "/sources?kind=webhook"
        label = "Webhook"
    elif editing:
        title = "Edit source"
        heading = "Edit source"
        submit_label = "Save"
        action = f"/sources/{draft['id']}"
        label = kind_label
    else:
        title = "Add a source"
        heading = "Add a source"
        submit_label = "Add a source"
        action = "/sources"
        label = "Private"
    return {
        "title": title,
        "heading": heading,
        "submit_label": submit_label,
        "action": action,
        "mode": mode,
        "kind_label": label,
        "draft": draft,
        "errors": errors,
        "webhook_url": "" if webhook is None else webhook["url"],
        "webhook_secret": "",
        "source_id": draft["id"],
        "error": "",
    }


def _secret_fragment(request: Request, source_id: str, secret: str) -> HTMLResponse:
    response = templates.TemplateResponse(
        request,
        "webhook_created.html",
        {
            "webhook_url": endpoint_url(source_id),
            "webhook_secret": secret,
            "source_id": source_id,
            "error": "",
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _secret_panel(request: Request, source_id: str, secret: str) -> HTMLResponse:
    response = _panel(request, source_id, secret, "")
    response.headers["Cache-Control"] = "no-store"
    return response


def _panel(request: Request, source_id: str, secret: str, error: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "webhook_panel.html",
        {
            "webhook_url": endpoint_url(source_id),
            "webhook_secret": secret,
            "source_id": source_id,
            "error": error,
        },
    )


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


def _webhook_view(found: dict) -> dict | None:
    if found["kind"] != "webhook":
        return None
    return {"url": endpoint_url(found["id"])}


def _redirect(url: str) -> HTMLResponse:
    return HTMLResponse("", status_code=200, headers={"HX-Redirect": url})


async def _form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}
