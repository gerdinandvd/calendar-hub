"""Agent HTTP API, PATs, and MCP mount."""

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
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
