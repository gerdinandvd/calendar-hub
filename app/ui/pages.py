"""HTML pages. Mutations go through the shared application services."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.services.sources import (
    SourceFailure,
    create_source,
    delete_source,
    get_source,
    list_sources,
    update_source,
)
from app.services.sources.present import build_caldav_info
from app.services.sources.sync_ops import start_sync
from app.services.sources.webhook import rotate_webhook_secret
from app.ui.render import (
    _ADMIN,
    _health_view,
    _page,
    _render,
    _sentence,
    _sync_rows,
    _t,
)
from app.ui.source_views import (
    _after_mutation,
    _blank_errors,
    _chosen_kind,
    _create_mode,
    _created_workspace,
    _draft,
    _edit_context,
    _errors,
    _form,
    _form_fragment,
    _form_page,
    _kind_values,
    _panel,
    _redirect,
    _saved,
    _secret_panel,
    _source_main,
    _sources_panel,
)

router = APIRouter()


@router.get("/")
def home(request: Request) -> HTMLResponse:
    engine = request.app.state.engine
    translate = _t(request)
    return _page(
        request,
        "home.html",
        {
            "title": translate("brand"),
            "sources": list_sources(engine),
            "caldav": build_caldav_info(engine),
            "notice": "",
        },
    )


@router.get("/sources")
def sources_page(request: Request) -> HTMLResponse:
    return _page(
        request,
        "sources.html",
        {
            "title": _t(request)("nav.sources"),
            "sources": list_sources(request.app.state.engine),
            "error": "",
        },
    )


@router.get("/sources/new")
def new_source_page(request: Request, kind: str = "private") -> HTMLResponse:
    mode = _create_mode(kind)
    chosen = _chosen_kind(kind)
    return _form_page(
        request,
        mode=mode,
        draft=_draft("", "Europe/Amsterdam", kind=chosen),
        errors=_blank_errors(),
    )


@router.post("/sources")
async def create_source_page(request: Request, kind: str = "private") -> HTMLResponse:
    form = await _form(request)
    chosen = _chosen_kind(kind)
    mode = _create_mode(kind)
    extra, extra_error = _kind_values(form, chosen, create=True)
    if isinstance(extra_error, SourceFailure):
        return _form_fragment(
            request,
            mode=mode,
            draft=_draft(
                form.get("name", ""), "Europe/Amsterdam", form=form, kind=chosen
            ),
            errors=_errors(extra_error, request),
        )
    result = create_source(
        request.app.state.engine,
        request.app.state.radicale,
        name=form.get("name", ""),
        timezone_name=None,
        enabled=None,
        kind=chosen,
        reveal_secret=chosen == "webhook",
        **extra,
    )
    if isinstance(result, SourceFailure):
        return _form_fragment(
            request,
            mode=mode,
            draft=_draft(
                form.get("name", ""), "Europe/Amsterdam", form=form, kind=chosen
            ),
            errors=_errors(result, request),
        )
    if chosen == "webhook":
        return _created_workspace(
            request, result["id"], result.get("webhook_secret", "")
        )
    return _redirect(f"/sources/{result['id']}")


@router.get("/sources/{source_id}")
def edit_source_page(source_id: str, request: Request) -> HTMLResponse:
    found = get_source(request.app.state.engine, source_id)
    if found is None:
        raise HTTPException(status_code=404)
    return _page(request, "source_page.html", _edit_context(request, found))


@router.post("/sources/{source_id}")
async def update_source_page(source_id: str, request: Request) -> HTMLResponse:
    form = await _form(request)
    found = get_source(request.app.state.engine, source_id)
    if found is None:
        raise HTTPException(status_code=404)
    extra, extra_error = _kind_values(form, found["kind"], create=False)
    if isinstance(extra_error, SourceFailure):
        return _form_fragment(
            request,
            mode="edit",
            draft=_draft(
                form.get("name", ""),
                form.get("timezone", ""),
                source_id,
                form=form,
                source=found,
            ),
            errors=_errors(extra_error, request),
            kind=found["kind"],
        )
    result = update_source(
        request.app.state.engine,
        request.app.state.radicale,
        source_id,
        name=form.get("name", ""),
        timezone_name=form.get("timezone", ""),
        **extra,
    )
    if isinstance(result, SourceFailure):
        if result.status == 404:
            raise HTTPException(status_code=404)
        return _form_fragment(
            request,
            mode="edit",
            draft=_draft(
                form.get("name", ""),
                form.get("timezone", ""),
                source_id,
                form=form,
                source=found,
            ),
            errors=_errors(result, request),
            kind=found["kind"],
        )
    return _saved(request, source_id)


@router.post("/sources/{source_id}/enabled")
async def set_enabled(source_id: str, request: Request) -> HTMLResponse:
    form = await _form(request)
    raw = form.get("enabled", "")
    if raw not in {"0", "1"}:
        return _after_mutation(request, source_id, "sentence.enabled")
    result = update_source(
        request.app.state.engine,
        request.app.state.radicale,
        source_id,
        enabled=int(raw),
    )
    if isinstance(result, SourceFailure):
        if result.status == 404:
            raise HTTPException(status_code=404)
        return _after_mutation(request, source_id, result.detail)
    return _after_mutation(request, source_id, "")


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
        return _after_mutation(request, source_id, result.detail)
    if request.query_params.get("view", "") == "source":
        return _redirect("/sources")
    return _sources_panel(request, "")


@router.post("/sources/{source_id}/rotate-secret")
def rotate_secret_page(source_id: str, request: Request) -> HTMLResponse:
    result = rotate_webhook_secret(request.app.state.engine, source_id, reveal=True)
    if isinstance(result, SourceFailure):
        if result.status == 404:
            raise HTTPException(status_code=404)
        return _panel(request, source_id, "", result.detail)
    return _secret_panel(
        request, source_id, result.get("webhook_secret", ""), replaced=True
    )


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
        return _render(
            request,
            "sync_panel.html",
            {"sources": _sync_rows(request), "error": _sentence(request, form_error)},
        )
    if view == "source":
        return _source_main(request, source_id, banner=form_error)
    return _sources_panel(request, form_error)


@router.get("/calendars")
def calendars_page(request: Request) -> HTMLResponse:
    return _page(
        request,
        "calendars.html",
        {
            "title": _t(request)("nav.calendars"),
            "calendars": list_sources(request.app.state.engine),
        },
    )


@router.get("/health")
def health_page(request: Request) -> HTMLResponse:
    return _page(
        request,
        "health.html",
        {"title": _t(request)("nav.health"), "health": _health_view(request)},
    )


@router.get("/health/panel")
def health_panel(request: Request) -> HTMLResponse:
    return _render(request, "health_panel.html", {"health": _health_view(request)})


@router.get("/sync")
def sync_page(request: Request) -> HTMLResponse:
    return _page(
        request,
        "sync.html",
        {
            "title": _t(request)("nav.sync"),
            "sources": _sync_rows(request),
            "error": "",
        },
    )


@router.get("/sync/panel")
def sync_panel(request: Request) -> HTMLResponse:
    return _render(
        request,
        "sync_panel.html",
        {"sources": _sync_rows(request), "error": ""},
    )


@router.get("/app.css")
def stylesheet() -> FileResponse:
    return FileResponse(_ADMIN / "app.css", media_type="text/css")


@router.get("/htmx.min.js")
def htmx_script() -> FileResponse:
    return FileResponse(_ADMIN / "htmx.min.js", media_type="text/javascript")
