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
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

log = logging.getLogger(__name__)

SPEC_PATH = Path("output/openapi.yaml")
CHANGES_PATH = Path("output/changes.jsonl")
QUALITY_PATH = Path("output/quality.json")
REPORTS_DIR = Path("output/reports")  # report.py writes contract-report-v*.docx + releases.json here
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


def read_quality() -> dict[str, Any]:
    """The quality report at QUALITY_PATH (A6), or an empty-but-well-shaped report if it's missing or
    unreadable — never raises, so an older spec built before this feature existed just shows nothing."""
    empty = {"lines_read": 0, "lines_skipped": 0, "skipped_by_reason": {}, "parse_rate": None,
            "endpoint_count": 0, "ambiguity_count": 0, "endpoints": [], "ambiguities": []}
    p = Path(QUALITY_PATH)
    try:
        with p.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty
    except (OSError, ValueError) as e:
        log.warning("could not read quality report %s: %s", p, e)
        return empty
    return data if isinstance(data, dict) else empty


MAX_TAIL_BYTES = 2 * 1024 * 1024  # /api/sample only wants the newest matches, so cap the read regardless
                                   # of how large the traffic log has grown (fine at 700 lines, not at 50k)


def read_traffic_log() -> list[dict[str, Any]]:
    """The newest ~MAX_TAIL_BYTES of the traffic log, oldest-first within that window. Bad/partial
    lines are skipped."""
    p = Path(LOG_PATH)
    rows: list[dict[str, Any]] = []
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > MAX_TAIL_BYTES:
                f.seek(size - MAX_TAIL_BYTES)
                f.readline()  # discard whatever partial line the seek landed inside
            text = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError as e:
        log.warning("could not read traffic log %s: %s", p, e)
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


MAX_RAW_FULL_BYTES = 5 * 1024 * 1024  # generated docs (spec/quality/changes) are never legitimately this
                                       # big; a cap here just bounds a pathological/corrupt file, same
                                       # spirit as MAX_TAIL_BYTES below for append-only logs


def _read_text_full(p: Path, cap: int | None = None) -> str | None:
    """The whole file as text, or None if missing/unreadable. Truncated (with a trailing notice) past
    `cap` (default MAX_RAW_FULL_BYTES) bytes rather than ever loading an unbounded file into memory.
    `cap` is resolved here (not as a signature default) so tests can monkeypatch MAX_RAW_FULL_BYTES,
    same as read_traffic_log does for MAX_TAIL_BYTES."""
    if cap is None:
        cap = MAX_RAW_FULL_BYTES
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            data = f.read(cap)
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("could not read %s: %s", p, e)
        return None
    text = data.decode("utf-8", errors="replace")
    if size > cap:
        text += f"\n... [truncated; file is {size} bytes, showing the first {cap}]"
    return text


def _read_text_tail(p: Path, max_bytes: int | None = None) -> str | None:
    """The newest ~max_bytes (default MAX_TAIL_BYTES) of an append-only file as text, or None if
    missing/unreadable — same byte-tail approach as read_traffic_log, but returns raw lines instead of
    parsed JSON rows."""
    if max_bytes is None:
        max_bytes = MAX_TAIL_BYTES
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard whatever partial line the seek landed inside
            data = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("could not read %s: %s", p, e)
        return None
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        text = f"... [showing the newest {max_bytes} bytes of {size}]\n" + text
    return text


def _prism_log_path() -> Path:
    return Path(SPEC_PATH).parent / "prism.log"


def _mock_spec_path() -> Path:
    """Mirrors spec_builder.mock_spec_path's naming ("openapi.yaml" -> "openapi.mock.json") without
    importing spec_builder — this module intentionally never imports the pipeline (see module docstring),
    so it still works standalone against any spec, even one built by a different tool version."""
    p = Path(SPEC_PATH)
    if p.suffix.lower() == ".json":
        p = p.with_suffix(".yaml")
    return p.with_suffix("").with_suffix(".mock.json")


# name -> (path, media type for download, "full" for a generated doc / "tail" for an append-only log).
# Deliberately a closed allowlist keyed by a fixed name, never a client-supplied path, so /api/raw can
# never be used to read an arbitrary file off disk.
def _raw_files() -> dict[str, tuple[Path, str, str]]:
    return {
        "spec": (Path(SPEC_PATH), "application/yaml", "full"),
        "mock-spec": (_mock_spec_path(), "application/json", "full"),
        "quality": (Path(QUALITY_PATH), "application/json", "full"),
        "changes": (Path(CHANGES_PATH), "application/x-ndjson", "full"),
        "prism-log": (_prism_log_path(), "text/plain", "tail"),
        "traffic-log": (Path(LOG_PATH), "application/x-ndjson", "tail"),
    }


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


@app.get("/api/quality")
def quality() -> dict[str, Any]:
    """The A6 quality report (see quality.py): parse rate, skip-reason tally, and per-endpoint
    confidence/ambiguities. An empty-but-well-shaped report if quality.json doesn't exist yet."""
    return read_quality()


@app.get("/api/raw/{name}")
def get_raw(name: str, download: bool = False):
    """Raw content of one of a fixed set of output files, for the dashboard's "Outputs" panel: `spec`,
    `mock-spec`, `quality`, `changes` are read whole (generated docs); `prism-log` and `traffic-log` are
    tailed (append-only). `name` is checked against a closed allowlist — never a client-supplied path.

    Default: JSON `{name, path, exists, content}` for the panel to render in a <pre>. `?download=1`:
    the raw bytes as a file attachment, for a real "Download" button."""
    files = _raw_files()
    if name not in files:
        raise HTTPException(status_code=404, detail=f"unknown output '{name}'; choose one of {sorted(files)}")
    path, media_type, mode = files[name]
    text = _read_text_tail(path) if mode == "tail" else _read_text_full(path)
    if download:
        if text is None:
            raise HTTPException(status_code=404, detail=f"{path} does not exist")
        return PlainTextResponse(text, media_type=media_type,
                                 headers={"Content-Disposition": f'attachment; filename="{path.name}"'})
    return {"name": name, "path": str(path), "exists": text is not None, "content": text or ""}


# ---------------------------------------------------------------------------
# Change reports (report.py): one Word document per release with breaking changes
# ---------------------------------------------------------------------------

_REPORT_NAME_RE = re.compile(r"^contract-report-v[0-9]+\.[0-9]+\.[0-9]+-[0-9]{8}-[0-9]{6}\.docx$")
_DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def read_releases() -> dict[str, Any]:
    p = Path(REPORTS_DIR) / "releases.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@app.get("/api/reports")
def list_reports() -> dict[str, Any]:
    """The change reports written so far, newest first, with the release each one documents:
    {current_version, reports: [{file, version, previous, generated_at, breaking, additive, size, url}]}."""
    data = read_releases()
    by_file = {r.get("file"): r for r in data.get("releases") or [] if isinstance(r, dict)}
    rows = []
    folder = Path(REPORTS_DIR)
    if folder.is_dir():
        for p in folder.iterdir():
            if not (p.is_file() and _REPORT_NAME_RE.match(p.name)):
                continue
            rel = by_file.get(p.name, {})
            rows.append({"file": p.name, "version": rel.get("version"), "previous": rel.get("previous"),
                         "generated_at": rel.get("generated_at") or _iso(_mtime(p)),
                         "breaking": rel.get("breaking"), "additive": rel.get("additive"),
                         "size": p.stat().st_size, "url": f"/api/reports/{p.name}"})
    rows.sort(key=lambda r: r["generated_at"] or "", reverse=True)
    return {"current_version": data.get("version") or "1.0.0", "reports": rows, "dir": str(folder)}


@app.get("/api/reports/{name}")
def get_report(name: str):
    """Download one report. `name` must match the exact file pattern report.py produces (never a
    client-supplied path), and resolve inside REPORTS_DIR."""
    if not _REPORT_NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="unknown report")
    folder = Path(REPORTS_DIR).resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        raise HTTPException(status_code=404, detail="report not found")
    return FileResponse(str(path), media_type=_DOCX_MEDIA, filename=name)


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
