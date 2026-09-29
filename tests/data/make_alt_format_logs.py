"""Regenerate tests/data/alt_format_logs.jsonl: the same 200 requests as sample_logs.jsonl, written in four
"unformatted" shapes other tools produce (rotating per line), to test parser.FIELD_ALIASES.

    python tests/data/make_alt_format_logs.py
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "sample_logs.jsonl"
OUT = Path(__file__).with_name("alt_format_logs.jsonl")


def nested(e: dict) -> dict:
    """{"request": {...}, "response": {...}} with a full https URL (query string in the URL)."""
    qs = "?" + urlencode(e["query"]) if e["query"] else ""
    return {"@timestamp": e["timestamp"],
            "request": {"method": e["method"].lower(), "url": f"https://api.example.com{e['path']}{qs}",
                        "headers": e["headers"], "body": e["request_body"]},
            "response": {"status": e["status"], "headers": {"Content-Type": "application/json"},
                         "body": e["response_body"]}}


def camel(e: dict) -> dict:
    """camelCase, status code as a string, bodies logged as JSON strings, and a misleading "status"."""
    qs = "?" + urlencode(e["query"]) if e["query"] else ""
    return {"time": e["timestamp"], "verb": e["method"], "uri": e["path"] + qs,
            "status": "completed", "statusCode": str(e["status"]),
            "requestBody": None if e["request_body"] is None else json.dumps(e["request_body"]),
            "responseBody": None if e["response_body"] is None else json.dumps(e["response_body"]),
            "headers": e["headers"]}


def snake(e: dict) -> dict:
    """snake_case, host:port URL without the query, query_params dict, headers under "request"."""
    return {"ts": e["timestamp"], "http_method": e["method"], "url": f"http://localhost:8080{e['path']}",
            "query_params": e["query"], "status_code": e["status"],
            "request_body": e["request_body"], "response_body": e["response_body"],
            "request": {"headers": e["headers"]}}


def har(e: dict) -> dict:
    """HAR-like: headers as a list of name/value pairs, query as a raw string, response.status_code."""
    return {"timestamp": e["timestamp"],
            "request": {"method": e["method"], "path": e["path"], "query": urlencode(e["query"]),
                        "headers": [{"name": k, "value": v} for k, v in e["headers"].items()],
                        "requestBody": e["request_body"]},
            "response": {"status_code": e["status"], "responseBody": e["response_body"]}}


SHAPES = (nested, camel, snake, har)


def main() -> None:
    lines = [json.loads(line) for line in SRC.read_text(encoding="utf-8").splitlines() if line.strip()]
    with OUT.open("w", encoding="utf-8", newline="\n") as f:
        for i, e in enumerate(lines):
            f.write(json.dumps(SHAPES[i % len(SHAPES)](e), ensure_ascii=False) + "\n")
    print(f"wrote {len(lines)} lines to {OUT}")


if __name__ == "__main__":
    main()
