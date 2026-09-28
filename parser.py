"""parser.py — read JSON-lines logs and extract fields.   Owner: [Name 1]

Contract:
    raw .jsonl file  ->  list[LogEntry]
Bad/malformed lines are skipped (never raise) so a half-written line can't crash continuous mode.
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

    method = data.get("method")
    raw_path = data.get("path")
    status = _parse_status(data.get("status"))
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
    parts = urlsplit(raw_path.strip())
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path

    query: dict[str, str] = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(_str_dict(data.get("query")))  # explicit `query` field wins on conflicts

    return LogEntry(
        timestamp=str(data.get("timestamp") or ""),
        method=method.strip().upper(),
        path=path,
        query=query,
        request_body=data.get("request_body"),
        status=status,
        response_body=data.get("response_body"),
        headers=_str_dict(data.get("headers")),
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
