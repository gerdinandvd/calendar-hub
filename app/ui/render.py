"""Jinja rendering, language cookies, and thin page adapters."""

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.health import report as health_report
from app.i18n import present, translator
from app.services.sources.sync_ops import list_sync_status

_LANGS = frozenset({"en", "nl"})
_LANG_COOKIE = "hub_lang"

_ADMIN = Path(__file__).resolve().parents[1] / "admin"
templates = Jinja2Templates(directory=_ADMIN)


def _health_view(request: Request) -> dict[str, object]:
    report = health_report(request.app.state.engine, request.app.state.radicale)
    shown = dict(report)
    for key in ("radicale_error", "litestream_error", "kopia_error"):
        shown[key] = _sentence(request, str(report.get(key) or ""))
    free = report["disk_free_bytes"]
    if not isinstance(free, int):
        raise TypeError("disk_free_bytes")
    shown["disk_free"] = _format_bytes(free)
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
