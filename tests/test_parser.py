"""Tests for parser.py. Run from repo root: pytest -q"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser import iter_logs, parse_line, read_logs, read_new_logs  # noqa: E402

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
