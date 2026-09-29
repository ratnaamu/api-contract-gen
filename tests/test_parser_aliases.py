"""Tolerant parsing of "unformatted" JSON logs: parser.FIELD_ALIASES, nested shapes, full URLs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from normalizer import group_by_endpoint  # noqa: E402
from parser import FIELD_ALIASES, parse_line, read_logs  # noqa: E402
from watcher import build_from_entries  # noqa: E402

SAMPLE = ROOT / "sample_logs.jsonl"
ALT = ROOT / "tests" / "data" / "alt_format_logs.jsonl"


def parse(record: dict):
    return parse_line(json.dumps(record))


# ---------- the alias table ----------

@pytest.mark.parametrize("key", ["method", "verb", "http_method", "httpMethod"])
def test_method_aliases(key):
    e = parse({key: "post", "path": "/a", "status": 201})
    assert e["method"] == "POST"


@pytest.mark.parametrize("key", ["path", "url", "uri"])
def test_path_aliases(key):
    assert parse({"method": "GET", key: "/users/7", "status": 200})["path"] == "/users/7"


@pytest.mark.parametrize("key", ["status", "statusCode", "status_code"])
def test_status_aliases(key):
    assert parse({"method": "GET", "path": "/a", key: "404"})["status"] == 404


def test_nested_request_response_shape():
    e = parse({"request": {"method": "PATCH", "url": "/a/1", "headers": {"Accept": "application/json"},
                           "body": {"x": 1}},
               "response": {"status": 200, "headers": {"Content-Type": "application/json"}, "body": {"ok": True}}})
    assert (e["method"], e["path"], e["status"]) == ("PATCH", "/a/1", 200)
    assert e["request_body"] == {"x": 1} and e["response_body"] == {"ok": True}
    assert e["headers"] == {"Accept": "application/json"}          # request headers, not response headers


@pytest.mark.parametrize("key", ["statusCode", "status_code"])
def test_nested_response_status_aliases(key):
    assert parse({"request": {"method": "GET", "path": "/a"}, "response": {key: 503}})["status"] == 503


def test_camel_case_bodies():
    e = parse({"verb": "POST", "uri": "/a", "statusCode": 201, "requestBody": {"a": 1}, "responseBody": {"id": 2}})
    assert e["request_body"] == {"a": 1} and e["response_body"] == {"id": 2}


def test_bodies_logged_as_json_strings_are_parsed():
    e = parse({"method": "POST", "path": "/a", "status": 201,
               "requestBody": '{"a": 1}', "responseBody": '[{"id": 1}]'})
    assert e["request_body"] == {"a": 1} and e["response_body"] == [{"id": 1}]
    plain = parse({"method": "GET", "path": "/a", "status": 200, "response_body": "just text"})
    assert plain["response_body"] == "just text"


def test_first_usable_alias_wins():
    """"status": "completed" is not an HTTP status, so statusCode is used."""
    e = parse({"method": "GET", "path": "/a", "status": "completed", "statusCode": "200"})
    assert e["status"] == 200


def test_canonical_names_take_precedence():
    e = parse({"method": "GET", "verb": "DELETE", "path": "/a", "url": "/b", "status": 200, "statusCode": 500})
    assert (e["method"], e["path"], e["status"]) == ("GET", "/a", 200)


def test_timestamp_aliases():
    for key in ("timestamp", "time", "ts", "@timestamp", "datetime"):
        assert parse({"method": "GET", "path": "/a", "status": 200, key: "2026-09-29T10:00:00Z"})["timestamp"] \
            == "2026-09-29T10:00:00Z"


def test_har_style_header_list():
    e = parse({"request": {"method": "GET", "url": "/a", "headers": [{"name": "Accept", "value": "*/*"},
                                                                      {"name": "X-Id", "value": "1"}]},
               "response": {"status": 200}})
    assert e["headers"] == {"Accept": "*/*", "X-Id": "1"}


def test_alias_table_covers_the_brief():
    assert {"method", "verb", "http_method"} <= set(FIELD_ALIASES["method"])
    assert {"path", "url", "uri", "request.url"} <= set(FIELD_ALIASES["path"])
    assert {"status", "statusCode", "status_code", "response.status"} <= set(FIELD_ALIASES["status"])
    assert {"request_body", "requestBody", "request.body"} <= set(FIELD_ALIASES["request_body"])
    assert {"response_body", "responseBody", "response.body"} <= set(FIELD_ALIASES["response_body"])
    assert "request.headers" in FIELD_ALIASES["headers"]


def test_missing_required_fields_still_skip_the_line():
    assert parse({"verb": "GET", "statusCode": 200}) is None          # no path under any alias
    assert parse({"url": "/a", "statusCode": 200}) is None            # no method
    assert parse({"verb": "GET", "url": "/a", "status": "completed"}) is None  # no usable status


# ---------- URLs ----------

@pytest.mark.parametrize("url, path", [
    ("https://api.example.com/users/12", "/users/12"),
    ("http://localhost:8080/users/12", "/users/12"),
    ("//cdn.example.com/users/12", "/users/12"),
    ("localhost:8080/users/12", "/users/12"),
    ("api.example.com/users/12", "/users/12"),
    ("users/12", "/users/12"),
    ("https://api.example.com", "/"),
])
def test_scheme_and_host_are_stripped(url, path):
    assert parse({"method": "GET", "url": url, "status": 200})["path"] == path


def test_query_string_moves_into_query():
    e = parse({"method": "GET", "url": "https://h/products?limit=10&category=books", "status": 200})
    assert e["path"] == "/products" and e["query"] == {"limit": "10", "category": "books"}


def test_explicit_query_wins_and_raw_query_strings_are_read():
    e = parse({"method": "GET", "url": "/p?limit=10", "query_params": "limit=5&page=2", "status": 200})
    assert e["query"] == {"limit": "5", "page": "2"}


# ---------- the alternative-format fixture ----------

def test_alt_format_fixture_uses_several_shapes():
    shapes = {frozenset(json.loads(line)) for line in ALT.read_text(encoding="utf-8").splitlines()}
    assert len(shapes) >= 4
    assert all("method" not in s or "request" in s for s in shapes)  # never the canonical flat shape


def test_alt_format_parses_to_the_same_entries():
    assert read_logs(ALT) == read_logs(SAMPLE)


def test_alt_format_produces_the_same_endpoints():
    alt, normal = read_logs(ALT), read_logs(SAMPLE)
    assert len(alt) == len(normal) == 200
    assert set(group_by_endpoint(alt)) == set(group_by_endpoint(normal))
    assert build_from_entries(alt) == build_from_entries(normal)      # the very same contract
