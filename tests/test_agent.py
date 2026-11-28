"""Agent HTTP API, PATs, and MCP mount."""

from fastapi.testclient import TestClient
from tests.helpers import JSON

from app.services.agent import create_token


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


def test_agent_requires_bearer(hub_client) -> None:
    client = hub_client
    response = client.get("/api/agent/v1/calendars")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_agent_scope_and_crud(hub_client, fake_radicale) -> None:
    client, fake = hub_client, fake_radicale
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


def test_agent_rejects_ics_writes(hub_client) -> None:
    client = hub_client
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


def test_agent_list_default_window_echo(hub_client) -> None:
    client = hub_client
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


def test_lazy_backfill_maps_caldav_object(hub_client, fake_radicale) -> None:
    client, fake = hub_client, fake_radicale
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


def test_admin_tokens_page_and_revoke(hub_client) -> None:
    client = hub_client
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


def test_mcp_requires_bearer_and_lists_tools(hub_client) -> None:
    client = hub_client
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


def test_agent_patch_search_fields_and_confirm(hub_client, fake_radicale) -> None:
    client, fake = hub_client, fake_radicale
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
        fields="standard",
    )
    assert isinstance(searched, dict)
    assert searched["q"] == "Beta"
    assert searched["fields"] == "standard"
    assert searched["events"]
    assert "rrule" in searched["events"][0]

    bad = client.delete(
        f"/api/agent/v1/events/{event_id}",
        headers=auth,
        params={"confirm_token": "nope"},
    )
    assert bad.status_code == 422


def test_expand_weekly_occurrences(hub_client, fake_radicale) -> None:
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
        dtstamp="20320601T000000Z",
        rrules=("FREQ=DAILY;COUNT=3",),
    )
    obj = CalendarObject(uid="series-1", version="1", components=(master,))
    found = expand_object(
        obj,
        datetime(2032, 6, 1, tzinfo=UTC),
        datetime(2032, 6, 10, tzinfo=UTC),
    )
    assert len(found) == 3
    assert found[0].is_occurrence is True


def test_expand_all_day_rrule(hub_client, fake_radicale) -> None:
    from datetime import UTC, datetime

    from app.services.agent.expand import expand_object
    from app.sync.ical_types import CalendarEvent, CalendarObject

    master = CalendarEvent(
        uid="allday-1",
        summary="Holiday",
        description="",
        location="",
        status=None,
        start_value="20320601",
        end_value="20320602",
        all_day=True,
        timezone=None,
        dtstamp="20320601T000000Z",
        rrules=("FREQ=DAILY;COUNT=3",),
    )
    obj = CalendarObject(uid="allday-1", version="1", components=(master,))
    found = expand_object(
        obj,
        datetime(2032, 6, 1, tzinfo=UTC),
        datetime(2032, 6, 10, tzinfo=UTC),
    )
    assert len(found) == 3
    assert found[0].all_day is True
    assert found[0].start == "2032-06-01"


def test_mcp_tools_call_services(hub_client) -> None:
    client = hub_client
    source = _private(client)
    from app.mcp import server as mcp_server
    from app.services.agent.auth import (
        VerifiedToken,
        bind_current_token,
        reset_current_token,
    )

    handle = bind_current_token(
        VerifiedToken(
            id="mcp-test",
            name="mcp",
            scopes=frozenset({"calendars:read", "calendars:write"}),
        )
    )
    try:
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
        reset_current_token(handle)


def test_mcp_read_only_cannot_write(hub_client) -> None:
    client = hub_client
    source = _private(client)
    from app.mcp import server as mcp_server
    from app.services.agent.auth import (
        VerifiedToken,
        bind_current_token,
        reset_current_token,
    )

    handle = bind_current_token(
        VerifiedToken(id="ro", name="ro", scopes=frozenset({"calendars:read"}))
    )
    try:
        created = mcp_server.create_event_tool(
            {
                "calendar": source["id"],
                "summary": "Nope",
                "start": "2033-01-02T10:00:00",
                "end": "2033-01-02T11:00:00",
                "timezone": "UTC",
            }
        )
        assert created["error"] == "forbidden"
        assert created["status"] == 403
    finally:
        reset_current_token(handle)


def test_agent_rejects_bad_timezone_and_all_day_flip(hub_client) -> None:
    client = hub_client
    source = _private(client)
    token = _bearer(client.app.state.engine)
    auth = {"Authorization": f"Bearer {token}", **JSON}
    bad = client.post(
        "/api/agent/v1/events",
        headers=auth,
        json={
            "calendar": source["id"],
            "summary": "Bad tz",
            "start": "2034-01-01T10:00:00",
            "end": "2034-01-01T11:00:00",
            "timezone": 123,
        },
    )
    assert bad.status_code == 422
    created = client.post(
        "/api/agent/v1/events",
        headers=auth,
        json={
            "calendar": source["id"],
            "summary": "Flip",
            "start": "2034-01-01T10:00:00",
            "end": "2034-01-01T11:00:00",
            "timezone": "UTC",
        },
    )
    assert created.status_code == 200, created.text
    event_id = created.json()["id"]
    flipped = client.patch(
        f"/api/agent/v1/events/{event_id}",
        headers=auth,
        json={"all_day": True},
    )
    assert flipped.status_code == 422
    ordered = client.post(
        "/api/agent/v1/events",
        headers=auth,
        json={
            "calendar": source["id"],
            "summary": "Order",
            "start": "2034-01-02T12:00:00",
            "end": "2034-01-02T11:00:00",
            "timezone": "UTC",
        },
    )
    assert ordered.status_code == 422


def test_patch_preserves_exdate_and_exceptions(hub_client, fake_radicale) -> None:
    client, fake = hub_client, fake_radicale
    source = _private(client)
    token = _bearer(client.app.state.engine)
    auth = {"Authorization": f"Bearer {token}", **JSON}
    created = client.post(
        "/api/agent/v1/events",
        headers=auth,
        json={
            "calendar": source["id"],
            "summary": "Series",
            "start": "2035-03-01T09:00:00",
            "end": "2035-03-01T10:00:00",
            "timezone": "UTC",
            "rrule": "FREQ=DAILY;COUNT=5",
        },
    )
    assert created.status_code == 200, created.text
    event_id = created.json()["id"]
    path = next(iter(fake.events))
    fake.events[path] = (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:keep-series\r\n"
        b"DTSTAMP:20350301T000000Z\r\n"
        b"DTSTART:20350301T090000Z\r\n"
        b"DTEND:20350301T100000Z\r\n"
        b"SUMMARY:Series\r\n"
        b"RRULE:FREQ=DAILY;COUNT=5\r\n"
        b"EXDATE:20350303T090000Z\r\n"
        b"END:VEVENT\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:keep-series\r\n"
        b"RECURRENCE-ID:20350302T090000Z\r\n"
        b"DTSTAMP:20350301T000000Z\r\n"
        b"DTSTART:20350302T110000Z\r\n"
        b"DTEND:20350302T120000Z\r\n"
        b"SUMMARY:Moved\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    from sqlalchemy.orm import Session

    from app.db.models import EventMapping

    with Session(client.app.state.engine) as session, session.begin():
        row = session.get(EventMapping, event_id)
        assert row is not None
        row.ical_uid = "keep-series"
    # Path must match ical uid used by update_event.
    old_path = path
    new_path = old_path.rsplit("/", 1)[0] + "/keep-series.ics"
    fake.events[new_path] = fake.events.pop(old_path)

    patched = client.patch(
        f"/api/agent/v1/events/{event_id}",
        headers=auth,
        json={"summary": "Series2"},
    )
    assert patched.status_code == 200, patched.text
    stored = fake.events[new_path].decode("utf-8")
    assert "EXDATE" in stored
    assert "RECURRENCE-ID" in stored
    assert "Series2" in stored


def test_confirm_mismatch_keeps_token(hub_client, fake_radicale) -> None:
    from app.services.agent.confirm import ConfirmStore

    store = ConfirmStore(ttl=60)
    token, _expires = store.issue("event-a", {"id": "event-a"})
    assert store.consume(token, "event-b") is None
    assert store.consume(token, "event-a") is not None


def test_rate_limiter_does_not_extend_ban(hub_client, fake_radicale) -> None:
    from app.services.rate_limit import RateLimiter

    limiter = RateLimiter(limit=2, window=60.0)
    assert limiter.allow("k", now=100.0) is True
    assert limiter.allow("k", now=101.0) is True
    assert limiter.allow("k", now=102.0) is False
    assert limiter.allow("k", now=103.0) is False
    assert limiter.allow("k", now=161.0) is True


def test_search_echoes_default_scan_window(hub_client) -> None:
    client = hub_client
    _private(client)
    from app.services.agent import list_or_search_events

    searched = list_or_search_events(
        client.app.state.engine,
        client.app.state.radicale,
        mode="search",
        q="nothing",
        window_from=None,
        window_to=None,
        calendars=None,
        limit=10,
        cursor=None,
        fields="compact",
    )
    assert isinstance(searched, dict)
    assert searched["applied_defaults"]["window"] == "±366d"
    assert searched["window"] is not None


def test_revoke_token_blocks_agent(hub_client) -> None:
    client = hub_client
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
