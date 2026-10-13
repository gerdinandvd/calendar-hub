"""Private sources, collections, and the admin pages, without Docker."""

import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import SyncState
from app.main import create_app
from app.radicale.client import RadicaleError

JSON = {"Content-Type": "application/json"}
HX = {"HX-Request": "true"}
SECRET_PASSWORD = "secret-password-value"
INTERNAL_USER = "internal-account-name"


class FakeRadicale:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_create = False

    def create_collection(self, path: str, display_name: str, color: str) -> None:
        self.calls.append(("create", path, display_name, color))
        if self.fail_create:
            raise RadicaleError("The calendar could not be saved.")

    def update_properties(self, path: str, display_name: str, color: str) -> None:
        self.calls.append(("proppatch", path, display_name, color))

    def delete_collection(self, path: str) -> None:
        self.calls.append(("delete", path))


def _open(tmp_path: Path, monkeypatch) -> tuple[TestClient, FakeRadicale]:
    monkeypatch.setenv("HUB_SQLITE_PATH", str(tmp_path / "hub.sqlite"))
    monkeypatch.setenv("HUB_HOST", "localhost:8443")
    monkeypatch.setenv("HUB_USER", "owner")
    monkeypatch.setenv("HUB_PASSWORD", SECRET_PASSWORD)
    monkeypatch.setenv("HUB_INTERNAL_USER", INTERNAL_USER)
    monkeypatch.setenv("HUB_INTERNAL_PASSWORD", "internal-secret-value")
    client = TestClient(create_app())
    client.__enter__()
    fake = FakeRadicale()
    client.app.state.radicale = fake
    return client, fake


def _close(client: TestClient) -> None:
    client.__exit__(None, None, None)


def _create(client: TestClient, name: str, **extra) -> dict:
    body = {"name": name, **extra}
    response = client.post("/api/sources", json=body, headers=JSON)
    assert response.status_code == 201, response.text
    return response.json()


def test_private_source_lifecycle(tmp_path: Path, monkeypatch, caplog) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        rejected = client.post("/api/sources", content=b'{"name":"Home"}')
        assert rejected.status_code == 415
        assert fake.calls == []

        created = _create(client, "Home", enabled=0)
        assert created["kind"] == "private"
        assert created["kind_label"] == "Private"
        assert created["color"] == "#1a73e8"
        assert created["color_name"] == "blue"
        assert created["enabled"] == 0
        assert created["timezone"] == "Europe/Amsterdam"
        assert created["status"] == "na"
        assert created["status_label"] == "not applicable"
        assert created["collection_path"] == f"/owner/{created['id']}/"
        assert created["collection_url"].endswith(created["collection_path"])
        assert "ics_url" not in created
        assert SECRET_PASSWORD not in str(created)
        assert INTERNAL_USER not in str(created)
        assert fake.calls == [("create", created["collection_path"], "Home", "#1a73e8")]

        renamed = client.patch(
            f"/api/sources/{created['id']}",
            json={"name": "Home renamed"},
            headers=JSON,
        )
        assert renamed.status_code == 200
        body = renamed.json()
        assert body["name"] == "Home renamed"
        assert body["collection_path"] == created["collection_path"]
        assert body["color"] == "#1a73e8"
        assert fake.calls[-1] == (
            "proppatch",
            created["collection_path"],
            "Home renamed",
            "#1a73e8",
        )
        assert all(call[0] != "put" for call in fake.calls)

        calls_after_rename = len(fake.calls)
        zone = client.patch(
            f"/api/sources/{created['id']}",
            json={"timezone": "Europe/Berlin"},
            headers=JSON,
        )
        assert zone.status_code == 200
        assert zone.json()["timezone"] == "Europe/Berlin"
        assert len(fake.calls) == calls_after_rename

        enabled = client.patch(
            f"/api/sources/{created['id']}",
            json={"enabled": 1},
            headers=JSON,
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] == 1
        assert [call[0] for call in fake.calls].count("proppatch") == 1

        other = _create(client, "Other")
        assert fake.calls[-1] == (
            "create",
            other["collection_path"],
            "Other",
            "#1a73e8",
        )
        assert created["collection_path"] not in {call[1] for call in fake.calls[-1:]}

        kept = client.delete(f"/api/sources/{created['id']}", headers=JSON)
        assert kept.status_code == 422
        assert kept.json()["error"] == "invalid_request"
        assert client.get(f"/api/sources/{created['id']}").status_code == 200
        assert ("delete", created["collection_path"]) not in fake.calls

        removed = client.delete(
            f"/api/sources/{created['id']}?confirm=true",
            headers=JSON,
        )
        assert removed.status_code == 200
        assert client.get(f"/api/sources/{created['id']}").status_code == 404
        assert ("delete", created["collection_path"]) in fake.calls
        assert ("delete", other["collection_path"]) not in fake.calls
        assert "actor=owner" in caplog.text
        assert f"source_id={created['id']}" in caplog.text
        assert SECRET_PASSWORD not in caplog.text
    finally:
        _close(client)


def test_twenty_first_source_is_refused(tmp_path: Path, monkeypatch) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        for number in range(20):
            _create(client, f"Source {number}")
        extra = client.post("/api/sources", json={"name": "Source 20"}, headers=JSON)
        assert extra.status_code == 422
        assert extra.json()["detail"] == "You can add at most 20 sources."
        listed = client.get("/api/sources").json()["sources"]
        assert len(listed) == 20
        assert len([call for call in fake.calls if call[0] == "create"]) == 20
    finally:
        _close(client)


def test_invalid_source_input_does_not_create_a_collection(
    tmp_path: Path, monkeypatch
) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        cases = [
            {"name": "   "},
            {"name": "Home", "timezone": "Not/AZone"},
            {"name": "Home", "color": "#ffffff"},
            {"name": "Home", "enabled": 2},
        ]
        for body in cases:
            response = client.post("/api/sources", json=body, headers=JSON)
            assert response.status_code == 422, body
        assert fake.calls == []
        assert client.get("/api/sources").json()["sources"] == []

        _create(client, "Home")
        duplicate = client.post("/api/sources", json={"name": "Home"}, headers=JSON)
        assert duplicate.status_code == 422
        assert duplicate.json()["detail"] == "That name is already in use."
        assert len([call for call in fake.calls if call[0] == "create"]) == 1

        fake.fail_create = True
        failed = client.post("/api/sources", json={"name": "Away"}, headers=JSON)
        assert failed.status_code == 500
        assert "Traceback" not in failed.text
        names = [item["name"] for item in client.get("/api/sources").json()["sources"]]
        assert names == ["Home"]
    finally:
        _close(client)


def test_admin_pages_and_field_errors(tmp_path: Path, monkeypatch) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        home = client.get("/")
        assert home.status_code == 200
        assert "This hub keeps sources and sync in one place." in home.text
        assert "There is no source yet." in home.text
        assert "Add a source" in home.text
        assert "Sources" in home.text
        assert "Calendars" in home.text
        assert home.text.count('href="/sources"') == 1
        assert home.text.count('href="/calendars"') == 1
        assert home.text.count('href="/sync"') == 1
        assert home.text.count('href="/health"') == 1
        assert 'href="/" aria-current="page"' in home.text
        assert "Use the password of the hub user." in home.text
        assert "On Android, add this account in DAVx⁵." in home.text
        assert "On iOS, add a CalDAV account in the calendar app." in home.text
        assert (
            "In Thunderbird, add a calendar on the network and choose CalDAV."
            in home.text
        )
        assert SECRET_PASSWORD not in home.text
        assert INTERNAL_USER not in home.text
        assert "Sync now" not in home.text
        assert (
            "hx-on:htmx:after-settle=\"var node = document.getElementById('form-error')"
            " || document.getElementById('form-notice'); if (node) node.focus();\""
            in home.text
        )
        assert 'href="/?lang=en"' in home.text
        assert 'href="/?lang=nl"' in home.text
        assert 'href="http' not in home.text

        blocked = client.post("/sources", data={"name": "Home"})
        assert blocked.status_code == 400
        assert fake.calls == []

        form = client.get("/sources/new")
        assert 'href="/sources/new?lang=en"' in form.text
        hook_form = client.get("/sources/new?kind=webhook")
        assert 'href="/sources/new?kind=webhook&amp;lang=nl"' in hook_form.text
        assert 'for="name"' in form.text
        assert ">Name<" in form.text
        assert ">In this hub<" in form.text
        assert "Time zone" not in form.text
        _assert_labels(form.text)

        missing = client.post("/sources", data={"name": "  "}, headers=HX)
        assert missing.status_code == 200
        assert 'id="form-error"' in missing.text
        assert 'id="name-error"' in missing.text
        assert missing.text.count("Enter a name.") >= 2
        again = client.post("/sources", data={"name": ""}, headers=HX)
        assert "Enter a name." in again.text
        assert fake.calls == []

        created = client.post("/sources", data={"name": "Home"}, headers=HX)
        assert created.status_code == 200
        assert created.headers["hx-redirect"].startswith("/sources/")
        workplace = client.get(created.headers["hx-redirect"])
        assert "Add this URL in your calendar app." in workplace.text
        assert "no sync" in workplace.text
        assert "This calendar lives in the hub. Nothing is fetched." in workplace.text
        page = client.get("/sources")
        assert "Home" in page.text
        assert 'href="/sources" aria-current="page"' in page.text
        assert "In this hub" in page.text
        assert "no sync" in page.text
        assert "Turn off" in page.text
        assert "Edit" in page.text
        assert (
            "This removes the calendar from this hub, including its events."
            in page.text
        )
        assert "Enter a name." not in page.text
        assert "Sync now" not in page.text
        assert page.text.count('name="confirm" value="true"') == 1
        assert 'name="confirm"' not in page.text.split("<dialog")[0]
        _assert_labels(page.text)

        source_id = client.get("/api/sources").json()["sources"][0]["id"]
        bad = client.post(
            f"/sources/{source_id}",
            data={"name": "", "timezone": "Not/AZone"},
            headers=HX,
        )
        assert "Enter a name." in bad.text
        assert "Enter a valid time zone." in bad.text
        assert 'id="name-error"' in bad.text
        assert 'id="timezone-error"' in bad.text
        fixed_name = client.post(
            f"/sources/{source_id}",
            data={"name": "Home", "timezone": "Not/AZone"},
            headers=HX,
        )
        assert "Enter a name." not in fixed_name.text
        assert "Enter a valid time zone." in fixed_name.text
        saved = client.post(
            f"/sources/{source_id}",
            data={"name": "Home", "timezone": "Europe/Amsterdam"},
            headers=HX,
        )
        assert saved.headers["hx-retarget"] == "main"
        assert "Saved." in saved.text

        off = client.post(
            f"/sources/{source_id}/enabled",
            data={"enabled": "0"},
            headers=HX,
        )
        assert "Turn on" in off.text
        assert ">Off<" in off.text
        assert client.get(f"/api/sources/{source_id}").json()["collection_path"]

        calendars = client.get("/calendars")
        assert "blue" in calendars.text
        assert "#1a73e8" in calendars.text
        assert "In this hub" in calendars.text
        assert "Home" in calendars.text
        assert "<form" not in calendars.text

        info = client.get("/api/caldav-info").json()
        assert info["host"] == "localhost:8443"
        assert info["username"] == "owner"
        assert info["collections"][0]["color_name"] == "blue"
        assert "password" not in info
        assert INTERNAL_USER not in str(info)
    finally:
        _close(client)


def test_dutch_ui_keeps_english_json(tmp_path: Path, monkeypatch) -> None:
    client, _fake = _open(tmp_path, monkeypatch)
    try:
        created = _create(client, "Home")
        page = client.get("/sources?lang=nl")
        assert page.status_code == 200
        assert page.cookies.get("hub_lang") == "nl"
        assert 'lang="nl"' in page.text
        assert "In deze hub" in page.text
        assert "geen synchronisatie" in page.text
        assert "Bronnen" in page.text
        fragment = client.post(
            f"/sources/{created['id']}/enabled",
            data={"enabled": "0"},
            headers=HX,
        )
        assert fragment.status_code == 200
        assert "lang" not in fragment.request.url.query.decode()
        assert "In deze hub" in fragment.text
        assert "geen synchronisatie" in fragment.text
        assert "Aanzetten" in fragment.text
        body = client.get("/api/sources").json()["sources"][0]
        assert body["kind_label"] == "Private"
        assert body["status_label"] == "not applicable"
        assert body["color_name"] == "blue"
    finally:
        _close(client)


def test_errors_use_codes_and_keep_stored_sentences(
    tmp_path: Path, monkeypatch
) -> None:
    client, _fake = _open(tmp_path, monkeypatch)
    try:
        missing = client.post("/sources?lang=nl", data={"name": "  "}, headers=HX)
        assert missing.status_code == 200
        assert missing.text.count("Vul een naam in.") >= 2
        assert "sentence.enter_name" not in missing.text
        rejected = client.post("/api/sources", json={"name": "  "}, headers=JSON)
        assert rejected.status_code == 422
        assert rejected.json()["detail"] == "Enter a name."

        coded = _create(
            client, "Coded", kind="ics", ics_url="https://example.com/coded.ics"
        )
        legacy = _create(
            client, "Legacy", kind="ics", ics_url="https://example.com/legacy.ics"
        )
        unknown = _create(
            client, "Unknown", kind="ics", ics_url="https://example.com/unknown.ics"
        )
        with Session(client.app.state.engine) as session, session.begin():
            session.get(
                SyncState, coded["id"]
            ).last_error_message = "sentence.event_saved"
            session.get(
                SyncState, legacy["id"]
            ).last_error_message = "The event could not be saved."
            session.get(
                SyncState, unknown["id"]
            ).last_error_message = "A custom operator note."
        page = client.get("/sync?lang=nl")
        assert page.status_code == 200
        assert page.text.count("De afspraak kon niet worden opgeslagen.") >= 2
        assert "A custom operator note." in page.text
        assert "sentence.event_saved" not in page.text
        rows = {row["name"]: row for row in client.get("/api/sync").json()["sources"]}
        assert rows["Coded"]["last_error_message"] == "sentence.event_saved"
        assert rows["Legacy"]["last_error_message"] == "The event could not be saved."
        assert rows["Unknown"]["last_error_message"] == "A custom operator note."
    finally:
        _close(client)


def _assert_labels(html: str) -> None:
    for field_id in re.findall(r'<input[^>]*\sid="([^"]+)"', html):
        assert f'for="{field_id}"' in html
