"""Tests for mock_proxy.py (B5). Run from repo root: pytest -q"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mock_proxy  # noqa: E402

SPEC = {
    "paths": {
        "/items": {
            "post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "required": ["name"], "properties": {"name": {"type": "string"}},
                }}}},
                "responses": {
                    "201": {"content": {"application/json": {"schema": {"type": "object"}}}},
                    "400": {"content": {"application/json": {
                        "example": {"error": "bad_request", "message": "invalid"}}}},
                },
            },
        },
        "/items/{id}": {
            "get": {
                "security": [{"bearerAuth": []}],
                "responses": {
                    "200": {"content": {"application/json": {"schema": {"type": "object"}}}},
                    "401": {"content": {"application/json": {"example": {"error": "unauthorized"}}}},
                    "404": {"content": {"application/json": {"example": {"error": "not_found"}}}},
                    "500": {"x-inferred": True, "content": {"application/json": {
                        "example": {"error": "server_error"}}}},
                },
            },
        },
    },
}


class FakeUpstreamResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.content = json.dumps(body).encode() if body is not None else b""
        self.headers = {"content-type": "application/json"}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeAsyncClient:
    """Stands in for httpx.AsyncClient so tests never need a real Prism running."""
    response = FakeUpstreamResponse(200, {"id": "abc123", "name": "Widget"})
    calls: list[tuple[str, str]] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, **kw):
        FakeAsyncClient.calls.append((method, url))
        return FakeAsyncClient.response


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Fresh spec file, empty known-ids/spec-cache, and a fake upstream for every test."""
    spec_path = tmp_path / "openapi.yaml"
    spec_path.write_text(yaml.dump(SPEC), encoding="utf-8")
    monkeypatch.setattr(mock_proxy, "SPEC_PATH", spec_path)
    monkeypatch.setattr(mock_proxy, "LOG_PATH", tmp_path / "live.jsonl")
    monkeypatch.setattr(mock_proxy, "_known_ids", {})
    monkeypatch.setattr(mock_proxy, "_spec_cache", {"mtime": None, "spec": {}})
    monkeypatch.setattr(mock_proxy, "CHAOS_RATE", 0.0)  # deterministic unless a test opts back in
    monkeypatch.setattr(mock_proxy.httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.calls = []
    FakeAsyncClient.response = FakeUpstreamResponse(200, {"id": "abc123", "name": "Widget"})
    return spec_path


@pytest.fixture
def client() -> TestClient:
    return TestClient(mock_proxy.app)


def read_log(tmp_path) -> list[dict]:
    p = tmp_path / "live.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ---------- pure helpers ----------

def test_match_operation_finds_template_and_path_params():
    spec = mock_proxy.load_spec()
    result = mock_proxy.match_operation(spec, "GET", "/items/42")
    assert result is not None
    template, op, params = result
    assert template == "/items/{id}"
    assert list(params.values()) == ["42"]  # the group's exact internal name is an implementation detail


def test_match_operation_none_for_unknown_route():
    assert mock_proxy.match_operation(mock_proxy.load_spec(), "GET", "/nope") is None


@pytest.mark.parametrize("template, prefix", [("/items/{id}", "/items"), ("/items", "/items"), ("/", "/")])
def test_resource_prefix(template, prefix):
    assert mock_proxy.resource_prefix(template) == prefix


def test_validation_failure_reports_missing_field():
    op = SPEC["paths"]["/items"]["post"]
    failure = mock_proxy.validation_failure(op, {})
    assert failure["field"] == "name"


def test_validation_failure_none_when_valid():
    op = SPEC["paths"]["/items"]["post"]
    assert mock_proxy.validation_failure(op, {"name": "x"}) is None


def test_error_body_uses_spec_example_when_present():
    op = SPEC["paths"]["/items"]["post"]
    assert mock_proxy.error_body(op, 400, "fallback") == {"error": "bad_request", "message": "invalid"}


def test_error_body_falls_back_when_no_example():
    op = {"responses": {}}
    body = mock_proxy.error_body(op, 500, "boom")
    assert body == {"error": "error", "message": "boom"}


def test_validation_error_body_reflects_the_actual_failing_field_not_a_static_example():
    # Regression test: error_body() alone would return the SAME static spec example regardless of
    # which field actually failed on THIS request — defeating "real validation". Caught by hand while
    # exercising a live demo: two different failures both returned an identical canned body.
    op = SPEC["paths"]["/items"]["post"]  # its 400 example is {"error": "bad_request", "message": "invalid"}
    body = mock_proxy.validation_error_body(op, 400, {"field": "name", "message": "'name' is required"})
    assert body["message"] != "invalid"
    assert "name" in body["message"]
    assert body["error"] == "bad_request"  # still matches the envelope's other keys


def test_validation_error_body_two_different_failures_produce_different_messages():
    op = SPEC["paths"]["/items"]["post"]
    a = mock_proxy.validation_error_body(op, 400, {"field": "name", "message": "'name' is required"})
    b = mock_proxy.validation_error_body(op, 400, {"field": "price", "message": "'price' must be a number"})
    assert a["message"] != b["message"]


def test_validation_error_body_generic_when_no_spec_example():
    op = {"responses": {}}
    body = mock_proxy.validation_error_body(op, 400, {"field": "x", "message": "'x' is required"})
    assert body["field"] == "x" and "x" in body["message"]


def test_is_unknown_id_bootstrap_vs_learned():
    assert mock_proxy.is_unknown_id("/items", "1") is False  # nothing learned yet -> can't judge
    mock_proxy.record_ids("/items", {"id": "1"})
    assert mock_proxy.is_unknown_id("/items", "1") is False
    assert mock_proxy.is_unknown_id("/items", "999") is True


# ---------- the proxy route ----------

def test_unmatched_route_returns_404(client, tmp_path):
    r = client.get("/nowhere")
    assert r.status_code == 404
    assert read_log(tmp_path)[0]["status"] == 404


def test_mock_scenario_header_forces_status(client, tmp_path):
    r = client.get("/items/1", headers={"X-Mock-Scenario": "500", "Authorization": "Bearer x"})
    assert r.status_code == 500
    assert r.json() == {"error": "server_error"}
    assert FakeAsyncClient.calls == []  # never reached Prism


def test_missing_auth_on_secured_endpoint_returns_401(client):
    r = client.get("/items/1")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_auth_present_forwards_to_upstream(client):
    r = client.get("/items/1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"id": "abc123", "name": "Widget"}
    assert FakeAsyncClient.calls == [("GET", mock_proxy.UPSTREAM_URL + "/items/1")]


def test_invalid_request_body_returns_400_naming_the_actual_field(client, tmp_path):
    r = client.post("/items", json={})
    assert r.status_code == 400
    assert "name" in r.json()["message"]  # the field that's ACTUALLY missing from THIS request
    assert read_log(tmp_path)[0]["response_body"] == r.json()


def test_valid_request_body_forwards_to_upstream(client):
    FakeAsyncClient.response = FakeUpstreamResponse(201, {"id": "new1", "name": "x"})
    r = client.post("/items", json={"name": "x"})
    assert r.status_code == 201
    assert FakeAsyncClient.calls[0][0] == "POST"


def test_unknown_id_returns_404_once_a_resource_is_learned(client):
    # first, a successful lookup teaches the proxy that id "1" is real
    r1 = client.get("/items/1", headers={"Authorization": "Bearer x"})
    assert r1.status_code == 200
    # now an id it has never seen for the same resource -> learned 404, not forwarded
    FakeAsyncClient.calls.clear()
    r2 = client.get("/items/999", headers={"Authorization": "Bearer x"})
    assert r2.status_code == 404
    assert r2.json() == {"error": "not_found"}
    assert FakeAsyncClient.calls == []


def test_chaos_disabled_by_default_never_fires(client, monkeypatch):
    monkeypatch.setattr(mock_proxy.random, "random", lambda: 0.0)  # would always fire if enabled
    spec = mock_proxy.load_spec()
    spec["paths"]["/items/{id}"]["get"]["responses"]["500"]["x-observed-rate"] = 1.0
    mock_proxy._spec_cache["spec"] = spec  # inject directly; CHAOS_RATE is 0 either way
    r = client.get("/items/1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200


def test_chaos_enabled_fires_on_high_rate(client, monkeypatch):
    monkeypatch.setattr(mock_proxy, "CHAOS_RATE", 1.0)
    monkeypatch.setattr(mock_proxy.random, "random", lambda: 0.0)
    spec = mock_proxy.load_spec()
    spec["paths"]["/items/{id}"]["get"]["responses"]["500"]["x-observed-rate"] = 1.0
    mock_proxy._spec_cache["spec"] = spec
    mock_proxy._spec_cache["mtime"] = "frozen"  # prevent load_spec() re-reading the file and losing our edit
    r = client.get("/items/1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 500
    assert FakeAsyncClient.calls == []


def test_traffic_is_logged_in_parser_compatible_shape(client, tmp_path):
    client.get("/items/1", headers={"Authorization": "Bearer x"})
    rows = read_log(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) >= {"timestamp", "method", "path", "query", "request_body", "status", "response_body", "headers"}
    assert row["method"] == "GET" and row["path"] == "/items/1" and row["status"] == 200
