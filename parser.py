"""parser.py — read JSON-lines logs and extract fields.   Owner: [Name 1]

Contract:
    raw .jsonl file  ->  list[LogEntry]
Bad/malformed lines are skipped (never raise) so a half-written line can't crash continuous mode.

Tolerant of "unformatted" logs from other tools: FIELD_ALIASES (rule-based, no AI) maps common field
names and nested shapes onto the LogEntry fields, matched case-insensitively — e.g. verb / http_method,
url / uri / request.url, statusCode / status_code / response.status, requestBody / request.body,
responseBody / response.body, request.headers. Full URLs lose scheme and host, and their query string
moves into `query`. A string body that's truncated JSON gets a best-effort repair; a status logged as
text ("200 OK") is coerced to its number. Either kind of "we had to guess" is recorded in the entry's
own `flags`, and skip reasons for lines dropped entirely can be tallied via `skipped=` (see quality.py).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlsplit

from models import LogEntry

log = logging.getLogger(__name__)

_MAX_PREVIEW = 80  # chars of a bad line to include in warnings

# Reasons parse_line_with_reason() reports a line as skipped (used for quality.py's skip tally).
SkipReason = str  # "invalid_json" | "non_object" | "no_method" | "no_path" | "no_status"

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
    "request_body": ("request_body", "requestBody", "request.body", "request.request_body", "request.requestBody",
                     "request.postData.text"),   # HAR
    "response_body": ("response_body", "responseBody", "response.body", "response.response_body",
                      "response.responseBody", "response.content.text"),   # HAR
    "headers": ("headers", "request_headers", "requestHeaders", "request.headers"),
    "timestamp": ("timestamp", "time", "ts", "@timestamp", "datetime", "request.timestamp",
                  "startedDateTime"),   # HAR
}
# (response headers are accepted in the input but not kept: LogEntry.headers are the request headers)

_MISSING = object()


def _ci_get(d: dict[str, Any], key: str) -> Any:
    """d[key], matching the key case-insensitively ("PATH"/"Path"/"path" are the same key)."""
    if key in d:  # fast path: the overwhelmingly common case is already-correct casing
        return d[key]
    lowered = key.lower()
    for k, v in d.items():
        if isinstance(k, str) and k.lower() == lowered:
            return v
    return _MISSING


def _lookup(data: dict[str, Any], dotted: str) -> Any:
    """data["a"]["b"] for "a.b", case-insensitively at every level; a literal key containing dots
    ("@timestamp") is tried first."""
    hit = _ci_get(data, dotted)
    if hit is not _MISSING:
        return hit
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return _MISSING
        node = _ci_get(node, part)
        if node is _MISSING:
            return _MISSING
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


def _repair_json(text: str) -> Any | None:
    """Best-effort repair for JSON cut off mid-value (a common truncated-log shape): close any
    unterminated string, then close open brackets/braces in the right order. None if that still
    doesn't parse (it wasn't just truncation — e.g. genuinely corrupt or not JSON at all)."""
    in_string = escape = False
    stack: list[str] = []
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    repaired = text + ('"' if in_string else "") + "".join({"{": "}", "[": "]"}[c] for c in reversed(stack))
    try:
        return json.loads(repaired)
    except ValueError:
        return None


def _json_if_text(value: Any) -> tuple[Any, str | None]:
    """A body logged as a JSON *string* ('{"id": 1}') becomes the object; other strings stay as they
    are. Returns (value, flag):
    - flag is None: not a string, or a string that needed no help (parsed as-is, or plainly not JSON).
    - "body_repaired": looked like truncated JSON and was salvaged by closing it out.
    - "body_truncated": looked like JSON, repair failed too -> value becomes None (excluded from
      schema inference rather than silently typed as a plain string).
    - "non_json_body": a non-empty string that never looked like JSON (e.g. an HTML error page logged
      under a JSON-typed response) -> kept as-is, just flagged for the quality report.
    """
    if not isinstance(value, str):
        return value, None
    stripped = value.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped), None
        except ValueError:
            repaired = _repair_json(stripped)
            return (repaired, "body_repaired") if repaired is not None else (None, "body_truncated")
    if not stripped:
        return value, None
    return value, "non_json_body"


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


_STATUS_LEADING_RE = re.compile(r"^\s*(\d{3})\b")
_STATUS_TRAILING_RE = re.compile(r"(\d{3})\s*$")


def _parse_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _coerce_status(value: Any) -> tuple[int | None, bool]:
    """(status, coerced): a direct int/numeric-string parse first; failing that, for a string, pull a
    3-digit HTTP status out of surrounding text ("200 OK" -> 200, "HTTP/1.1 404" -> 404).
    coerced=True only when the second path was needed."""
    status = _parse_status(value)
    if status is not None:
        return status, False
    if isinstance(value, str):
        m = _STATUS_LEADING_RE.match(value) or _STATUS_TRAILING_RE.search(value)
        if m:
            status = _parse_status(m.group(1))
            if status is not None:
                return status, True
    return None, False


def parse_line(line: str) -> LogEntry | None:
    """Parse one JSON line into a LogEntry, or None if it can't be. See parse_line_with_reason for why."""
    return parse_line_with_reason(line)[0]


def parse_line_with_reason(line: str) -> tuple[LogEntry | None, SkipReason | None]:
    """Parse one JSON line into a LogEntry.

    Returns (entry, None) on success, or (None, reason) where reason is one of "invalid_json",
    "non_object", "no_method", "no_path", "no_status" — used to tally *why* lines were skipped
    (see quality.py). `parse_line` is the same thing without the reason, for callers that don't need it.

    Normalises: method -> upper-case; strips any query string from `path` and merges it into `query`;
    missing `query`/`headers` -> {}; missing bodies -> None; `status` -> int (coercing "200 OK"-style
    text). `entry["flags"]` notes anything that needed help along the way (see _json_if_text/`_coerce_status`).
    """
    if not line or not line.strip():
        return None, None  # a blank line is not a data-quality problem, just nothing to parse
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        log.warning("skipping invalid JSON line: %s", _preview(line))
        return None, "invalid_json"
    if not isinstance(data, dict):
        log.warning("skipping non-object line: %s", _preview(line))
        return None, "non_object"
    return _parse_entry_dict(data, _preview(line))


def _parse_entry_dict(data: dict[str, Any], preview: str = "") -> tuple[LogEntry | None, SkipReason | None]:
    """The part of parse_line_with_reason after `json.loads` — reused by `iter_har_entries` for a HAR
    entry that's already a dict (a whole HAR file is one JSON document, not JSON-lines)."""
    method = _first(data, "method", lambda v: isinstance(v, str) and v.strip().isalpha())
    raw_path = _first(data, "path", lambda v: isinstance(v, str) and bool(v.strip()))
    status, status_coerced = _coerce_status(_first(data, "status", lambda v: _coerce_status(v)[0] is not None))
    if not isinstance(method, str) or not method.strip():
        log.warning("skipping line without valid method: %s", preview)
        return None, "no_method"
    if not isinstance(raw_path, str) or not raw_path.strip():
        log.warning("skipping line without valid path: %s", preview)
        return None, "no_path"
    if status is None:
        log.warning("skipping line without valid status: %s", preview)
        return None, "no_status"

    # Split off any query string (also handles full URLs like "http://host/users/1?x=2").
    path, qs = _split_url(raw_path)
    query: dict[str, str] = dict(parse_qsl(qs, keep_blank_values=True))
    explicit = _first(data, "query")
    if explicit is not _MISSING:
        query.update(_query(explicit))  # explicit `query` field wins on conflicts

    def value(field: str) -> Any:
        v = _first(data, field)
        return None if v is _MISSING else v

    flags: list[str] = []
    if status_coerced:
        flags.append("status_coerced")
    request_body, req_flag = _json_if_text(value("request_body"))
    if req_flag:
        flags.append(req_flag)
    response_body, resp_flag = _json_if_text(value("response_body"))
    if resp_flag:
        flags.append(resp_flag)

    timestamp = value("timestamp")
    return LogEntry(
        timestamp=str(timestamp) if timestamp not in (None, "") else "",
        method=method.strip().upper(),
        path=path,
        query=query,
        request_body=request_body,
        status=status,
        response_body=response_body,
        headers=_headers(value("headers")),
        flags=flags,
    ), None


def iter_logs(path: str | Path, skipped: dict[SkipReason, int] | None = None) -> Iterator[LogEntry]:
    """Yield every valid LogEntry in the file, in file order. If `skipped` is given, every skipped
    non-blank line increments `skipped[reason]` (see quality.py for the aggregate report)."""
    p = Path(path)
    if not p.exists():
        log.warning("log file not found: %s", p)
        return
    with p.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            entry, reason = parse_line_with_reason(line)
            if entry is not None:
                yield entry
            elif reason is not None and skipped is not None:
                skipped[reason] = skipped.get(reason, 0) + 1


def read_logs(path: str | Path, skipped: dict[SkipReason, int] | None = None) -> list[LogEntry]:
    """Read the whole file. Convenience wrapper around iter_logs."""
    return list(iter_logs(path, skipped))


def read_new_logs(
    path: str | Path, offset: int = 0, skipped: dict[SkipReason, int] | None = None,
) -> tuple[list[LogEntry], int]:
    """Read entries appended since byte `offset` (used by watcher for tailing).

    Returns (new_entries, new_offset). Only consumes complete lines (ending in '\\n'); a trailing
    partial line is left for the next call. If the file shrank (truncated/rotated), restart from 0.
    If `skipped` is given, every skipped non-blank line increments `skipped[reason]`.
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
        entry, reason = parse_line_with_reason(line)
        if entry is not None:
            entries.append(entry)
        elif reason is not None and skipped is not None:
            skipped[reason] = skipped.get(reason, 0) + 1
    return entries, new_offset


def iter_har_entries(path: str | Path, skipped: dict[SkipReason, int] | None = None) -> Iterator[LogEntry]:
    """Yield every valid LogEntry from a HAR file (a browser DevTools "Export HAR" / any
    `{"log": {"entries": [...]}}` document) — the same FIELD_ALIASES table already covers HAR's shape
    (request.url, request.method, response.status, request.headers as name/value pairs); the only
    HAR-specific aliases are the string body locations (request.postData.text, response.content.text)
    and the timestamp (startedDateTime)."""
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("could not read HAR file %s: %s", p, e)
        return
    raw_entries = (data.get("log") or {}).get("entries") if isinstance(data, dict) else None
    for raw in raw_entries or []:
        if not isinstance(raw, dict):
            continue
        entry, reason = _parse_entry_dict(raw, preview=str(raw)[:_MAX_PREVIEW])
        if entry is not None:
            yield entry
        elif reason is not None and skipped is not None:
            skipped[reason] = skipped.get(reason, 0) + 1


def read_har(path: str | Path, skipped: dict[SkipReason, int] | None = None) -> list[LogEntry]:
    """Read a whole HAR file. Convenience wrapper around iter_har_entries."""
    return list(iter_har_entries(path, skipped))
