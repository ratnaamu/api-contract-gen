"""Tests for normalizer.py. Run from repo root: pytest -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from normalizer import group_by_endpoint, id_param_type, is_id_segment, normalize_path  # noqa: E402
from parser import read_logs  # noqa: E402

SAMPLE = ROOT / "sample_logs.jsonl"
UUID = "598336e3-75d6-4ed4-ab1f-a9f2d10bd1d0"


def entry(method: str, path: str) -> dict:
    return {"timestamp": "", "method": method, "path": path, "query": {},
            "request_body": None, "status": 200, "response_body": None, "headers": {}}


# ---------- is_id_segment ----------

@pytest.mark.parametrize("seg", ["12", "0", "1741", UUID, UUID.upper(),
                                 "507f1f77bcf86cd799439011", "usr_000012", "ord-8f3k2a91"])
def test_id_segments(seg):
    assert is_id_segment(seg)


@pytest.mark.parametrize("seg", ["", "users", "me", "orders", "v1", "v2", "api", "search",
                                 "health-check", "abc"])
def test_non_id_segments(seg):
    assert not is_id_segment(seg)


# ---------- id_param_type ----------

def test_id_param_type():
    assert id_param_type(["1", "12", "1741"]) == {"type": "integer"}
    assert id_param_type([UUID, UUID.upper()]) == {"type": "string", "format": "uuid"}
    assert id_param_type(["1", UUID]) == {"type": "string"}
    assert id_param_type(["usr_000012"]) == {"type": "string"}
    assert id_param_type([]) == {"type": "string"}


# ---------- normalize_path ----------

@pytest.mark.parametrize("path, template, params", [
    ("/users/12", "/users/{id}", {"id": "12"}),
    (f"/orders/{UUID}", "/orders/{id}", {"id": UUID}),
    (f"/products/{UUID}", "/products/{id}", {"id": UUID}),
    ("/users", "/users", {}),
    ("/users/", "/users", {}),
    ("/", "/", {}),
    ("", "/", {}),
    ("/users/me", "/users/me", {}),
    ("/users/5/orders/9", "/users/{userId}/orders/{orderId}", {"userId": "5", "orderId": "9"}),
    (f"/categories/3/products/{UUID}", "/categories/{categoryId}/products/{productId}",
     {"categoryId": "3", "productId": UUID}),
    ("/users/12/profile", "/users/{id}/profile", {"id": "12"}),
    ("/api/v1/users/12", "/api/v1/users/{id}", {"id": "12"}),
    ("/users//12/", "/users/{id}", {"id": "12"}),
    ("/users/12?x=1", "/users/{id}", {"id": "12"}),
])
def test_normalize_path(path, template, params):
    assert normalize_path(path) == (template, params)


def test_consecutive_ids_get_unique_names():
    template, params = normalize_path("/files/12/34")
    assert template == "/files/{fileId}/{id}"
    assert params == {"fileId": "12", "id": "34"}


def test_leading_ids_get_unique_names():
    template, params = normalize_path("/12/34")
    assert template == "/{id}/{id2}"
    assert params == {"id": "12", "id2": "34"}


# ---------- group_by_endpoint ----------

def test_group_by_endpoint_basic():
    entries = [entry("GET", "/users/1"), entry("GET", "/users/2"),
               entry("POST", "/users"), entry("get", "/users/3")]
    g = group_by_endpoint(entries)
    assert set(g) == {("GET", "/users/{id}"), ("POST", "/users")}
    assert [e["path"] for e in g[("GET", "/users/{id}")]] == ["/users/1", "/users/2", "/users/3"]


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample_logs.jsonl not generated")
def test_sample_logs_grouping():
    g = group_by_endpoint(read_logs(SAMPLE))
    assert set(g) == {
        ("GET", "/users"), ("POST", "/users"), ("GET", "/users/{id}"),
        ("GET", "/products"), ("GET", "/products/{id}"),
        ("POST", "/orders"), ("GET", "/orders/{id}"),
    }
    assert sum(len(v) for v in g.values()) == 200

    user_ids = [normalize_path(e["path"])[1]["id"] for e in g[("GET", "/users/{id}")]]
    assert id_param_type(user_ids) == {"type": "integer"}
    order_ids = [normalize_path(e["path"])[1]["id"] for e in g[("GET", "/orders/{id}")]]
    assert id_param_type(order_ids) == {"type": "string", "format": "uuid"}
