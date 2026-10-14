"""Source form, edit, and panel presentation helpers."""

from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from app.services.sources import (
    DEFAULT_POLL_SECONDS,
    GOOGLE_POLL_SECONDS,
    SourceFailure,
    get_source,
    list_sources,
)
from app.services.sources.prepare import poll_from_form
from app.services.sources.present import endpoint_url
from app.ui.render import _page, _render, _sentence, _sync_rows, _t


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
