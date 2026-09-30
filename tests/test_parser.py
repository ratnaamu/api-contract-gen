"""Tests for parser.py. Run from repo root: pytest -q"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser import iter_logs, parse_line, parse_line_with_reason, read_har, read_logs, read_new_logs  # noqa: E402

SAMPLE = ROOT / "sample_logs.jsonl"

GOOD = {
    "timestamp": "2026-09-01T09:00:00Z", "method": "get", "path": "/users/12",
    "query": {}, "request_body": None, "status": 200,
    "response_body": {"id": 12}, "headers": {"Accept": "application/json"},
}


def line(**overrides) -> str:
    d = {**GOOD, **overrides}
    return json.dumps({k: v for k, v in d.items() if v is not ...})


# ---------- parse_line ----------

def test_parse_valid_line():
    e = parse_line(line())
    assert e is not None
    assert e["method"] == "GET"
    assert e["path"] == "/users/12"
    assert e["status"] == 200
    assert e["response_body"] == {"id": 12}
    assert e["headers"] == {"Accept": "application/json"}


@pytest.mark.parametrize("bad", [
    "", "   ", "\n", "not json", '{"method": "GET", "path": "/x", "sta',  # half-written
    "[1, 2, 3]", "42", "null",
])
def test_parse_garbage_returns_none(bad):
    assert parse_line(bad) is None


@pytest.mark.parametrize("field", ["method", "path", "status"])
def test_missing_required_field_returns_none(field):
    assert parse_line(line(**{field: ...})) is None


@pytest.mark.parametrize("status", ["abc", None, True, 42, 1000])
def test_bad_status_returns_none(status):
    assert parse_line(line(status=status)) is None


def test_status_string_coerced_to_int():
    assert parse_line(line(status="404"))["status"] == 404


def test_query_string_split_and_merged():
    e = parse_line(line(path="/products?category=audio&page=2", query={"limit": 10}))
    assert e["path"] == "/products"
    assert e["query"] == {"category": "audio", "page": "2", "limit": "10"}


def test_explicit_query_wins_over_query_string():
    e = parse_line(line(path="/p?page=1", query={"page": "3"}))
    assert e["query"] == {"page": "3"}


def test_missing_optional_fields_defaulted():
    raw = json.dumps({"method": "DELETE", "path": "/users/1", "status": 204})
    e = parse_line(raw)
    assert e["query"] == {} and e["headers"] == {}
    assert e["request_body"] is None and e["response_body"] is None
    assert e["timestamp"] == ""


def test_non_dict_query_and_headers_become_empty():
    e = parse_line(line(query="oops", headers=["x"]))
    assert e["query"] == {} and e["headers"] == {}


def test_full_url_path():
    e = parse_line(line(path="http://api.local/users/5?x=1"))
    assert e["path"] == "/users/5" and e["query"] == {"x": "1"}


def test_flags_default_empty():
    assert parse_line(line())["flags"] == []


# ---------- case-insensitive top-level keys ----------

def test_uppercase_top_level_keys_are_recognized():
    raw = json.dumps({"PATH": "/x", "Method": "get", "Status": 200})
    e = parse_line(raw)
    assert e is not None
    assert e["method"] == "GET" and e["path"] == "/x" and e["status"] == 200


def test_case_insensitive_dotted_alias():
    raw = json.dumps({"Request": {"Method": "post", "Url": "/orders"}, "status": 201})
    e = parse_line(raw)
    assert e is not None
    assert e["method"] == "POST" and e["path"] == "/orders"


# ---------- truncated / non-JSON bodies ----------

def test_truncated_json_body_is_repaired():
    raw = json.dumps({**GOOD, "response_body": '{"id": 2, "name": "b"'})  # cut mid-string
    e = parse_line(raw)
    assert e["response_body"] == {"id": 2, "name": "b"}
    assert "body_repaired" in e["flags"]


def test_unrepairable_json_body_becomes_none_and_flagged():
    raw = json.dumps({**GOOD, "response_body": '{"id": ,,, garbage'})
    e = parse_line(raw)
    assert e["response_body"] is None
    assert "body_truncated" in e["flags"]


def test_non_json_string_body_is_flagged_but_kept():
    raw = json.dumps({**GOOD, "response_body": "<html>502 Bad Gateway</html>"})
    e = parse_line(raw)
    assert e["response_body"] == "<html>502 Bad Gateway</html>"
    assert "non_json_body" in e["flags"]


def test_empty_string_body_not_flagged():
    raw = json.dumps({**GOOD, "response_body": ""})
    e = parse_line(raw)
    assert e["response_body"] == "" and e["flags"] == []


# ---------- status coercion ----------

def test_status_with_trailing_text_is_coerced():
    e = parse_line(line(status="200 OK"))
    assert e["status"] == 200
    assert "status_coerced" in e["flags"]


def test_status_with_leading_text_is_coerced():
    e = parse_line(line(status="HTTP/1.1 404"))
    assert e["status"] == 404
    assert "status_coerced" in e["flags"]


def test_plain_numeric_status_is_not_flagged_as_coerced():
    e = parse_line(line(status="404"))
    assert e["status"] == 404
    assert "status_coerced" not in e["flags"]


# ---------- skip-reason tallying ----------

def test_parse_line_with_reason_reports_why():
    assert parse_line_with_reason("not json") == (None, "invalid_json")
    assert parse_line_with_reason("[1, 2]") == (None, "non_object")
    assert parse_line_with_reason(line(method=...)) == (None, "no_method")
    assert parse_line_with_reason(line(path=...)) == (None, "no_path")
    assert parse_line_with_reason(line(status=...)) == (None, "no_status")
    entry, reason = parse_line_with_reason(line())
    assert entry is not None and reason is None


def test_read_logs_tallies_skip_reasons(tmp_path):
    f = tmp_path / "logs.jsonl"
    f.write_text("\n".join([line(), "garbage", line(method=...), line(path="/ok")]) + "\n",
                 encoding="utf-8")
    skipped: dict[str, int] = {}
    entries = read_logs(f, skipped)
    assert [e["path"] for e in entries] == ["/users/12", "/ok"]
    assert skipped == {"invalid_json": 1, "no_method": 1}


def test_read_new_logs_tallies_skip_reasons(tmp_path):
    f = tmp_path / "live.jsonl"
    f.write_text("garbage\n" + line(path="/ok") + "\n", encoding="utf-8")
    skipped: dict[str, int] = {}
    entries, _ = read_new_logs(f, 0, skipped)
    assert [e["path"] for e in entries] == ["/ok"]
    assert skipped == {"invalid_json": 1}


# ---------- read_logs / iter_logs ----------

def test_read_logs_skips_bad_lines(tmp_path):
    f = tmp_path / "logs.jsonl"
    f.write_text("\n".join([line(), "garbage", "", line(path="/orders"), '{"method":']) + "\n",
                 encoding="utf-8")
    entries = read_logs(f)
    assert [e["path"] for e in entries] == ["/users/12", "/orders"]


def test_read_logs_missing_file_returns_empty(tmp_path):
    assert read_logs(tmp_path / "nope.jsonl") == []


def test_iter_logs_handles_bom(tmp_path):
    f = tmp_path / "bom.jsonl"
    f.write_bytes(b"\xef\xbb\xbf" + line().encode() + b"\n")
    assert len(list(iter_logs(f))) == 1


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample_logs.jsonl not generated")
def test_sample_logs_all_parse():
    entries = read_logs(SAMPLE)
    assert len(entries) == 200
    statuses = [e["status"] for e in entries]
    assert statuses.count(200) == 133
    assert statuses.count(201) == 34
    assert statuses.count(400) == 12
    assert statuses.count(404) == 17
    assert statuses.count(500) == 4
    assert all(e["method"].isupper() for e in entries)
    assert all("?" not in e["path"] for e in entries)


# ---------- read_new_logs ----------

def test_read_new_logs_tailing(tmp_path):
    f = tmp_path / "live.jsonl"
    f.write_text(line(path="/a") + "\n", encoding="utf-8")

    entries, off = read_new_logs(f, 0)
    assert [e["path"] for e in entries] == ["/a"]
    assert off == f.stat().st_size

    # nothing new
    assert read_new_logs(f, off) == ([], off)

    # partial line is not consumed
    partial = line(path="/b")
    with f.open("a", encoding="utf-8") as fh:
        fh.write(partial[:20])
    entries, off2 = read_new_logs(f, off)
    assert entries == [] and off2 == off

    # finishing the line makes it available
    with f.open("a", encoding="utf-8") as fh:
        fh.write(partial[20:] + "\n")
    entries, off3 = read_new_logs(f, off2)
    assert [e["path"] for e in entries] == ["/b"]
    assert off3 == f.stat().st_size


def test_read_new_logs_skips_bad_complete_lines(tmp_path):
    f = tmp_path / "live.jsonl"
    f.write_text("garbage\n" + line(path="/ok") + "\n", encoding="utf-8")
    entries, _ = read_new_logs(f, 0)
    assert [e["path"] for e in entries] == ["/ok"]


def test_read_new_logs_truncation_restarts(tmp_path):
    f = tmp_path / "live.jsonl"
    f.write_text((line(path="/a") + "\n") * 5, encoding="utf-8")
    _, off = read_new_logs(f, 0)
    f.write_text(line(path="/new") + "\n", encoding="utf-8")  # truncated/rotated
    entries, off2 = read_new_logs(f, off)
    assert [e["path"] for e in entries] == ["/new"]
    assert off2 == f.stat().st_size


def test_read_new_logs_missing_file(tmp_path):
    assert read_new_logs(tmp_path / "nope.jsonl", 123) == ([], 0)


def test_read_new_logs_crlf(tmp_path):
    f = tmp_path / "win.jsonl"
    f.write_bytes((line(path="/a") + "\r\n" + line(path="/b") + "\r\n").encode())
    entries, off = read_new_logs(f, 0)
    assert [e["path"] for e in entries] == ["/a", "/b"]
    assert off == f.stat().st_size


# ---------- HAR files ----------

def _har_file(tmp_path, entries) -> Path:
    f = tmp_path / "export.har"
    f.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}), encoding="utf-8")
    return f


def test_har_basic_get_entry(tmp_path):
    f = _har_file(tmp_path, [{
        "startedDateTime": "2026-01-01T00:00:00.000Z",
        "request": {"method": "GET", "url": "https://api.example.com/users/12?x=1",
                    "headers": [{"name": "Authorization", "value": "Bearer abc"}]},
        "response": {"status": 200, "content": {"mimeType": "application/json", "text": '{"id": 12}'}},
    }])
    entries = read_har(f)
    assert len(entries) == 1
    e = entries[0]
    assert e["method"] == "GET" and e["path"] == "/users/12"
    assert e["query"] == {"x": "1"}
    assert e["status"] == 200
    assert e["response_body"] == {"id": 12}
    assert e["headers"] == {"Authorization": "Bearer abc"}
    assert e["timestamp"] == "2026-01-01T00:00:00.000Z"


def test_har_post_with_request_body(tmp_path):
    f = _har_file(tmp_path, [{
        "startedDateTime": "2026-01-01T00:00:00.000Z",
        "request": {"method": "POST", "url": "https://api.example.com/orders",
                    "postData": {"mimeType": "application/json", "text": '{"item": "x"}'}},
        "response": {"status": 201, "content": {"text": '{"id": 1}'}},
    }])
    e = read_har(f)[0]
    assert e["method"] == "POST" and e["request_body"] == {"item": "x"}


def test_har_multiple_entries_and_skips_invalid(tmp_path):
    f = _har_file(tmp_path, [
        {"startedDateTime": "t", "request": {"method": "GET", "url": "/a"}, "response": {"status": 200}},
        {"request": {"method": "GET"}, "response": {}},  # no path/status -> skipped
        {"startedDateTime": "t", "request": {"method": "GET", "url": "/b"}, "response": {"status": 404}},
    ])
    entries = read_har(f)
    assert [e["path"] for e in entries] == ["/a", "/b"]


def test_har_tallies_skip_reasons(tmp_path):
    f = _har_file(tmp_path, [{"request": {"method": "GET"}, "response": {}}])
    skipped: dict[str, int] = {}
    assert read_har(f, skipped) == []
    assert skipped == {"no_path": 1}


def test_har_missing_file_returns_empty(tmp_path):
    assert read_har(tmp_path / "nope.har") == []


def test_har_not_json_returns_empty(tmp_path):
    f = tmp_path / "bad.har"
    f.write_text("not json", encoding="utf-8")
    assert read_har(f) == []


def test_har_wrong_shape_returns_empty(tmp_path):
    f = tmp_path / "wrong.har"
    f.write_text(json.dumps({"not": "a har file"}), encoding="utf-8")
    assert read_har(f) == []
