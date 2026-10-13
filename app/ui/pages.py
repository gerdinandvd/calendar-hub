"""HTML pages. Mutations go through the shared application services."""

from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.health import report as health_report
from app.i18n import present, translator
from app.services.sources import (
    DEFAULT_POLL_SECONDS,
    GOOGLE_POLL_SECONDS,
    SourceFailure,
    build_caldav_info,
    create_source,
    delete_source,
    endpoint_url,
    get_source,
    list_sources,
    list_sync_status,
    poll_from_form,
    rotate_webhook_secret,
    start_sync,
    update_source,
)

_GOOGLE_NOTICE = {
    "state": "sentence.google_incomplete",
    "scope": "sentence.google_write",
    "token": "sentence.google_incomplete",
    "config": "sentence.google_config",
}
_LANGS = frozenset({"en", "nl"})
_LANG_COOKIE = "hub_lang"

router = APIRouter()
_ADMIN = Path(__file__).resolve().parents[1] / "admin"
templates = Jinja2Templates(directory=_ADMIN)


@router.get("/")
def home(request: Request) -> HTMLResponse:
    engine = request.app.state.engine
    translate = _t(request)
    notice = _GOOGLE_NOTICE.get(request.query_params.get("google", ""), "")
    return _page(
        request,
        "home.html",
        {
            "title": translate("brand"),
            "sources": list_sources(engine),
            "caldav": build_caldav_info(engine),
            "notice": _sentence(request, notice),
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
    notice = _GOOGLE_NOTICE.get(request.query_params.get("google", ""), "")
    return _page(
        request, "source_page.html", _edit_context(request, found, notice=notice)
    )


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


def _health_view(request: Request) -> dict[str, object]:
    report = health_report(request.app.state.engine, request.app.state.radicale)
    shown = dict(report)
    for key in ("radicale_error", "litestream_error", "kopia_error"):
        shown[key] = _sentence(request, str(report.get(key) or ""))
    shown["disk_free"] = _format_bytes(int(report["disk_free_bytes"]))
    return shown


def _format_bytes(amount: int) -> str:
    size = float(amount)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _page(request: Request, name: str, context: dict) -> HTMLResponse:
    return _render(request, name, context)


def _render(request: Request, name: str, context: dict) -> HTMLResponse:
    lang = _language(request)
    payload = dict(context)
    payload["t"] = translator(lang)
    payload["lang"] = lang
    payload["section"] = _section(request.url.path)
    response = templates.TemplateResponse(request, name, payload)
    query = request.query_params.get("lang", "")
    if query in _LANGS:
        response.set_cookie(
            _LANG_COOKIE, query, path="/", samesite="lax", httponly=True
        )
    return response


def _section(path: str) -> str:
    if path == "/":
        return "home"
    for name in ("sources", "calendars", "sync", "health"):
        if path == f"/{name}" or path.startswith(f"/{name}/"):
            return name
    return ""


def _language(request: Request) -> str:
    query = request.query_params.get("lang", "")
    if query in _LANGS:
        return query
    cookie = request.cookies.get(_LANG_COOKIE, "")
    if cookie in _LANGS:
        return cookie
    return "en"


def _t(request: Request):
    return translator(_language(request))


def _sentence(request: Request, text: str) -> str:
    return present(_language(request), text)


def _sync_rows(request: Request) -> list[dict]:
    rows = []
    for source in list_sync_status(request.app.state.engine):
        row = dict(source)
        message = row.get("last_error_message") or ""
        if message:
            row["last_error_message"] = _sentence(request, message)
        rows.append(row)
    return rows


def _form_page(
    request: Request,
    *,
    mode: str,
    draft: dict,
    errors: dict[str, str],
    kind: str = "",
    webhook: dict | None = None,
    google_connect: dict | None = None,
) -> HTMLResponse:
    translate = _t(request)
    context = _form_context(
        mode, draft, errors, kind=kind, webhook=webhook, translate=translate
    )
    if google_connect is None:
        context["google_connect"] = None
    else:
        context["google_connect"] = {
            "href": google_connect["href"],
            "label": _sentence(request, google_connect["label"]),
        }
    return _page(request, "source_page.html", context)


def _form_fragment(
    request: Request,
    *,
    mode: str,
    draft: dict,
    errors: dict[str, str],
    kind: str = "",
) -> HTMLResponse:
    return _render(
        request,
        "source_form.html",
        _form_context(mode, draft, errors, kind=kind, translate=_t(request)),
    )


def _form_context(
    mode: str,
    draft: dict,
    errors: dict[str, str],
    *,
    kind: str = "",
    webhook: dict | None = None,
    translate,
) -> dict:
    editing = mode == "edit"
    webhook_mode = mode == "webhook"
    ics_mode = mode == "ics"
    google_mode = mode == "google"
    if webhook_mode:
        title = translate("action.add_webhook")
        action = "/sources?kind=webhook"
        kind_code = "webhook"
    elif ics_mode:
        title = translate("action.add_ics")
        action = "/sources?kind=ics"
        kind_code = "ics"
    elif google_mode:
        title = translate("action.add_google")
        action = "/sources?kind=google"
        kind_code = "google"
    elif editing:
        title = translate("action.edit_source")
        action = f"/sources/{draft['id']}"
        kind_code = kind or "private"
    else:
        title = translate("action.add_source")
        action = "/sources"
        kind_code = "private"
    submit_label = translate("action.save") if editing else title
    return {
        "title": title,
        "heading": title,
        "submit_label": submit_label,
        "action": action,
        "mode": mode,
        "kind": kind_code,
        "ics": ics_mode or (editing and kind == "ics"),
        "google": google_mode or (editing and kind == "google"),
        "draft": draft,
        "errors": errors,
        "webhook_url": "" if webhook is None else webhook["url"],
        "webhook_secret": "",
        "source_id": draft["id"],
        "error": "",
        "workplace": False,
        "source": None,
        "saved": False,
        "notice": "",
        "banner": "",
        "sync_error": "",
        "replaced": False,
    }


def _created_workspace(request: Request, source_id: str, secret: str) -> HTMLResponse:
    response = _source_main(request, source_id, webhook_secret=secret)
    response.headers["Cache-Control"] = "no-store"
    response.headers["HX-Retarget"] = "main"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Replace-Url"] = f"/sources/{source_id}"
    return response


def _saved(request: Request, source_id: str) -> HTMLResponse:
    response = _source_main(request, source_id, saved=True)
    response.headers["HX-Retarget"] = "main"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


def _after_mutation(request: Request, source_id: str, error: str) -> HTMLResponse:
    if request.query_params.get("view", "") == "source":
        return _source_main(request, source_id, banner=error)
    return _sources_panel(request, error)


def _source_main(
    request: Request,
    source_id: str,
    *,
    banner: str = "",
    saved: bool = False,
    notice: str = "",
    webhook_secret: str = "",
    replaced: bool = False,
) -> HTMLResponse:
    found = get_source(request.app.state.engine, source_id)
    if found is None:
        raise HTTPException(status_code=404)
    return _render(
        request,
        "source_main.html",
        _edit_context(
            request,
            found,
            banner=banner,
            saved=saved,
            notice=notice,
            webhook_secret=webhook_secret,
            replaced=replaced,
        ),
    )


def _edit_context(
    request: Request,
    found: dict,
    *,
    banner: str = "",
    saved: bool = False,
    notice: str = "",
    webhook_secret: str = "",
    replaced: bool = False,
) -> dict:
    context = _form_context(
        "edit",
        _draft(found["name"], found["timezone"], found["id"], source=found),
        _blank_errors(),
        kind=found["kind"],
        webhook=_webhook_view(found),
        translate=_t(request),
    )
    context["google_connect"] = _google_connect(found)
    context["heading"] = found["name"]
    context["title"] = found["name"]
    context["workplace"] = True
    context["source"] = found
    context["saved"] = saved
    context["notice"] = _sentence(request, notice) if notice else ""
    context["banner"] = _sentence(request, banner) if banner else ""
    context["webhook_secret"] = webhook_secret
    context["replaced"] = replaced
    row = next(
        (item for item in _sync_rows(request) if item["id"] == found["id"]), None
    )
    context["sync_error"] = "" if row is None else (row.get("last_error_message") or "")
    return context


def _secret_panel(
    request: Request, source_id: str, secret: str, *, replaced: bool = False
) -> HTMLResponse:
    response = _panel(request, source_id, secret, "", replaced=replaced)
    response.headers["Cache-Control"] = "no-store"
    return response


def _panel(
    request: Request, source_id: str, secret: str, error: str, *, replaced: bool = False
) -> HTMLResponse:
    return _render(
        request,
        "webhook_panel.html",
        {
            "webhook_url": endpoint_url(source_id),
            "webhook_secret": secret,
            "source_id": source_id,
            "error": _sentence(request, error),
            "replaced": replaced,
        },
    )


def _sources_panel(request: Request, error: str) -> HTMLResponse:
    return _render(
        request,
        "sources_panel.html",
        {
            "sources": list_sources(request.app.state.engine),
            "error": _sentence(request, error),
        },
    )


def _draft(
    name: str,
    timezone_name: str,
    source_id: str = "",
    *,
    form: dict | None = None,
    source: dict | None = None,
    kind: str = "",
) -> dict:
    posted = form or {}
    stored = source or {}
    stored_kind = stored.get("kind", kind)
    if form is not None:
        poll = posted.get("poll_seconds", "")
        allow = 1 if posted.get("ics_allow_http") == "1" else 0
        url = posted.get("ics_url", "")
        calendar = posted.get("google_calendar_id", "")
    elif stored_kind == "ics":
        poll = stored.get("poll_seconds", DEFAULT_POLL_SECONDS)
        allow = stored.get("ics_allow_http", 0)
        url = stored.get("ics_url") or ""
        calendar = ""
    elif stored_kind == "google":
        poll = stored.get("poll_seconds", GOOGLE_POLL_SECONDS)
        allow = 0
        url = ""
        calendar = stored.get("google_calendar_id") or "primary"
    else:
        poll = DEFAULT_POLL_SECONDS
        allow = 0
        url = ""
        calendar = "primary" if kind == "google" else ""
    header_set = (
        bool(stored.get("ics_authorization_set"))
        and posted.get("clear_ics_authorization") != "1"
    )
    return {
        "id": source_id or stored.get("id", ""),
        "name": name,
        "timezone": timezone_name,
        "ics_url": url,
        "poll_seconds": poll,
        "ics_allow_http": allow,
        "ics_authorization_set": header_set,
        "google_calendar_id": calendar,
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
        "google_calendar_id": "",
    }


def _errors(failure: SourceFailure, request: Request) -> dict[str, str]:
    return {
        "top": _sentence(request, failure.detail),
        "name": _sentence(request, failure.fields.get("name", "")),
        "timezone": _sentence(request, failure.fields.get("timezone", "")),
        "ics_url": _sentence(request, failure.fields.get("ics_url", "")),
        "poll_seconds": _sentence(request, failure.fields.get("poll_seconds", "")),
        "ics_authorization_header": _sentence(
            request, failure.fields.get("ics_authorization_header", "")
        ),
        "ics_allow_http": _sentence(request, failure.fields.get("ics_allow_http", "")),
        "google_calendar_id": _sentence(
            request, failure.fields.get("google_calendar_id", "")
        ),
    }


def _create_mode(kind: str) -> str:
    if kind == "webhook":
        return "webhook"
    if kind == "ics":
        return "ics"
    if kind == "google":
        return "google"
    return "create"


def _chosen_kind(kind: str) -> str:
    if kind == "webhook":
        return "webhook"
    if kind == "ics":
        return "ics"
    if kind == "google":
        return "google"
    return "private"


def _kind_values(
    form: dict[str, str], kind: str, *, create: bool
) -> tuple[dict, SourceFailure | None]:
    if kind == "ics":
        return _ics_values(form, create=create)
    if kind == "google":
        return _google_values(form, create=create)
    return {}, None


def _google_values(
    form: dict[str, str], *, create: bool
) -> tuple[dict, SourceFailure | None]:
    raw_poll = form.get("poll_seconds", "")
    if create and raw_poll.strip() == "":
        poll: int | SourceFailure = GOOGLE_POLL_SECONDS
    else:
        poll = poll_from_form(raw_poll)
    if isinstance(poll, SourceFailure):
        return {}, poll
    return {
        "google_calendar_id": form.get("google_calendar_id", ""),
        "poll_seconds": poll,
    }, None


def _google_connect(found: dict) -> dict | None:
    if found.get("kind") != "google":
        return None
    return {
        "href": f"/api/oauth/google/start?source_id={found['id']}",
        "label": "action.reconnect_google"
        if found.get("google_connected")
        else "action.connect_google",
    }


def _ics_values(
    form: dict[str, str], *, create: bool
) -> tuple[dict, SourceFailure | None]:
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
