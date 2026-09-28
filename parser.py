"""parser.py — read JSON-lines logs and extract fields.   Owner: [Name 1]

Contract:
    raw .jsonl file  ->  list[LogEntry]
Bad/malformed lines are skipped (never raise) so a half-written line can't crash continuous mode.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from models import LogEntry


def parse_line(line: str) -> LogEntry | None:
    """Parse one JSON line into a LogEntry.

    - Returns None for blank lines, invalid JSON, or entries missing `method`/`path`/`status`.
    - Normalises: method -> upper-case; strips any query string from `path` and merges it into `query`;
      missing `query`/`headers` -> {}; missing bodies -> None; `status` -> int.
    """
    raise NotImplementedError


def iter_logs(path: str | Path) -> Iterator[LogEntry]:
    """Yield every valid LogEntry in the file, in file order."""
    raise NotImplementedError


def read_logs(path: str | Path) -> list[LogEntry]:
    """Read the whole file. Convenience wrapper around iter_logs."""
    raise NotImplementedError


def read_new_logs(path: str | Path, offset: int = 0) -> tuple[list[LogEntry], int]:
    """Read entries appended since byte `offset` (used by watcher for tailing).

    Returns (new_entries, new_offset). Only consumes complete lines (ending in '\\n'); a trailing
    partial line is left for the next call. If the file shrank (truncated/rotated), restart from 0.
    """
    raise NotImplementedError
