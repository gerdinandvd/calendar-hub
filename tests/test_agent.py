"""Agent HTTP API, PATs, and MCP mount."""

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.mcp.runtime import bind as bind_mcp
from app.services.agent import create_token

JSON = {"Content-Type": "application/json"}
SECRET_KEY = base64.urlsafe_b64encode(b"calendar-hub-local-dev-key-32b!!").decode()


class FakeRadicale:
    def __init__(self) -> None:
        self.events: dict[str, bytes] = {}

    def create_collection(self, path: str, display_name: str, color: str) -> None:
        return None

    def update_properties(self, path: str, display_name: str, color: str) -> None:
        return None

    def delete_collection(self, path: str) -> None:
        return None

    def put_event(self, path: str, body: bytes, *, fresh: bool = False) -> None:
        self.events[path] = body

    def delete_event(self, path: str) -> None:
        self.events.pop(path, None)

    def get_event(self, path: str) -> bytes | None:
        return self.events.get(path)

    def count_events(self, path: str) -> int:
        return len(self.list_event_hrefs(path))

    def list_event_hrefs(self, path: str) -> list[str]:
        prefix = path if path.endswith("/") else f"{path}/"
        return sorted(
            key
            for key in self.events
            if key.startswith(prefix) and key.lower().endswith(".ics")
        )

    def reachable(self) -> bool:
        return True


def _open(tmp_path: Path, monkeypatch) -> tuple[TestClient, FakeRadicale]:
    monkeypatch.setenv("HUB_SQLITE_PATH", str(tmp_path / "hub.sqlite"))
    monkeypatch.setenv("HUB_HOST", "localhost:8443")
    monkeypatch.setenv("HUB_USER", "owner")
    monkeypatch.setenv("HUB_PASSWORD", "secret-password-value")
    monkeypatch.setenv("HUB_INTERNAL_USER", "internal-account-name")
    monkeypatch.setenv("HUB_INTERNAL_PASSWORD", "internal-secret-value")
    monkeypatch.setenv("HUB_SECRET_KEY", SECRET_KEY)
    client = TestClient(create_app())
    client.__enter__()
    fake = FakeRadicale()
    client.app.state.radicale = fake
    bind_mcp(client.app.state.engine, fake, client.app.state.agent_confirms)
    return client, fake


def _close(client: TestClient) -> None:
    client.__exit__(None, None, None)


def _bearer(engine, scopes: list[str] | None = None) -> str:
    _public, plain = create_token(
        engine,
        name="agent",
        scopes=scopes or ["calendars:read", "calendars:write"],
    )
    assert isinstance(plain, str)
    return plain


def _private(client: TestClient) -> dict:
    response = client.post(
        "/api/sources",
        json={"name": "Desk", "kind": "private", "timezone": "UTC"},
        headers=JSON,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_agent_requires_bearer(tmp_path: Path, monkeypatch) -> None:
    client, _fake = _open(tmp_path, monkeypatch)
    try:
        response = client.get("/api/agent/v1/calendars")
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"
    finally:
        _close(client)


def test_agent_scope_and_crud(tmp_path: Path, monkeypatch) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        source = _private(client)
        read_only = _bearer(client.app.state.engine, ["calendars:read"])
        full = _bearer(client.app.state.engine, ["calendars:read", "calendars:write"])

        listed = client.get(
            "/api/agent/v1/calendars",
            headers={"Authorization": f"Bearer {read_only}"},
        )
        assert listed.status_code == 200
        assert listed.json()["calendars"][0]["id"] == source["id"]

        refused = client.post(
            "/api/agent/v1/events",
            headers={"Authorization": f"Bearer {read_only}", **JSON},
            json={
                "calendar": source["id"],
                "summary": "Meet",
                "start": "2031-05-01T09:00:00",
                "end": "2031-05-01T10:00:00",
                "timezone": "UTC",
            },
        )
        assert refused.status_code == 403

        created = client.post(
            "/api/agent/v1/events",
            headers={"Authorization": f"Bearer {full}", **JSON},
            json={
                "calendar": source["id"],
                "summary": "Meet",
                "start": "2031-05-01T09:00:00",
                "end": "2031-05-01T10:00:00",
                "timezone": "UTC",
            },
        )
        assert created.status_code == 200, created.text
        event = created.json()
        assert event["summary"] == "Meet"
        assert event["id"]
        assert fake.events

        window = client.get(
            "/api/agent/v1/events",
            headers={"Authorization": f"Bearer {full}"},
            params={"from": "2031-05-01T00:00:00", "to": "2031-05-02T00:00:00"},
        )
        assert window.status_code == 200
        body = window.json()
        assert body["applied_defaults"] == {}
        assert body["window"]["from"].startswith("2031-05-01")
        assert len(body["events"]) == 1

        preview = client.delete(
            f"/api/agent/v1/events/{event['id']}",
            headers={"Authorization": f"Bearer {full}"},
        )
        assert preview.status_code == 200
        token = preview.json()["confirm_token"]
        assert token
        assert fake.events

        deleted = client.delete(
            f"/api/agent/v1/events/{event['id']}",
            headers={"Authorization": f"Bearer {full}"},
            params={"confirm_token": token},
        )
        assert deleted.status_code == 200
        assert deleted.json()["ok"] is True
        assert not fake.events
    finally:
        _close(client)


def test_agent_rejects_ics_writes(tmp_path: Path, monkeypatch) -> None:
    client, _fake = _open(tmp_path, monkeypatch)
    try:
        response = client.post(
            "/api/sources",
            json={
                "name": "Feed",
                "kind": "ics",
                "ics_url": "https://example.test/cal.ics",
                "poll_seconds": 3600,
            },
            headers=JSON,
        )
        assert response.status_code == 201, response.text
        source = response.json()
        token = _bearer(client.app.state.engine)
        created = client.post(
            "/api/agent/v1/events",
            headers={"Authorization": f"Bearer {token}", **JSON},
            json={
                "calendar": source["id"],
                "summary": "Nope",
                "start": "2031-05-01T09:00:00",
                "end": "2031-05-01T10:00:00",
                "timezone": "UTC",
            },
        )
        assert created.status_code == 422
        assert created.json()["error"] == "invalid_request"
    finally:
        _close(client)


def test_agent_list_default_window_echo(tmp_path: Path, monkeypatch) -> None:
    client, _fake = _open(tmp_path, monkeypatch)
    try:
        _private(client)
        token = _bearer(client.app.state.engine)
        response = client.get(
            "/api/agent/v1/events",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["applied_defaults"]["window"] == "now/+7d"
        assert body["window"] is not None
        assert body["pagination"]["limit"] == 50
    finally:
        _close(client)


def test_lazy_backfill_maps_caldav_object(tmp_path: Path, monkeypatch) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        source = _private(client)
        path = f"{source['collection_path'].rstrip('/')}/caldav-only.ics"
        fake.events[path] = (
            b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
            b"UID:caldav-only\r\nDTSTAMP:20310501T000000Z\r\n"
            b"DTSTART;TZID=UTC:20310503T120000\r\n"
            b"DTEND;TZID=UTC:20310503T130000\r\n"
            b"SUMMARY:Phone\r\nDESCRIPTION:\r\nLOCATION:\r\n"
            b"END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        token = _bearer(client.app.state.engine)
        response = client.get(
            "/api/agent/v1/events",
            headers={"Authorization": f"Bearer {token}"},
            params={"from": "2031-05-01T00:00:00", "to": "2031-05-10T00:00:00"},
        )
        assert response.status_code == 200, response.text
        events = response.json()["events"]
        assert len(events) == 1
        assert events[0]["summary"] == "Phone"
        assert events[0]["id"]
        detail = client.get(
            f"/api/agent/v1/events/{events[0]['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert detail.status_code == 200
        assert detail.json()["summary"] == "Phone"
    finally:
        _close(client)


def test_admin_tokens_page_and_revoke(tmp_path: Path, monkeypatch) -> None:
    client, _fake = _open(tmp_path, monkeypatch)
    try:
        page = client.get("/tokens")
        assert page.status_code == 200
        assert b"Agent tokens" in page.content or b"agent" in page.content.lower()
        created = client.post(
            "/tokens",
            data={"name": "Cursor", "scope": ["calendars:read", "calendars:write"]},
            headers={"HX-Request": "true"},
        )
        assert created.status_code == 200
        assert b"ch_" in created.content
        tokens = client.get("/tokens")
        assert tokens.status_code == 200
    finally:
        _close(client)


def test_mcp_requires_bearer_and_lists_tools(tmp_path: Path, monkeypatch) -> None:
    client, _fake = _open(tmp_path, monkeypatch)
    try:
        denied = client.post("/mcp")
        assert denied.status_code == 401
        token = _bearer(client.app.state.engine)
        # Streamable HTTP initialize is protocol-specific; mount + auth is enough here.
        from app.mcp.server import mcp

        names = {tool.name for tool in mcp._tool_manager.list_tools()}
        assert names == {
            "list_calendars",
            "list_events",
            "search_events",
            "get_event",
            "create_event",
            "update_event",
            "delete_event",
        }
        assert token.startswith("ch_")
    finally:
        _close(client)


def test_agent_patch_search_fields_and_confirm(tmp_path: Path, monkeypatch) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        source = _private(client)
        token = _bearer(client.app.state.engine)
        auth = {"Authorization": f"Bearer {token}"}
        created = client.post(
            "/api/agent/v1/events",
            headers={**auth, **JSON},
            json={
                "calendar": source["id"],
                "summary": "Alpha",
                "description": "Find me later",
                "location": "Room",
                "start": "2032-06-01T09:00:00",
                "end": "2032-06-01T10:00:00",
                "timezone": "UTC",
                "status": "confirmed",
                "rrule": "FREQ=DAILY;COUNT=3",
            },
        )
        assert created.status_code == 200, created.text
        event_id = created.json()["id"]

        patched = client.patch(
            f"/api/agent/v1/events/{event_id}",
            headers={**auth, **JSON},
            json={"summary": "Beta", "description": None, "location": "Hall"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["summary"] == "Beta"

        listed = client.get(
            "/api/agent/v1/events",
            headers=auth,
            params={
                "from": "2032-06-01T00:00:00",
                "to": "2032-06-05T00:00:00",
                "q": "Beta",
                "fields": "standard",
            },
        )
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["fields"] == "standard"
        assert len(body["events"]) >= 1
        assert body["events"][0]["location"] == "Hall"

        from app.services.agent import list_or_search_events

        searched = list_or_search_events(
            client.app.state.engine,
            fake,
            mode="search",
            q="Beta",
            window_from="2032-06-01T00:00:00",
            window_to="2032-06-10T00:00:00",
            calendars=None,
            limit=10,
            cursor=None,
            fields="full",
        )
        assert isinstance(searched, dict)
        assert searched["q"] == "Beta"
        assert searched["fields"] == "full"
        assert searched["events"]

        bad = client.delete(
            f"/api/agent/v1/events/{event_id}",
            headers=auth,
            params={"confirm_token": "nope"},
        )
        assert bad.status_code == 422
    finally:
        _close(client)


def test_expand_weekly_occurrences() -> None:
    from datetime import UTC, datetime

    from app.services.agent.expand import expand_object
    from app.sync.ical_types import CalendarEvent, CalendarObject

    master = CalendarEvent(
        uid="series-1",
        summary="Standup",
        description="",
        location="",
        status=None,
        start_value="20320601T090000",
        end_value="20320601T093000",
        all_day=False,
        timezone="UTC",
        rrule="FREQ=DAILY;COUNT=3",
        dtstamp="20320601T000000Z",
    )
    obj = CalendarObject(uid="series-1", version="1", components=(master,))
    found = expand_object(
        obj,
        datetime(2032, 6, 1, tzinfo=UTC),
        datetime(2032, 6, 10, tzinfo=UTC),
    )
    assert len(found) == 3
    assert found[0].is_occurrence is True


def test_mcp_tools_call_services(tmp_path: Path, monkeypatch) -> None:
    client, _fake = _open(tmp_path, monkeypatch)
    try:
        source = _private(client)
        from app.mcp import server as mcp_server

        calendars = mcp_server.list_calendars()
        assert calendars["calendars"]
        created = mcp_server.create_event_tool(
            {
                "calendar": source["id"],
                "summary": "Via MCP",
                "start": "2033-01-02T10:00:00",
                "end": "2033-01-02T11:00:00",
                "timezone": "UTC",
            }
        )
        assert "id" in created
        listed = mcp_server.list_events(
            window_from="2033-01-01T00:00:00",
            window_to="2033-01-08T00:00:00",
        )
        assert listed["events"]
        got = mcp_server.get_event_tool(created["id"], fields="standard")
        assert got["summary"] == "Via MCP"
        updated = mcp_server.update_event_tool(created["id"], {"summary": "MCP2"})
        assert updated["summary"] == "MCP2"
        preview = mcp_server.delete_event_tool(created["id"])
        assert "confirm_token" in preview
        done = mcp_server.delete_event_tool(
            created["id"], confirm_token=preview["confirm_token"]
        )
        assert done.get("ok") is True
        searched = mcp_server.search_events(
            q="missing-no-hits",
            window_from="2033-01-01T00:00:00",
            window_to="2033-01-08T00:00:00",
        )
        assert searched["events"] == []
        assert "error" not in searched
    finally:
        _close(client)


def test_revoke_token_blocks_agent(tmp_path: Path, monkeypatch) -> None:
    client, _fake = _open(tmp_path, monkeypatch)
    try:
        from app.services.agent import create_token, revoke_token

        public, plain = create_token(
            client.app.state.engine,
            name="temp",
            scopes=["calendars:read", "calendars:write"],
        )
        assert isinstance(public, dict)
        ok = client.get(
            "/api/agent/v1/calendars",
            headers={"Authorization": f"Bearer {plain}"},
        )
        assert ok.status_code == 200
        revoked = revoke_token(client.app.state.engine, public["id"])
        assert isinstance(revoked, dict)
        denied = client.get(
            "/api/agent/v1/calendars",
            headers={"Authorization": f"Bearer {plain}"},
        )
        assert denied.status_code == 401
        page = client.post(
            f"/tokens/{public['id']}/revoke",
            headers={"HX-Request": "true"},
        )
        assert page.status_code == 200
    finally:
        _close(client)
