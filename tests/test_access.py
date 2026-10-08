"""Mutation requirements and error responses, without Docker."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("HUB_SQLITE_PATH", str(tmp_path / "hub.sqlite"))
    return TestClient(create_app())


def test_json_mutation_without_content_type_does_not_run(tmp_path: Path, monkeypatch) -> None:
    hit = {"value": False}
    with _client(tmp_path, monkeypatch) as client:
        @client.app.post("/api/guard-probe")
        def create_source() -> dict[str, bool]:
            hit["value"] = True
            return {"ok": True}

        rejected = client.post("/api/guard-probe", content=b"name=x")
        assert rejected.status_code == 415
        assert rejected.json() == {
            "error": "unsupported_media_type",
            "detail": "Content-Type must be application/json.",
        }
        assert "Traceback" not in rejected.text
        assert hit["value"] is False
        assert "access-control-allow-origin" not in rejected.headers

        accepted = client.post(
            "/api/guard-probe",
            content=b"{}",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        assert accepted.status_code == 200
        assert hit["value"] is True


def test_html_mutation_without_hx_request_does_not_run(tmp_path: Path, monkeypatch) -> None:
    hit = {"value": False}
    with _client(tmp_path, monkeypatch) as client:
        @client.app.post("/source")
        def update_source() -> dict[str, bool]:
            hit["value"] = True
            return {"ok": True}

        rejected = client.post("/source", content=b"name=x")
        assert rejected.status_code == 400
        assert "HX-Request" in rejected.text
        assert "Traceback" not in rejected.text
        assert hit["value"] is False

        accepted = client.post("/source", headers={"HX-Request": "true"})
        assert accepted.status_code == 200
        assert hit["value"] is True


def test_webhook_is_exempt_from_json_requirement(tmp_path: Path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/webhooks/source-1", content=b"raw")
        assert response.status_code != 415
        assert response.status_code == 404
        assert "Content-Type must be application/json." not in response.text
        assert "Traceback" not in response.text


def test_unexpected_error_hides_cause_and_traceback(tmp_path: Path, monkeypatch, caplog) -> None:
    secret = "super-secret-token"
    with _client(tmp_path, monkeypatch) as client:
        @client.app.get("/api/boom")
        def boom() -> None:
            raise RuntimeError(f"token={secret} summary=SecretTitle")

        with caplog.at_level("ERROR", logger="app"):
            response = client.get("/api/boom")

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error", "detail": "Something went wrong."}
    assert secret not in response.text
    assert "SecretTitle" not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text
    assert secret not in caplog.text
    assert "SecretTitle" not in caplog.text


def test_api_not_found_is_json_without_cors(tmp_path: Path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/api/missing")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert "Traceback" not in response.text
    assert "access-control-allow-origin" not in response.headers
