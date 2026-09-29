"""Tests for inferrer.py. Run from repo root: pytest -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inferrer import infer_all, infer_endpoint, infer_params, infer_schema  # noqa: E402
from models import EndpointSchema  # noqa: E402
from normalizer import group_by_endpoint  # noqa: E402
from parser import read_logs  # noqa: E402

SAMPLE = ROOT / "sample_logs.jsonl"
UUID = "598336e3-75d6-4ed4-ab1f-a9f2d10bd1d0"


def entry(method="GET", path="/x", status=200, query=None, req=None, resp=None, ts="2026-09-01T09:00:00Z"):
    return {"timestamp": ts, "method": method, "path": path, "query": query or {},
            "request_body": req, "status": status, "response_body": resp, "headers": {}}


# ---------- infer_schema ----------

@pytest.mark.parametrize("samples", [[], [None], [None, None]])
def test_infer_schema_empty_returns_none(samples):
    assert infer_schema(samples) is None


def test_infer_schema_no_dollar_schema():
    assert "$schema" not in infer_schema([{"a": 1}])


def test_infer_schema_required_vs_optional():
    s = infer_schema([{"id": 1, "name": "a", "phone": "x"}, {"id": 2, "name": "b"}])
    assert s["type"] == "object"
    assert set(s["properties"]) == {"id", "name", "phone"}
    assert sorted(s["required"]) == ["id", "name"]


def test_infer_schema_types():
    s = infer_schema([{"i": 1, "f": 1.5, "b": True, "s": "x", "l": ["a"], "o": {"k": 1}}])
    p = s["properties"]
    assert p["i"]["type"] == "integer"
    assert p["f"]["type"] == "number"
    assert p["b"]["type"] == "boolean"
    assert p["s"]["type"] == "string"
    assert p["l"] == {"type": "array", "items": {"type": "string"}}
    assert p["o"]["properties"]["k"]["type"] == "integer"


def test_infer_schema_int_and_float_merge_to_number():
    s = infer_schema([{"price": 10}, {"price": 10.5}])
    assert s["properties"]["price"]["type"] == "number"


def test_infer_schema_sometimes_null_field():
    s = infer_schema([{"notes": None}, {"notes": "hi"}])
    assert set(s["properties"]["notes"]["type"]) == {"string", "null"}
    assert s["required"] == ["notes"]


def test_infer_schema_nested_array_required():
    s = infer_schema([{"items": [{"a": 1, "b": 2}, {"a": 3}]}])
    item = s["properties"]["items"]["items"]
    assert item["required"] == ["a"]


def test_infer_schema_skips_none_samples():
    assert infer_schema([None, {"a": 1}]) == infer_schema([{"a": 1}])


# ---------- infer_params ----------

def test_path_param_integer():
    params = infer_params("/users/{id}", [entry(path="/users/1"), entry(path="/users/22")])
    assert len(params) == 1
    p = params[0]
    assert (p.name, p.location, p.required, p.schema) == ("id", "path", True, {"type": "integer"})


def test_path_param_uuid():
    [p] = infer_params("/products/{id}", [entry(path=f"/products/{UUID}")])
    assert p.schema == {"type": "string", "format": "uuid"}


def test_multiple_path_params_in_template_order():
    params = infer_params("/users/{userId}/orders/{orderId}",
                          [entry(path=f"/users/5/orders/{UUID}")])
    assert [(p.name, p.schema.get("format", p.schema["type"])) for p in params] == \
        [("userId", "integer"), ("orderId", "uuid")]


def test_query_param_types_and_required():
    entries = [
        entry(query={"page": "1", "limit": "10", "q": "abc", "active": "true", "min": "1.5"}),
        entry(query={"page": "2", "q": "5", "active": "False", "min": "3"}),
    ]
    params = {p.name: p for p in infer_params("/x", entries)}
    assert all(p.location == "query" for p in params.values())
    assert params["page"].schema == {"type": "integer"} and params["page"].required
    assert params["limit"].schema == {"type": "integer"} and not params["limit"].required
    assert params["q"].schema == {"type": "string"} and params["q"].required
    assert params["active"].schema == {"type": "boolean"}
    assert params["min"].schema == {"type": "number"}


def test_no_params():
    assert infer_params("/health", [entry(path="/health")]) == []


# ---------- infer_endpoint ----------

def test_infer_endpoint_basic():
    entries = [
        entry("post", "/users", 201, req={"name": "a", "email": "e"}, resp={"id": 1, "name": "a"},
              ts="2026-09-01T09:00:02Z"),
        entry("post", "/users", 201, req={"name": "b"}, resp={"id": 2, "name": "b"},
              ts="2026-09-01T09:00:01Z"),
        entry("post", "/users", 400, req={"bogus": 1}, resp={"error": "bad"},
              ts="2026-09-01T09:00:03Z"),
        entry("post", "/users", 204, req={"name": "c"}, resp=None),
    ]
    ep = infer_endpoint("post", "/users", entries)
    assert isinstance(ep, EndpointSchema)
    assert ep.method == "POST" and ep.path_template == "/users"
    # request schema only from 2xx -> "bogus" from the 400 must not appear
    assert set(ep.request_schema["properties"]) == {"name", "email"}
    assert ep.request_schema["required"] == ["name"]
    assert list(ep.responses) == [201, 204, 400]
    assert ep.responses[204] is None
    assert sorted(ep.responses[201]["required"]) == ["id", "name"]
    assert ep.examples == {201: {"id": 1, "name": "a"}, 400: {"error": "bad"}}
    assert ep.sample_count == 4
    assert ep.first_seen == "2026-09-01T09:00:00Z"
    assert ep.last_seen == "2026-09-01T09:00:03Z"


def test_infer_endpoint_no_request_body():
    ep = infer_endpoint("GET", "/users/{id}", [entry(path="/users/1", resp={"id": 1})])
    assert ep.request_schema is None
    assert ep.params[0].name == "id"


# ---------- infer_all ----------

def test_infer_all_sorted_and_skips_empty():
    grouped = {
        ("POST", "/b"): [entry("POST", "/b")],
        ("GET", "/b"): [entry("GET", "/b")],
        ("GET", "/a"): [entry("GET", "/a")],
        ("GET", "/empty"): [],
    }
    assert [(e.path_template, e.method) for e in infer_all(grouped)] == \
        [("/a", "GET"), ("/b", "GET"), ("/b", "POST")]


def test_infer_all_empty():
    assert infer_all({}) == []


# ---------- against sample_logs.jsonl ----------

@pytest.fixture(scope="module")
def sample_eps() -> dict[tuple[str, str], EndpointSchema]:
    eps = infer_all(group_by_endpoint(read_logs(SAMPLE)))
    return {(e.method, e.path_template): e for e in eps}


def test_sample_endpoints(sample_eps):
    assert set(sample_eps) == {
        ("GET", "/users"), ("POST", "/users"), ("GET", "/users/{id}"),
        ("GET", "/products"), ("GET", "/products/{id}"),
        ("POST", "/orders"), ("GET", "/orders/{id}"),
    }
    assert sum(e.sample_count for e in sample_eps.values()) == 200


def test_sample_status_codes(sample_eps):
    assert set(sample_eps[("GET", "/users/{id}")].responses) == {200, 404, 500}
    assert set(sample_eps[("POST", "/orders")].responses) == {201, 400, 500}
    assert set(sample_eps[("GET", "/products")].responses) == {200}


def test_sample_user_schema(sample_eps):
    s = sample_eps[("GET", "/users/{id}")].responses[200]
    assert s["properties"]["id"]["type"] == "integer"
    assert s["properties"]["is_active"]["type"] == "boolean"
    assert {"id", "name", "email"} <= set(s["required"])
    assert "phone" in s["properties"] and "phone" not in s["required"]  # optional field


def test_sample_path_params(sample_eps):
    [p] = sample_eps[("GET", "/users/{id}")].params
    assert p.schema == {"type": "integer"} and p.required
    [p] = sample_eps[("GET", "/products/{id}")].params
    assert p.schema == {"type": "string", "format": "uuid"}


def test_sample_query_params(sample_eps):
    users = {p.name: p for p in sample_eps[("GET", "/users")].params}
    assert users["page"].schema == {"type": "integer"} and not users["page"].required
    assert users["limit"].schema == {"type": "integer"}
    products = {p.name: p for p in sample_eps[("GET", "/products")].params}
    assert products["category"].schema == {"type": "string"}
    assert products["in_stock"].schema == {"type": "boolean"}


def test_sample_request_schema(sample_eps):
    req = sample_eps[("POST", "/orders")].request_schema
    assert {"user_id", "items", "shipping_address"} <= set(req["required"])
    assert "coupon_code" in req["properties"] and "coupon_code" not in req["required"]
    assert sample_eps[("GET", "/orders/{id}")].request_schema is None


def test_sample_nullable_field(sample_eps):
    notes = sample_eps[("POST", "/orders")].responses[201]["properties"]["notes"]
    assert set(notes["type"]) == {"string", "null"}


def test_sample_examples_and_timestamps(sample_eps):
    for ep in sample_eps.values():
        assert set(ep.examples) <= set(ep.responses)
        for status, schema in ep.responses.items():
            if schema is not None:
                assert status in ep.examples
        assert ep.first_seen and ep.last_seen and ep.first_seen <= ep.last_seen


# ---------- query params come from successful requests ----------

def _q(code: int, **query) -> dict:
    return {"timestamp": "2026-09-29T09:00:00Z", "method": "GET", "path": "/applications", "query": query,
            "request_body": None, "status": code, "response_body": {"items": []}, "headers": {}}


def test_query_param_type_ignores_rejected_requests():
    """?limit=all got a 400; it must not turn the documented type into string."""
    params = {p.name: p for p in infer_params("/applications", [_q(200, limit="10"), _q(200, limit="5"),
                                                                _q(400, limit="all")])}
    assert params["limit"].schema == {"type": "integer"}


def test_query_param_required_counted_over_successful_requests():
    params = {p.name: p for p in infer_params("/applications", [_q(200, status="approved"), _q(200, status="issued"),
                                                                _q(400)])}
    assert params["status"].required is True


def test_query_param_only_in_failed_requests_is_still_listed():
    params = {p.name: p for p in infer_params("/applications", [_q(200), _q(400, bogus="x")])}
    assert params["bogus"].schema == {"type": "string"} and params["bogus"].required is False


def test_query_params_without_any_2xx_use_all_entries():
    params = {p.name: p for p in infer_params("/applications", [_q(404, limit="3"), _q(404, limit="4")])}
    assert params["limit"].schema == {"type": "integer"} and params["limit"].required is True
