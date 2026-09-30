"""dashboard/app.py — FastAPI app showing endpoints, diffs and alerts.   Owner: [Name 4]

Reads ONLY output/openapi.yaml and output/changes.jsonl (written by watcher), so it works
standalone against any spec. It never imports watcher.py. The HTML page polls /api/* every 2s.

Paths are module globals read on every request, so main.py (--spec) and tests can repoint them.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse

log = logging.getLogger(__name__)

SPEC_PATH = Path("output/openapi.yaml")
CHANGES_PATH = Path("output/changes.jsonl")
LOG_PATH = Path("live_logs.jsonl")
STATIC_DIR = Path(__file__).parent / "static"
PRISM_PORT = 4010
MAX_SAMPLE_MATCHES = 10

_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")

app = FastAPI(title="API Contract Dashboard")


# ---------------------------------------------------------------------------
# File readers (never raise: a missing / half-written / garbage file just means "nothing yet")
# ---------------------------------------------------------------------------

def empty_spec() -> dict[str, Any]:
    return {"openapi": "3.0.3", "info": {"title": "Inferred API", "version": "0.1.0"}, "paths": {}}


def read_spec() -> dict[str, Any]:
    """The spec at SPEC_PATH, or an empty spec if it is missing or unreadable."""
    p = Path(SPEC_PATH)
    try:
        with p.open("r", encoding="utf-8-sig") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return empty_spec()
    except (OSError, yaml.YAMLError) as e:
        log.warning("could not read spec %s: %s", p, e)
        return empty_spec()
    if not isinstance(data, dict):
        return empty_spec()
    if not isinstance(data.get("paths"), dict):
        data["paths"] = {}
    return data


def read_changes() -> list[dict[str, Any]]:
    """All change rows from CHANGES_PATH in file (oldest-first) order. Bad/partial lines are skipped."""
    p = Path(CHANGES_PATH)
    rows: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except FileNotFoundError:
        return []
    except OSError as e:
        log.warning("could not read changes %s: %s", p, e)
    return rows


def read_traffic_log() -> list[dict[str, Any]]:
    """Raw traffic entries from LOG_PATH in file (oldest-first) order. Bad/partial lines are skipped."""
    p = Path(LOG_PATH)
    rows: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except FileNotFoundError:
        return []
    except OSError as e:
        log.warning("could not read traffic log %s: %s", p, e)
    return rows


def _path_pattern(template: str) -> re.Pattern[str]:
    """Turn an OpenAPI path template ("/users/{id}") into a regex matching concrete paths."""
    parts = re.split(r"(\{[^{}]+\})", template)
    return re.compile("".join(r"[^/]+" if part.startswith("{") else re.escape(part) for part in parts))


def find_samples(method: str, path: str, status: int | None, limit: int) -> list[dict[str, Any]]:
    """The newest `limit` raw traffic entries matching method/path template (and status, if given)."""
    pattern = _path_pattern(path)
    matches: list[dict[str, Any]] = []
    for entry in reversed(read_traffic_log()):
        if str(entry.get("method", "")).upper() != method.upper():
            continue
        if not pattern.fullmatch(str(entry.get("path", ""))):
            continue
        if status is not None and entry.get("status") != status:
            continue
        matches.append({
            "timestamp": entry.get("timestamp"),
            "method": entry.get("method"),
            "path": entry.get("path"),
            "status": entry.get("status"),
            "request_body": entry.get("request_body"),
            "response_body": entry.get("response_body"),
        })
        if len(matches) >= limit:
            break
    return matches


def operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """[(METHOD, path, operation)] sorted by path, then method."""
    out = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in _HTTP_METHODS:
            op = item.get(method)
            if isinstance(op, dict):
                out.append((method.upper(), str(path), op))
    return sorted(out, key=lambda t: (t[1], _HTTP_METHODS.index(t[0].lower())))


def _mtime(p: Path) -> float | None:
    try:
        return Path(p).stat().st_mtime
    except OSError:
        return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve static/index.html."""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/spec")
def get_spec() -> dict[str, Any]:
    """The current spec as JSON (an empty spec with `paths: {}` if there is none yet)."""
    return read_spec()


@app.get("/api/changes")
def list_changes(limit: int | None = None) -> list[dict[str, Any]]:
    """All change rows, newest first (optionally only the newest `limit`)."""
    rows = list(reversed(read_changes()))  # the watcher appends, so file order is oldest-first
    return rows[:limit] if limit is not None and limit >= 0 else rows


@app.get("/api/sample")
def get_sample(method: str, path: str, status: int | None = None, limit: int = 3) -> dict[str, Any]:
    """Raw request/response samples from the traffic log for a given method/path template (and status),
    newest first, so a change row can show the actual HTTP data behind a one-line diff summary."""
    limit = max(1, min(limit, MAX_SAMPLE_MATCHES))
    log_path = Path(LOG_PATH)
    return {
        "log_path": str(log_path),
        "log_exists": log_path.exists(),
        "matches": find_samples(method, path, status, limit),
    }


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    """Counts for the header cards, plus when either output file last changed (null if neither exists)."""
    spec = read_spec()
    rows = read_changes()
    times = [t for t in (_mtime(SPEC_PATH), _mtime(CHANGES_PATH)) if t is not None]
    return {
        "endpoint_count": len(operations(spec)),
        "total_changes": len(rows),
        "breaking_changes": sum(1 for r in rows if r.get("breaking") is True),
        "last_updated": _iso(max(times)) if times else None,
        "spec_exists": Path(SPEC_PATH).exists(),
        "prism_port": PRISM_PORT,
    }


# --- extras from the original skeleton (handy for curl / other clients) ---

@app.get("/api/endpoints")
def list_endpoints() -> list[dict[str, Any]]:
    """Flatten the spec into rows: [{"method", "path", "statuses", "summary", "sample_count"}]."""
    rows = []
    for method, path, op in operations(read_spec()):
        statuses = []
        for s in (op.get("responses") or {}):
            statuses.append(int(s) if str(s).isdigit() else str(s))
        rows.append({"method": method, "path": path, "statuses": statuses,
                     "summary": op.get("summary", ""), "sample_count": op.get("x-sample-count")})
    return rows


@app.get("/api/alerts")
def list_alerts() -> list[dict[str, Any]]:
    """Only changes with breaking=True, newest first."""
    return [r for r in list_changes() if r.get("breaking") is True]


@app.get("/api/health")
def health() -> dict[str, Any]:
    spec_exists = Path(SPEC_PATH).exists()
    return {"spec_exists": spec_exists, "endpoint_count": len(operations(read_spec())), "prism_port": PRISM_PORT}
