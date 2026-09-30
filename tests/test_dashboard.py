"""Tests for dashboard/app.py with FastAPI's TestClient. Run from repo root: pytest -q"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard import app as dash  # noqa: E402
from main import cmd_build  # noqa: E402

SAMPLE = ROOT / "sample_logs.jsonl"


@pytest.fixture
def out(tmp_path, monkeypatch) -> Path:
    """Point the dashboard at an empty temp output folder."""
    d = tmp_path / "output"
    d.mkdir()
    monkeypatch.setattr(dash, "SPEC_PATH", d / "openapi.yaml")
    monkeypatch.setattr(dash, "CHANGES_PATH", d / "changes.jsonl")
    monkeypatch.setattr(dash, "QUALITY_PATH", d / "quality.json")
    return d


@pytest.fixture
def client() -> TestClient:
    return TestClient(dash.app)


@pytest.fixture
def real_spec(out, capsys) -> dict:
    """A real spec built from sample_logs.jsonl by the actual pipeline."""
    assert cmd_build(str(SAMPLE), str(out / "openapi.yaml")) == 0
    capsys.readouterr()
    import yaml
    return yaml.safe_load((out / "openapi.yaml").read_text(encoding="utf-8"))


def change(kind: str, breaking: bool, n: int, method: str = "GET", path: str = "/users/{id}") -> dict:
    return {"kind": kind, "method": method, "path": path, "location": f"response.200.body.f{n}",
            "detail": "d", "breaking": breaking, "detected_at": f"2026-09-28T10:00:{n:02d}.000Z"}


def write_changes(out: Path, rows: list[dict], extra: str = "") -> None:
    (out / "changes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + extra,
                                       encoding="utf-8", newline="\n")


# ---------- missing files ----------

def test_spec_missing_returns_empty_spec(out, client):
    r = client.get("/api/spec")
    assert r.status_code == 200
    spec = r.json()
    assert spec["openapi"].startswith("3.") and spec["paths"] == {}


def test_changes_missing_returns_empty_list(out, client):
    r = client.get("/api/changes")
    assert r.status_code == 200 and r.json() == []


def test_summary_with_no_files(out, client):
    s = client.get("/api/summary").json()
    assert s["endpoint_count"] == 0
    assert s["total_changes"] == 0
    assert s["breaking_changes"] == 0
    assert s["last_updated"] is None
    assert s["spec_exists"] is False


def test_output_folder_missing_entirely(tmp_path, monkeypatch, client):
    monkeypatch.setattr(dash, "SPEC_PATH", tmp_path / "nope" / "openapi.yaml")
    monkeypatch.setattr(dash, "CHANGES_PATH", tmp_path / "nope" / "changes.jsonl")
    monkeypatch.setattr(dash, "QUALITY_PATH", tmp_path / "nope" / "quality.json")
    assert client.get("/api/spec").json()["paths"] == {}
    assert client.get("/api/changes").json() == []
    assert client.get("/api/summary").json()["endpoint_count"] == 0
    assert client.get("/api/quality").json()["endpoints"] == []


def test_quality_missing_returns_empty_but_well_shaped_report(out, client):
    r = client.get("/api/quality")
    assert r.status_code == 200
    body = r.json()
    assert body["endpoints"] == [] and body["ambiguities"] == [] and body["parse_rate"] is None


def test_quality_garbage_file_is_treated_as_empty(out, client):
    (out / "quality.json").write_text("not json at all", encoding="utf-8")
    assert client.get("/api/quality").json()["endpoints"] == []


def test_garbage_spec_file_is_treated_as_empty(out, client):
    (out / "openapi.yaml").write_text("paths: [unclosed\n  : : :", encoding="utf-8")
    assert client.get("/api/spec").json()["paths"] == {}
    (out / "openapi.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    assert client.get("/api/spec").json()["paths"] == {}
    assert client.get("/api/summary").json()["endpoint_count"] == 0


# ---------- real spec ----------

def test_spec_endpoint_returns_real_spec(real_spec, client):
    spec = client.get("/api/spec").json()
    assert spec == real_spec
    assert "/users/{id}" in spec["paths"]
    get_user = spec["paths"]["/users/{id}"]["get"]
    assert get_user["x-sample-count"] > 0
    assert "200" in get_user["responses"]


def test_quality_endpoint_returns_real_report(real_spec, client):
    q = client.get("/api/quality").json()
    assert q["lines_read"] == 200
    assert q["parse_rate"] == 1.0
    assert q["endpoint_count"] == 7
    assert len(q["endpoints"]) == 7
    row = next(e for e in q["endpoints"] if e["method"] == "GET" and e["path"] == "/users/{id}")
    assert row["confidence"] in ("high", "medium", "low")
    assert row["sample_count"] == real_spec["paths"]["/users/{id}"]["get"]["x-sample-count"]


def test_summary_counts_real_spec(real_spec, client):
    s = client.get("/api/summary").json()
    expected = sum(1 for item in real_spec["paths"].values() for m in item if m in ("get", "post", "put", "delete", "patch"))
    assert s["endpoint_count"] == expected == 7
    assert s["spec_exists"] is True
    assert s["last_updated"].endswith("Z")


def test_endpoints_extra_route_lists_statuses_and_samples(real_spec, client):
    rows = client.get("/api/endpoints").json()
    assert len(rows) == 7
    row = next(r for r in rows if r["method"] == "GET" and r["path"] == "/users/{id}")
    assert 200 in row["statuses"] and 404 in row["statuses"]
    assert row["sample_count"] == real_spec["paths"]["/users/{id}"]["get"]["x-sample-count"]


def test_spec_is_reread_on_every_request(out, client, capsys):
    assert client.get("/api/summary").json()["endpoint_count"] == 0
    cmd_build(str(SAMPLE), str(out / "openapi.yaml"))
    capsys.readouterr()
    assert client.get("/api/summary").json()["endpoint_count"] == 7


# ---------- changes ----------

def test_changes_newest_first(out, client):
    rows = [change("endpoint_added", False, 1), change("field_added", False, 2), change("field_removed", True, 3)]
    write_changes(out, rows)
    got = client.get("/api/changes").json()
    assert [r["detected_at"] for r in got] == [rows[2]["detected_at"], rows[1]["detected_at"], rows[0]["detected_at"]]
    assert got[0] == rows[2]


def test_changes_limit(out, client):
    write_changes(out, [change("field_added", False, i) for i in range(5)])
    got = client.get("/api/changes", params={"limit": 2}).json()
    assert [r["location"] for r in got] == ["response.200.body.f4", "response.200.body.f3"]


def test_summary_counts_changes_and_breaking(real_spec, out, client):
    write_changes(out, [change("endpoint_added", False, 1), change("field_removed", True, 2),
                        change("type_changed", True, 3), change("status_added", False, 4)])
    s = client.get("/api/summary").json()
    assert s["total_changes"] == 4
    assert s["breaking_changes"] == 2
    assert s["endpoint_count"] == 7


def test_bad_and_partial_change_lines_are_skipped(out, client):
    write_changes(out, [change("field_added", False, 1), change("field_removed", True, 2)],
                  extra='not json\n\n["a list"]\n{"kind": "half-writ')
    got = client.get("/api/changes").json()
    assert len(got) == 2
    assert client.get("/api/summary").json()["breaking_changes"] == 1


def test_alerts_extra_route_only_breaking(out, client):
    write_changes(out, [change("field_added", False, 1), change("field_removed", True, 2),
                        {**change("type_changed", True, 3), "breaking": "yes"}])  # only a real True counts
    got = client.get("/api/alerts").json()
    assert [r["kind"] for r in got] == ["field_removed"]


# ---------- index page ----------

def test_index_serves_dashboard_html(out, client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "API Contract Dashboard" in html
    for needle in ("/api/summary", "/api/spec", "/api/changes", "http://127.0.0.1:4010", "x-sample-count", "POLL_MS = 2000"):
        assert needle in html, needle


# ---------- /api/sample ----------

def _write_log(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8", newline="\n")


def _traffic(method: str, path: str, status: int, body: dict, ts: str = "t") -> dict:
    return {"timestamp": ts, "method": method, "path": path, "status": status,
            "request_body": None, "response_body": body, "headers": {}}


def test_sample_missing_log_returns_log_exists_false(tmp_path, monkeypatch, client):
    monkeypatch.setattr(dash, "LOG_PATH", tmp_path / "nope.jsonl")
    body = client.get("/api/sample", params={"method": "GET", "path": "/x"}).json()
    assert body["log_exists"] is False and body["matches"] == []


def test_sample_matches_method_path_status_newest_first(tmp_path, monkeypatch, client):
    log = tmp_path / "live.jsonl"
    _write_log(log, [
        _traffic("GET", "/users/1", 200, {"id": 1}),
        _traffic("GET", "/users/2", 200, {"id": 2}),
        _traffic("GET", "/users/3", 404, {"error": "x"}),
    ])
    monkeypatch.setattr(dash, "LOG_PATH", log)
    body = client.get("/api/sample", params={"method": "GET", "path": "/users/{id}", "status": 200}).json()
    assert body["log_exists"] is True
    assert [m["path"] for m in body["matches"]] == ["/users/2", "/users/1"]  # newest first


def test_sample_limit_is_capped_at_max(tmp_path, monkeypatch, client):
    log = tmp_path / "live.jsonl"
    _write_log(log, [_traffic("GET", "/x", 200, {"n": i}) for i in range(20)])
    monkeypatch.setattr(dash, "LOG_PATH", log)
    body = client.get("/api/sample", params={"method": "GET", "path": "/x", "limit": 50}).json()
    assert len(body["matches"]) == dash.MAX_SAMPLE_MATCHES


def test_sample_tail_read_ignores_entries_before_the_window(tmp_path, monkeypatch, client):
    # /api/sample only wants the newest matches, so read_traffic_log bounds itself to MAX_TAIL_BYTES —
    # shrink that window to just fit the newest line and confirm the older one is invisible to it.
    log = tmp_path / "live.jsonl"
    old, new = _traffic("GET", "/x", 200, {"which": "old"}), _traffic("GET", "/x", 200, {"which": "new"})
    _write_log(log, [old, new])
    monkeypatch.setattr(dash, "LOG_PATH", log)
    # +20 slack so the seek lands inside `old`'s line (not exactly on the boundary before `new`) —
    # the subsequent readline() then reliably discards the rest of `old` rather than all of `new`.
    monkeypatch.setattr(dash, "MAX_TAIL_BYTES", len(json.dumps(new)) + 20)
    body = client.get("/api/sample", params={"method": "GET", "path": "/x"}).json()
    assert [m["response_body"]["which"] for m in body["matches"]] == ["new"]


# ---------- /api/raw ----------

def test_raw_unknown_name_is_404(client):
    r = client.get("/api/raw/nope")
    assert r.status_code == 404


def test_raw_spec_missing_reports_not_exists(out, client):
    body = client.get("/api/raw/spec").json()
    assert body == {"name": "spec", "path": str(out / "openapi.yaml"), "exists": False, "content": ""}


def test_raw_spec_returns_full_file_contents(out, client):
    (out / "openapi.yaml").write_text("openapi: 3.0.3\npaths: {}\n", encoding="utf-8", newline="\n")
    body = client.get("/api/raw/spec").json()
    assert body["exists"] is True
    assert body["content"] == "openapi: 3.0.3\npaths: {}\n"


def test_raw_mock_spec_derives_path_from_spec_path(out, client):
    (out / "openapi.mock.json").write_text('{"openapi": "3.0.3"}', encoding="utf-8")
    body = client.get("/api/raw/mock-spec").json()
    assert body["path"] == str(out / "openapi.mock.json")
    assert body["exists"] is True and body["content"] == '{"openapi": "3.0.3"}'


def test_raw_quality_reads_quality_json(out, client):
    (out / "quality.json").write_text('{"lines_read": 5}', encoding="utf-8")
    body = client.get("/api/raw/quality").json()
    assert body["exists"] is True and body["content"] == '{"lines_read": 5}'


def test_raw_changes_reads_changes_jsonl(out, client):
    write_changes(out, [change("field_removed", True, 1)])
    body = client.get("/api/raw/changes").json()
    assert body["exists"] is True
    assert "field_removed" in body["content"]


def test_raw_prism_log_missing_reports_not_exists(out, client):
    body = client.get("/api/raw/prism-log").json()
    assert body["exists"] is False
    assert body["path"] == str(out / "prism.log")


def test_raw_prism_log_reads_next_to_spec(out, client):
    (out / "prism.log").write_text("prism started\nlistening on 4010\n", encoding="utf-8")
    body = client.get("/api/raw/prism-log").json()
    assert body["exists"] is True and "listening on 4010" in body["content"]


def test_raw_traffic_log_reads_live_log(tmp_path, monkeypatch, client):
    log = tmp_path / "live.jsonl"
    _write_log(log, [_traffic("GET", "/x", 200, {"n": 1})])
    monkeypatch.setattr(dash, "LOG_PATH", log)
    body = client.get("/api/raw/traffic-log").json()
    assert body["exists"] is True and '"n": 1' in body["content"]


def test_raw_traffic_log_tails_large_files(tmp_path, monkeypatch, client):
    log = tmp_path / "live.jsonl"
    old, new = _traffic("GET", "/x", 200, {"which": "old"}), _traffic("GET", "/x", 200, {"which": "new"})
    _write_log(log, [old, new])
    monkeypatch.setattr(dash, "LOG_PATH", log)
    monkeypatch.setattr(dash, "MAX_TAIL_BYTES", len(json.dumps(new)) + 20)
    body = client.get("/api/raw/traffic-log").json()
    assert "old" not in body["content"] and "new" in body["content"]


def test_raw_full_read_truncates_past_cap(out, client, monkeypatch):
    monkeypatch.setattr(dash, "MAX_RAW_FULL_BYTES", 10)
    (out / "openapi.yaml").write_text("x" * 100, encoding="utf-8")
    body = client.get("/api/raw/spec").json()
    assert body["content"].startswith("x" * 10)
    assert "truncated" in body["content"]


def test_raw_download_sets_content_disposition(out, client):
    (out / "openapi.yaml").write_text("openapi: 3.0.3\n", encoding="utf-8", newline="\n")
    r = client.get("/api/raw/spec", params={"download": "1"})
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="openapi.yaml"'
    assert r.text == "openapi: 3.0.3\n"


def test_raw_download_missing_file_is_404(out, client):
    r = client.get("/api/raw/spec", params={"download": "1"})
    assert r.status_code == 404


# ---------- independence ----------

def test_dashboard_never_imports_watcher():
    tree = ast.parse((ROOT / "dashboard" / "app.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "watcher" not in imported
    assert not imported & {"parser", "normalizer", "inferrer", "spec_builder", "models"}


def test_cmd_dashboard_points_app_at_spec_folder(tmp_path, monkeypatch):
    import uvicorn
    import main
    ran = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.update(app=app, **kw))
    monkeypatch.setattr(dash, "SPEC_PATH", dash.SPEC_PATH)      # restored after the test
    monkeypatch.setattr(dash, "CHANGES_PATH", dash.CHANGES_PATH)
    spec = tmp_path / "demo" / "openapi.yaml"
    assert main.cmd_dashboard("127.0.0.1", 8123, str(spec)) == 0
    assert ran["app"] is dash.app and ran["host"] == "127.0.0.1" and ran["port"] == 8123
    assert dash.SPEC_PATH == spec and dash.CHANGES_PATH == spec.parent / "changes.jsonl"
