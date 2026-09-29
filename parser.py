"""parser.py — read JSON-lines logs and extract fields.   Owner: [Name 1]

Contract:
    raw .jsonl file  ->  list[LogEntry]
Bad/malformed lines are skipped (never raise) so a half-written line can't crash continuous mode.

Tolerant of "unformatted" logs from other tools: FIELD_ALIASES (rule-based, no AI) maps common field
names and nested shapes onto the LogEntry fields — e.g. verb / http_method, url / uri / request.url,
statusCode / status_code / response.status, requestBody / request.body, responseBody / response.body,
request.headers. Full URLs lose scheme and host, and their query string moves into `query`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlsplit

from models import LogEntry

log = logging.getLogger(__name__)

_MAX_PREVIEW = 80  # chars of a bad line to include in warnings

# canonical LogEntry field -> accepted spellings, tried in order; the first one with a usable value wins.
# Dotted names look inside nested objects: "request.url" is data["request"]["url"].
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "method": ("method", "verb", "http_method", "httpMethod",
               "request.method", "request.verb", "request.http_method", "request.httpMethod"),
    "path": ("path", "url", "uri", "request.url", "request.path", "request.uri"),
    "status": ("status", "statusCode", "status_code", "http_status",
               "response.status", "response.statusCode", "response.status_code"),
    "query": ("query", "query_params", "queryParams", "request.query", "request.query_params",
              "request.queryParams"),
    "request_body": ("request_body", "requestBody", "request.body", "request.request_body", "request.requestBody"),
    "response_body": ("response_body", "responseBody", "response.body", "response.response_body",
                      "response.responseBody"),
    "headers": ("headers", "request_headers", "requestHeaders", "request.headers"),
    "timestamp": ("timestamp", "time", "ts", "@timestamp", "datetime", "request.timestamp"),
}
# (response headers are accepted in the input but not kept: LogEntry.headers are the request headers)

_MISSING = object()


def _lookup(data: dict[str, Any], dotted: str) -> Any:
    """data["a"]["b"] for "a.b"; a literal key containing dots ("@timestamp") is tried first."""
    if dotted in data:
        return data[dotted]
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _first(data: dict[str, Any], field: str, usable: Any = None) -> Any:
    """Value of the first alias of `field` that is present (and passes `usable`, if given)."""
    for alias in FIELD_ALIASES[field]:
        value = _lookup(data, alias)
        if value is _MISSING:
            continue
        if usable is None or usable(value):
            return value
    return _MISSING


def _json_if_text(value: Any) -> Any:
    """A body logged as a JSON *string* ('{"id": 1}') becomes the object; other strings stay as they are."""
    if isinstance(value, str) and value.strip()[:1] in ("{", "["):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _headers(value: Any) -> dict[str, str]:
    """dict, or a HAR-style list of {"name", "value"} pairs."""
    if isinstance(value, list):
        value = {h.get("name"): h.get("value") for h in value
                 if isinstance(h, dict) and isinstance(h.get("name"), str)}
    return _str_dict(value)


def _query(value: Any) -> dict[str, str]:
    """dict, or a raw query string ("a=1&b=2"). Any other string ("oops") is not a query -> {}."""
    if isinstance(value, str):
        return dict(parse_qsl(value.lstrip("?"), keep_blank_values=True)) if "=" in value else {}
    return _str_dict(value)


def _split_url(raw: str) -> tuple[str, str]:
    """(path, query string) from a path or full URL: scheme and host are dropped, also for a host
    without a scheme ("localhost:8080/users/1", "api.example.com/users/1")."""
    raw = raw.strip()
    if "://" in raw or raw.startswith("//"):
        parts = urlsplit(raw)
        path, qs = parts.path, parts.query
    else:  # urlsplit would read "localhost:8080/users" as scheme "localhost"
        path, _, qs = raw.split("#", 1)[0].partition("?")
        if not path.startswith("/"):
            first, sep, rest = path.partition("/")
            if sep and ("." in first or ":" in first):
                path = rest
    path = path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path, qs


def _preview(line: str) -> str:
    line = line.strip()
    return line if len(line) <= _MAX_PREVIEW else line[:_MAX_PREVIEW] + "..."


def _str_dict(value: Any) -> dict[str, str]:
    """Coerce a dict-ish value into dict[str, str]; anything else -> {}."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, list):  # e.g. {"tag": ["a", "b"]} -> keep first value
            if not v:
                continue
            v = v[0]
        out[str(k)] = str(v)
    return out


def _parse_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def parse_line(line: str) -> LogEntry | None:
    """Parse one JSON line into a LogEntry.

    - Returns None for blank lines, invalid JSON, or entries missing `method`/`path`/`status`.
    - Normalises: method -> upper-case; strips any query string from `path` and merges it into `query`;
      missing `query`/`headers` -> {}; missing bodies -> None; `status` -> int.
    """
    if not line or not line.strip():
        return None
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        log.warning("skipping invalid JSON line: %s", _preview(line))
        return None
    if not isinstance(data, dict):
        log.warning("skipping non-object line: %s", _preview(line))
        return None

    method = _first(data, "method", lambda v: isinstance(v, str) and v.strip().isalpha())
    raw_path = _first(data, "path", lambda v: isinstance(v, str) and bool(v.strip()))
    status = _parse_status(_first(data, "status", lambda v: _parse_status(v) is not None))
    if not isinstance(method, str) or not method.strip():
        log.warning("skipping line without valid method: %s", _preview(line))
        return None
    if not isinstance(raw_path, str) or not raw_path.strip():
        log.warning("skipping line without valid path: %s", _preview(line))
        return None
    if status is None:
        log.warning("skipping line without valid status: %s", _preview(line))
        return None

    # Split off any query string (also handles full URLs like "http://host/users/1?x=2").
    path, qs = _split_url(raw_path)
    query: dict[str, str] = dict(parse_qsl(qs, keep_blank_values=True))
    explicit = _first(data, "query")
    if explicit is not _MISSING:
        query.update(_query(explicit))  # explicit `query` field wins on conflicts

    def value(field: str) -> Any:
        v = _first(data, field)
        return None if v is _MISSING else v

    timestamp = value("timestamp")
    return LogEntry(
        timestamp=str(timestamp) if timestamp not in (None, "") else "",
        method=method.strip().upper(),
        path=path,
        query=query,
        request_body=_json_if_text(value("request_body")),
        status=status,
        response_body=_json_if_text(value("response_body")),
        headers=_headers(value("headers")),
    )


def iter_logs(path: str | Path) -> Iterator[LogEntry]:
    """Yield every valid LogEntry in the file, in file order."""
    p = Path(path)
    if not p.exists():
        log.warning("log file not found: %s", p)
        return
    with p.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            entry = parse_line(line)
            if entry is not None:
                yield entry


def read_logs(path: str | Path) -> list[LogEntry]:
    """Read the whole file. Convenience wrapper around iter_logs."""
    return list(iter_logs(path))


def read_new_logs(path: str | Path, offset: int = 0) -> tuple[list[LogEntry], int]:
    """Read entries appended since byte `offset` (used by watcher for tailing).

    Returns (new_entries, new_offset). Only consumes complete lines (ending in '\\n'); a trailing
    partial line is left for the next call. If the file shrank (truncated/rotated), restart from 0.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return [], 0  # file missing (not created yet / rotated away)

    if offset < 0 or size < offset:
        log.info("log file %s shrank (%d < %d); re-reading from start", p, size, offset)
        offset = 0
    if size == offset:
        return [], offset

    try:
        with p.open("rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
    except OSError as e:
        log.warning("could not read %s: %s", p, e)
        return [], offset

    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return [], offset  # only a partial line so far

    complete = chunk[: last_nl + 1]
    new_offset = offset + len(complete)

    text = complete.decode("utf-8", errors="replace")
    if offset == 0:
        text = text.lstrip("﻿")  # strip BOM on first read

    entries: list[LogEntry] = []
    for line in text.splitlines():
        entry = parse_line(line)
        if entry is not None:
            entries.append(entry)
    return entries, new_offset
