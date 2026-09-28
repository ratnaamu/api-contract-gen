"""dashboard/app.py — FastAPI app showing endpoints, diffs and alerts.   Owner: [Name 4]

Reads ONLY output/openapi.yaml and output/changes.jsonl (written by watcher), so it works
standalone against any spec. The HTML page polls /api/* every 2s.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse

SPEC_PATH = Path("output/openapi.yaml")
CHANGES_PATH = Path("output/changes.jsonl")
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="API Contract Dashboard")


@app.get("/")
def index() -> FileResponse:
    """Serve static/index.html."""
    raise NotImplementedError


@app.get("/api/endpoints")
def list_endpoints() -> list[dict[str, Any]]:
    """Flatten the spec into rows: [{"method", "path", "statuses": [200, 404], "summary"}]. [] if no spec yet."""
    raise NotImplementedError


@app.get("/api/changes")
def list_changes(limit: int = 100) -> list[dict[str, Any]]:
    """Last `limit` SpecChange rows from changes.jsonl, newest first."""
    raise NotImplementedError


@app.get("/api/alerts")
def list_alerts() -> list[dict[str, Any]]:
    """Only changes with breaking=True, newest first."""
    raise NotImplementedError


@app.get("/api/spec")
def get_spec() -> dict[str, Any]:
    """The current spec as JSON ({} if none)."""
    raise NotImplementedError


@app.get("/api/health")
def health() -> dict[str, Any]:
    """{"spec_exists": bool, "endpoint_count": int, "prism_port": 4010}."""
    raise NotImplementedError
