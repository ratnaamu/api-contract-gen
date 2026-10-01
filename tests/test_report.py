"""report.py — the Word change report written on every breaking burst."""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import report  # noqa: E402
import watcher  # noqa: E402
from dashboard import app as dash  # noqa: E402
from models import SpecChange  # noqa: E402

SPEC = {
    "openapi": "3.0.3", "info": {"title": "Passport API", "version": "0.1.0"},
    "paths": {
        "/applications/{id}": {"get": {
            "operationId": "get_app", "summary": "GET /applications/{id}", "x-sample-count": 42,
            "security": [{"bearerAuth": []}],
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "responses": {
                "200": {"description": "OK", "content": {"application/json": {"schema": {
                    "type": "object", "required": ["id", "status", "birth_date"], "additionalProperties": False,
                    "properties": {"id": {"type": "string"}, "status": {"type": "string", "enum": ["submitted", "issued"]},
                                   "birth_date": {"type": "string", "format": "date"},
                                   "office": {"type": "object", "required": ["id"], "additionalProperties": False,
                                              "properties": {"id": {"type": "string"}, "city": {"type": "string"}}},
                                   "history": {"type": "array", "items": {"type": "object", "properties": {"at": {"type": "string"}}}}}}}}},
                "404": {"description": "Not Found", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"error": {"type": "string"}}}}}},
            }}},
        "/applications": {"post": {
            "operationId": "post_app", "summary": "POST /applications",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["full_name"], "properties": {"full_name": {"type": "string"},
                                                                             "consent": {"type": "boolean"}}}}}},
            "responses": {"201": {"description": "Created"}},
        }},
    },
}


def change(kind, method, path, location, detail, breaking, at="2026-10-01T10:00:00Z") -> SpecChange:
    return SpecChange(kind=kind, method=method, path=path, location=location, detail=detail, breaking=breaking, detected_at=at)


CHANGES = [
    change("field_renamed", "GET", "/applications/{id}", "response.200.body.date_of_birth",
           "date_of_birth appears to be renamed to birth_date", True),
    change("field_added", "POST", "/applications", "request.body.consent",
           "new required field: sent in every successful request since it appeared (10+)", True),
    change("type_changed", "GET", "/applications/{id}", "response.200.body.pages", "type integer -> integer|string", True),
    change("field_removed", "GET", "/applications/{id}", "response.200.body.updated_at", "missing from the last 10+ responses", True),
    change("status_added", "GET", "/applications/{id}", "response.404", "new status 404", False),
    change("field_added", "GET", "/applications/{id}", "response.200.body.tracking_number", "new field", False),
]


def docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode("utf-8")


# ---------- building blocks ----------

def test_semver_bump():
    assert report.bump("1.0.0", breaking=True) == "2.0.0"
    assert report.bump("2.3.0", breaking=False) == "2.4.0"
    assert report.bump("garbage", breaking=True) == "2.0.0"


@pytest.mark.parametrize("c, expect", [
    (CHANGES[0], "Read `birth_date` instead of `date_of_birth`"),
    (CHANGES[1], "Send `consent` in every request"),
    (CHANGES[2], "Update parsing/validation of `pages`"),
    (CHANGES[3], "Remove any dependency on `updated_at`"),
    (CHANGES[4], "Handle this status"),
    (CHANGES[5], "optional for consumers"),
])
def test_migration_hints_speak_to_consumers(c, expect):
    assert expect in report.migration_hint(report._change_dict(c))


def test_flatten_fields_walks_nested_objects_and_arrays():
    schema = SPEC["paths"]["/applications/{id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    rows = report.flatten_fields(schema)
    names = [n for n, _, _ in rows]
    assert names == ["id", "status", "birth_date", "office", "office.id", "office.city", "history", "history[].at"]
    types = dict((n, t) for n, t, _ in rows)
    assert types["status"].startswith("enum: submitted, issued") and types["birth_date"] == "string (date)"
    assert dict((n, r) for n, _, r in rows)["office.city"] == "optional"


def test_build_report_writes_a_readable_docx(tmp_path):
    out = report.build_report(SPEC, CHANGES, tmp_path / "r.docx", old_version="1.0.0", new_version="2.0.0",
                              releases=[{"version": "1.0.0", "generated_at": "2026-10-01T09:00:00Z", "breaking": 0,
                                         "additive": 3, "file": "contract-report-v1.0.0-x.docx"}],
                              log_path="live_logs.jsonl")
    assert out.exists() and out.stat().st_size > 10_000
    xml = docx_text(out)
    for needle in ("API Contract Change Report", "version 1.0.0", "2.0.0", "Breaking changes (4)", "Other changes (2)",
                   "Read `birth_date` instead of `date_of_birth`", "Current contract", "GET /applications/{id}",
                   "changed in this release", "office.city", "Release history", "live_logs.jsonl"):
        assert needle in xml, needle
    assert "Passport API" in xml


def test_llm_summary_is_used_when_available_and_ignored_when_not(tmp_path):
    class Llm:
        def chat(self, system, user):
            assert "1.0.0 -> 2.0.0" in user and "field_renamed" in user
            return "Three fields changed shape. Frontend teams reading applications are affected. Update the readers."
    out = report.build_report(SPEC, CHANGES, tmp_path / "a.docx", old_version="1.0.0", new_version="2.0.0", llm=Llm())
    assert "Summary written by the configured LLM" in docx_text(out)

    class Broken:
        def chat(self, system, user):
            raise RuntimeError("gateway down")
    out = report.build_report(SPEC, CHANGES, tmp_path / "b.docx", old_version="1.0.0", new_version="2.0.0", llm=Broken())
    xml = docx_text(out)
    assert "Summary written by the configured LLM" not in xml and "contract moved from version 1.0.0 to 2.0.0" in xml


# ---------- the reporter: one report per breaking burst ----------

def test_reporter_waits_for_the_burst_to_settle_then_versions_and_records(tmp_path):
    said = []
    rep = report.ChangeReporter(tmp_path / "out" / "openapi.yaml", quiet_seconds=5.0, say=said.append)
    rep.notify(SPEC, CHANGES[4:], now=100.0)              # additive only: never a report on its own
    assert rep.flush_if_quiet(200.0) is None and rep.pending_breaking == 0 and len(rep.pending) == 2
    rep.notify(SPEC, CHANGES[:2], now=210.0)             # breaking arrives
    assert rep.flush_if_quiet(212.0) is None             # still settling
    rep.notify(SPEC, CHANGES[2:4], now=214.0)            # more of the same burst
    assert rep.flush_if_quiet(218.0) is None             # quiet window restarted
    out = rep.flush_if_quiet(219.5)
    assert out is not None and out.exists() and out.name.startswith("contract-report-v2.0.0-")
    assert (tmp_path / "out" / report.LATEST_NAME).exists()
    assert rep.pending == [] and rep.state.version == "2.0.0"
    releases = json.loads((tmp_path / "out" / "reports" / "releases.json").read_text())
    assert releases["version"] == "2.0.0"
    assert releases["releases"][0]["breaking"] == 4 and releases["releases"][0]["additive"] == 2
    assert any("REPORT: contract v1.0.0 -> v2.0.0" in m for m in said)
    # the next burst builds on the recorded version and lists the previous release in its history
    rep.notify(SPEC, CHANGES[:1], now=300.0)
    out2 = rep.flush()
    assert out2.name.startswith("contract-report-v3.0.0-") and "2.0.0" in docx_text(out2)
    assert len(json.loads((tmp_path / "out" / "reports" / "releases.json").read_text())["releases"]) == 2


def test_reporter_flush_without_breaking_or_spec_is_a_noop(tmp_path):
    rep = report.ChangeReporter(tmp_path / "openapi.yaml")
    assert rep.flush() is None
    rep.notify(SPEC, CHANGES[4:], now=1.0)
    assert rep.flush() is None and len(rep.pending) == 2


def test_reporter_survives_a_failing_build_and_retries(tmp_path, monkeypatch):
    said = []
    rep = report.ChangeReporter(tmp_path / "openapi.yaml", say=said.append)
    rep.notify(SPEC, CHANGES[:1], now=1.0)
    monkeypatch.setattr(report, "build_report", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert rep.flush() is None and rep.pending_breaking == 1 and rep.state.version == "1.0.0"
    assert any("could not write change report" in m for m in said)


# ---------- watcher hook ----------

def test_watcher_feeds_the_reporter_and_flushes_on_stop(tmp_path, monkeypatch):
    class Rep:
        def __init__(self):
            self.notified, self.quiet, self.flushed = [], 0, 0
        def notify(self, spec, changes, now):
            self.notified.append(list(changes))
        def flush_if_quiet(self, now):
            self.quiet += 1
        def flush(self):
            self.flushed += 1
    rep = Rep()
    log = tmp_path / "log.jsonl"
    log.write_text("", encoding="utf-8")
    w = watcher.ContractWatcher(log, tmp_path / "out" / "openapi.yaml", tmp_path / "out" / "changes.jsonl", reporter=rep)
    w.initialize()
    log.write_text(json.dumps({"timestamp": "2026-10-01T10:00:00Z", "method": "GET", "path": "/offices", "status": 200,
                               "response_body": [{"id": "OFF-LON"}]}) + "\n", encoding="utf-8")
    changes = w.process()
    assert changes and rep.notified == [changes]
    # watch(): flush_if_quiet on ticks, flush at shutdown
    import threading
    stop = threading.Event()
    threading.Timer(0.6, stop.set).start()
    watcher.watch(log, tmp_path / "out" / "openapi.yaml", tmp_path / "out" / "changes.jsonl", debounce_seconds=0.1,
                  stop_event=stop, poll_interval=0.1, reporter=rep)
    assert rep.quiet > 0 and rep.flushed == 1


# ---------- dashboard ----------

def test_dashboard_lists_and_serves_reports_safely(tmp_path, monkeypatch):
    out = tmp_path / "out"
    rep = report.ChangeReporter(out / "openapi.yaml", quiet_seconds=0)
    rep.notify(SPEC, CHANGES, now=1.0)
    written = rep.flush()
    (out / "reports" / "not-a-report.docx").write_bytes(b"x")
    monkeypatch.setattr(dash, "REPORTS_DIR", out / "reports")
    c = TestClient(dash.app)
    data = c.get("/api/reports").json()
    assert data["current_version"] == "2.0.0" and [r["file"] for r in data["reports"]] == [written.name]
    r0 = data["reports"][0]
    assert r0["breaking"] == 4 and r0["additive"] == 2 and r0["previous"] == "1.0.0" and r0["size"] > 10_000
    dl = c.get(r0["url"])
    assert dl.status_code == 200 and dl.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert zipfile.ZipFile(io.BytesIO(dl.content)).namelist()
    assert c.get("/api/reports/not-a-report.docx").status_code == 404
    assert c.get("/api/reports/..%2Freleases.json").status_code == 404
    assert c.get("/api/reports/contract-report-v9.9.9-20260101-000000.docx").status_code == 404


def test_dashboard_reports_endpoint_with_nothing_written(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "REPORTS_DIR", tmp_path / "nope")
    data = TestClient(dash.app).get("/api/reports").json()
    assert data == {"current_version": "1.0.0", "reports": [], "dir": str(tmp_path / "nope")}
