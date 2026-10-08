"""HTML pages. Mutations go through the functions in api/."""

from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.caldav_info import build_caldav_info
from app.api.sources import (
    DEFAULT_POLL_SECONDS,
    SourceFailure,
    create_source,
    delete_source,
    endpoint_url,
    get_source,
    list_calendars,
    list_sources,
    list_sync_status,
    poll_from_form,
    rotate_webhook_secret,
    start_sync,
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
    mode = _create_mode(kind)
    return _form_page(request, mode=mode, draft=_draft("", "Europe/Amsterdam"), errors=_blank_errors())


@router.post("/sources")
async def create_source_page(request: Request, kind: str = "private") -> HTMLResponse:
    form = await _form(request)
    chosen = _chosen_kind(kind)
    mode = _create_mode(kind)
    ics_values, ics_error = _ics_values(form, create=True) if chosen == "ics" else ({}, None)
    if isinstance(ics_error, SourceFailure):
        return _form_fragment(
            request,
            mode=mode,
            draft=_draft(form.get("name", ""), "Europe/Amsterdam", form=form),
            errors=_errors(ics_error),
        )
    result = create_source(
        request.app.state.engine,
        request.app.state.radicale,
        name=form.get("name", ""),
        timezone_name=None,
        enabled=None,
        kind=chosen,
        reveal_secret=chosen == "webhook",
        **ics_values,
    )
    if isinstance(result, SourceFailure):
        return _form_fragment(
            request,
            mode=mode,
            draft=_draft(form.get("name", ""), "Europe/Amsterdam", form=form),
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
        draft=_draft(found["name"], found["timezone"], found["id"], source=found),
        errors=_blank_errors(),
        kind_label=found["kind_label"],
        kind=found["kind"],
        webhook=_webhook_view(found),
    )


@router.post("/sources/{source_id}")
async def update_source_page(source_id: str, request: Request) -> HTMLResponse:
    form = await _form(request)
    found = get_source(request.app.state.engine, source_id)
    if found is None:
        raise HTTPException(status_code=404)
    ics_values: dict = {}
    if found["kind"] == "ics":
        ics_values, ics_error = _ics_values(form, create=False)
        if isinstance(ics_error, SourceFailure):
            return _form_fragment(
                request,
                mode="edit",
                draft=_draft(form.get("name", ""), form.get("timezone", ""), source_id, form=form, source=found),
                errors=_errors(ics_error),
                kind_label=found["kind_label"],
                kind=found["kind"],
            )
    result = update_source(
        request.app.state.engine,
        request.app.state.radicale,
        source_id,
        name=form.get("name", ""),
        timezone_name=form.get("timezone", ""),
        **ics_values,
    )
    if isinstance(result, SourceFailure):
        if result.status == 404:
            raise HTTPException(status_code=404)
        return _form_fragment(
            request,
            mode="edit",
            draft=_draft(form.get("name", ""), form.get("timezone", ""), source_id, form=form, source=found),
            errors=_errors(result),
            kind_label=found["kind_label"],
            kind=found["kind"],
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


@router.post("/sources/{source_id}/sync")
def sync_source_page(source_id: str, request: Request) -> HTMLResponse:
    form_error = ""
    result = start_sync(request.app.state.engine, request.app.state.radicale, source_id)
    if isinstance(result, SourceFailure):
        if result.status == 404:
            raise HTTPException(status_code=404)
        form_error = result.detail
    view = request.query_params.get("view", "")
    if view == "sync":
        return templates.TemplateResponse(
            request,
            "sync_panel.html",
            {"sources": list_sync_status(request.app.state.engine), "error": form_error},
        )
    return _sources_panel(request, form_error)


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
            "error": "",
        },
    )


@router.get("/sync/panel")
def sync_panel(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "sync_panel.html",
        {"sources": list_sync_status(request.app.state.engine), "error": ""},
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
    kind: str = "",
    webhook: dict | None = None,
) -> HTMLResponse:
    return _page(
        request,
        "source_page.html",
        _form_context(mode, draft, errors, kind_label=kind_label, kind=kind, webhook=webhook),
    )


def _form_fragment(
    request: Request,
    *,
    mode: str,
    draft: dict,
    errors: dict[str, str],
    kind_label: str = "Private",
    kind: str = "",
) -> HTMLResponse:
    name = "webhook_form.html" if mode == "webhook" else "source_form.html"
    return templates.TemplateResponse(
        request,
        name,
        _form_context(mode, draft, errors, kind_label=kind_label, kind=kind),
    )


def _form_context(
    mode: str,
    draft: dict,
    errors: dict[str, str],
    *,
    kind_label: str = "Private",
    kind: str = "",
    webhook: dict | None = None,
) -> dict:
    editing = mode == "edit"
    webhook_mode = mode == "webhook"
    ics_mode = mode == "ics"
    if webhook_mode:
        title = "Add a webhook"
        heading = "Add a webhook"
        submit_label = "Add a webhook"
        action = "/sources?kind=webhook"
        label = "Webhook"
    elif ics_mode:
        title = "Add an external ICS"
        heading = "Add an external ICS"
        submit_label = "Add an external ICS"
        action = "/sources?kind=ics"
        label = "External ICS"
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
        "ics": ics_mode or (editing and kind == "ics"),
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


def _draft(
    name: str,
    timezone_name: str,
    source_id: str = "",
    *,
    form: dict | None = None,
    source: dict | None = None,
) -> dict:
    posted = form or {}
    stored = source or {}
    if form is not None:
        poll = posted.get("poll_seconds", "")
        allow = 1 if posted.get("ics_allow_http") == "1" else 0
        url = posted.get("ics_url", "")
    elif stored.get("kind") == "ics":
        poll = stored.get("poll_seconds", DEFAULT_POLL_SECONDS)
        allow = stored.get("ics_allow_http", 0)
        url = stored.get("ics_url") or ""
    else:
        poll = DEFAULT_POLL_SECONDS
        allow = 0
        url = ""
    header_set = bool(stored.get("ics_authorization_set")) and posted.get("clear_ics_authorization") != "1"
    return {
        "id": source_id or stored.get("id", ""),
        "name": name,
        "timezone": timezone_name,
        "ics_url": url,
        "poll_seconds": poll,
        "ics_allow_http": allow,
        "ics_authorization_set": header_set,
    }


def _blank_errors() -> dict[str, str]:
    return {
        "top": "",
        "name": "",
        "timezone": "",
        "ics_url": "",
        "poll_seconds": "",
        "ics_authorization_header": "",
        "ics_allow_http": "",
    }


def _errors(failure: SourceFailure) -> dict[str, str]:
    return {
        "top": failure.detail,
        "name": failure.fields.get("name", ""),
        "timezone": failure.fields.get("timezone", ""),
        "ics_url": failure.fields.get("ics_url", ""),
        "poll_seconds": failure.fields.get("poll_seconds", ""),
        "ics_authorization_header": failure.fields.get("ics_authorization_header", ""),
        "ics_allow_http": failure.fields.get("ics_allow_http", ""),
    }


def _create_mode(kind: str) -> str:
    if kind == "webhook":
        return "webhook"
    if kind == "ics":
        return "ics"
    return "create"


def _chosen_kind(kind: str) -> str:
    if kind == "webhook":
        return "webhook"
    if kind == "ics":
        return "ics"
    return "private"


def _ics_values(form: dict[str, str], *, create: bool) -> tuple[dict, SourceFailure | None]:
    raw_poll = form.get("poll_seconds", "")
    if create and raw_poll.strip() == "":
        poll: int | SourceFailure = DEFAULT_POLL_SECONDS
    else:
        poll = poll_from_form(raw_poll)
    if isinstance(poll, SourceFailure):
        return {}, poll
    header = form.get("ics_authorization_header", "").strip()
    values = {
        "ics_url": form.get("ics_url", ""),
        "ics_allow_http": 1 if form.get("ics_allow_http") == "1" else 0,
        "poll_seconds": poll,
        "ics_authorization_header": header or None,
    }
    if not create:
        values["clear_ics_authorization"] = form.get("clear_ics_authorization") == "1"
    return values, None


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
