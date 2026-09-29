"""Tests for the passport demo service (demo_app): endpoints, validation, v2 mode and the JSON-line logging."""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo_app import create_app  # noqa: E402
from demo_app.logging_middleware import mask_header  # noqa: E402
from normalizer import is_id_segment, normalize_path  # noqa: E402
from parser import parse_line  # noqa: E402

TOKEN = "Bearer s3cr3t-token-abc.def.ghi"


def v1_body(**over) -> dict:
    body = {"full_name": "Ana Silva", "date_of_birth": "1990-05-01", "nationality": "PT",
            "email": "ana.silva@example.com", "office_id": "OFF-LON"}
    body.update(over)
    return body


def v2_body(**over) -> dict:
    body = {"full_name": "Ana Silva", "birth_date": "1990-05-01", "nationality": "PT",
            "email": "ana.silva@example.com", "office_id": "OFF-LON",
            "emergency_contact": {"name": "Rui Silva", "phone": "+351 912 345 678"}}
    body.update(over)
    return body


@pytest.fixture
def log(tmp_path) -> Path:
    return tmp_path / "logs" / "app.jsonl"


@pytest.fixture
def client(log) -> TestClient:
    return TestClient(create_app(v2=False, log_path=log, error_rate=0, seed=1))


@pytest.fixture
def client_v2(log) -> TestClient:
    return TestClient(create_app(v2=True, log_path=log, error_rate=0, seed=1))


def lines(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def fields(resp) -> set[str]:
    return {d["field"] for d in resp.json()["details"]}


# ---------- endpoints ----------

def test_offices(client):
    r = client.get("/offices")
    assert r.status_code == 200
    offices = r.json()
    assert len(offices) >= 3
    assert {"id", "name", "city", "country", "services"} <= set(offices[0])


def test_create_and_get_v1(client):
    r = client.post("/applications", json=v1_body(phone="+44 7700 900123",
                                                   address={"street": "1 High St", "city": "London",
                                                            "postal_code": "SW1A 1AA", "country": "GB"}))
    assert r.status_code == 201
    app = r.json()
    assert re.fullmatch(r"PA-\d{6}", app["id"]) and is_id_segment(app["id"])
    assert app["status"] == "submitted" and app["date_of_birth"] == "1990-05-01"
    assert "birth_date" not in app and "emergency_contact" not in app
    assert app["office"] == {"id": "OFF-LON", "name": "London Passport Office", "city": "London"}
    assert app["status_history"][0]["status"] == "submitted"
    got = client.get(f"/applications/{app['id']}")
    assert got.status_code == 200 and got.json() == app


def test_seeded_applications_exist(client):
    r = client.get("/applications/PA-000001")
    assert r.status_code == 200
    listing = client.get("/applications", params={"limit": 100}).json()
    assert listing["total"] == 25 and len(listing["items"]) == 25


def test_optional_fields_are_omitted_not_null(client):
    app = client.post("/applications", json=v1_body()).json()
    assert "phone" not in app and "address" not in app and "previous_passport_number" not in app


@pytest.mark.parametrize("change, bad_field", [
    ({"full_name": None}, "full_name"),
    ({"email": "ana at example.com"}, "email"),
    ({"date_of_birth": (date.today() + timedelta(days=5)).isoformat()}, "date_of_birth"),
    ({"date_of_birth": "01/05/1990"}, "date_of_birth"),
    ({"nationality": "Portugal"}, "nationality"),
    ({"office_id": "OFF-XXX"}, "office_id"),
    ({"office_id": "OFF-MAN", "passport_type": "express"}, "passport_type"),
    ({"pages": 40}, "pages"),
    ({"phone": "call me"}, "phone"),
    ({"address": {"street": "x", "city": "y"}}, "address.postal_code"),
])
def test_validation_errors_are_400(client, change, bad_field):
    body = v1_body(**change)
    body = {k: v for k, v in body.items() if v is not None}
    r = client.post("/applications", json=body)
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error"
    assert bad_field in fields(r)


def test_malformed_json_is_400(client):
    r = client.post("/applications", content=b'{"full_name": ', headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and fields(r) == {"body"}


def test_missing_records_are_404(client):
    assert client.get("/applications/PA-999999").status_code == 404
    r = client.patch("/applications/PA-999999/status", json={"status": "in_review"})
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_list_filters(client):
    r = client.get("/applications", params={"status": "submitted", "limit": 5})
    assert r.status_code == 200
    data = r.json()
    assert data["limit"] == 5 and len(data["items"]) <= 5
    assert all(i["status"] == "submitted" for i in data["items"])
    assert set(data["items"][0]) == {"id", "status", "full_name", "office_id", "submitted_at"}
    by_office = client.get("/applications", params={"office_id": "OFF-LON"}).json()
    assert all(i["office_id"] == "OFF-LON" for i in by_office["items"])


@pytest.mark.parametrize("params", [{"status": "pending"}, {"limit": 0}, {"limit": 101}, {"limit": "all"}])
def test_bad_list_filters_are_400(client, params):
    assert client.get("/applications", params=params).status_code == 400


def test_status_workflow(client):
    app_id = client.post("/applications", json=v1_body()).json()["id"]
    r = client.patch(f"/applications/{app_id}/status", json={"status": "in_review", "note": "docs ok"})
    assert r.status_code == 200 and r.json()["status"] == "in_review"
    assert r.json()["status_history"][-1] == {"status": "in_review", "changed_at": r.json()["updated_at"], "note": "docs ok"}
    conflict = client.patch(f"/applications/{app_id}/status", json={"status": "issued"})
    assert conflict.status_code == 409
    assert conflict.json()["current_status"] == "in_review" and conflict.json()["allowed"] == ["approved", "rejected"]
    assert client.patch(f"/applications/{app_id}/status", json={"status": "done"}).status_code == 400
    assert client.patch(f"/applications/{app_id}/status", json={"state": "approved"}).status_code == 400


# ---------- v2 ----------

def test_v2_requires_emergency_contact_and_renames_birth_date(client_v2):
    old = client_v2.post("/applications", json=v1_body())         # an un-upgraded client
    assert old.status_code == 400
    assert {"birth_date", "emergency_contact"} <= fields(old)

    r = client_v2.post("/applications", json=v2_body())
    assert r.status_code == 201
    app = r.json()
    assert app["birth_date"] == "1990-05-01" and "date_of_birth" not in app
    assert app["emergency_contact"] == {"name": "Rui Silva", "phone": "+351 912 345 678"}


def test_v2_validates_emergency_contact(client_v2):
    r = client_v2.post("/applications", json=v2_body(emergency_contact={"name": "R", "phone": "nope"}))
    assert r.status_code == 400
    assert {"emergency_contact.name", "emergency_contact.phone"} <= fields(r)


def test_v2_representation_applies_to_existing_records(client_v2):
    app = client_v2.get("/applications/PA-000001").json()
    assert "birth_date" in app and "date_of_birth" not in app and "emergency_contact" in app


def test_v2_from_env_var(monkeypatch, log):
    monkeypatch.setenv("PASSPORT_API_VERSION", "2")
    c = TestClient(create_app(log_path=log, error_rate=0))
    assert c.get("/meta").json()["version"] == 2
    assert c.post("/applications", json=v2_body()).status_code == 201
    monkeypatch.setenv("PASSPORT_API_VERSION", "1")
    assert TestClient(create_app(log_path=log, error_rate=0)).get("/meta").json()["version"] == 1


# ---------- 500s ----------

def test_random_500s(log):
    c = TestClient(create_app(log_path=log, error_rate=1.0, seed=3))
    r = c.get("/offices")
    assert r.status_code == 500 and r.json()["error"] == "internal_error"
    assert c.get("/").status_code == 200            # the form page is never failed on purpose
    assert [e["status"] for e in lines(log)] == [500]


def test_random_500_rate_is_roughly_right(log):
    c = TestClient(create_app(log_path=log, error_rate=0.1, seed=11))
    statuses = [c.get("/offices").status_code for _ in range(400)]
    assert 20 <= statuses.count(500) <= 65


def test_unhandled_exception_becomes_logged_json_500(client, log):
    client.app.state.store.get = lambda app_id: 1 / 0
    r = client.get("/applications/PA-000001")
    assert r.status_code == 500 and r.json()["error"] == "internal_error"
    assert lines(log)[-1]["status"] == 500 and lines(log)[-1]["response_body"]["error"] == "internal_error"


# ---------- logging ----------

def test_every_api_call_is_one_parseable_line(client, log):
    client.get("/offices")
    client.post("/applications", json=v1_body(), headers={"Authorization": TOKEN})
    client.get("/applications", params={"status": "submitted", "limit": "5"})
    client.get("/applications/PA-999999")
    raw = log.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 4
    for line in raw:
        record = json.loads(line)
        assert set(record) == {"timestamp", "method", "path", "query", "headers", "request_body", "status", "response_body"}
        entry = parse_line(line)
        assert entry is not None, line
    first, post, listing, missing = (parse_line(x) for x in raw)
    assert post["method"] == "POST" and post["status"] == 201
    assert post["request_body"]["full_name"] == "Ana Silva" and post["response_body"]["id"].startswith("PA-")
    assert listing["path"] == "/applications" and listing["query"] == {"status": "submitted", "limit": "5"}
    assert missing["status"] == 404 and normalize_path(missing["path"])[0] == "/applications/{id}"
    assert first["timestamp"].endswith("Z")


def test_authorization_and_other_secrets_are_masked(client, log):
    client.get("/offices", headers={"Authorization": TOKEN, "Cookie": "session=abc123",
                                    "X-API-Key": "key-987", "X-Request-ID": "req-1"})
    client.get("/offices", headers={"Authorization": "raw-token-no-scheme"})
    text = log.read_text(encoding="utf-8")
    for secret in ("s3cr3t", "abc123", "key-987", "raw-token-no-scheme"):
        assert secret not in text
    a, b = lines(log)
    assert a["headers"]["Authorization"] == "Bearer <redacted>"
    assert a["headers"]["Cookie"] == "<redacted>" and a["headers"]["X-Api-Key"] == "<redacted>"
    assert a["headers"]["X-Request-Id"] == "req-1"          # non-secret headers untouched
    assert b["headers"]["Authorization"] == "<redacted>"


def test_mask_header():
    assert mask_header("Authorization", "Bearer abc") == "Bearer <redacted>"
    assert mask_header("authorization", "Basic dXNlcjpwYXNz") == "Basic <redacted>"
    assert mask_header("Authorization", "abc") == "<redacted>"
    assert mask_header("User-Agent", "curl/8") == "curl/8"


def test_ui_and_meta_are_not_logged(client, log):
    assert client.get("/").status_code == 200 and "Passport Application" in client.get("/").text
    assert client.get("/meta").json()["version"] == 1
    assert lines(log) == []


def test_malformed_request_body_logged_as_text(client, log):
    client.post("/applications", content=b"{oops", headers={"Content-Type": "application/json"})
    record = lines(log)[-1]
    assert record["status"] == 400 and record["request_body"] == "{oops"


def test_log_file_can_be_deleted_while_running(client, log):
    client.get("/offices")
    log.unlink()                                   # e.g. `main.py watch --fresh`
    client.get("/offices")
    assert len(lines(log)) == 1
