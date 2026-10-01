"""Tests for llm_refine.py. Run from repo root: pytest -q

No real network calls: AidhClient.chat() is exercised against a monkeypatched `requests.post`, and the
refine_* functions are exercised against a small FakeClient so their prompt-building/JSON-parsing logic
is covered without depending on AidhClient's HTTP details.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import watcher as watcher_module  # noqa: E402
from inferrer import infer_endpoint  # noqa: E402
from llm_refine import (  # noqa: E402
    AidhClient, refine_field_descriptions, refine_operation_summary, suggest_error_statuses,
)
from models import EndpointSchema  # noqa: E402
from spec_builder import build_operation, load_spec  # noqa: E402
from watcher import ContractWatcher, build_from_entries  # noqa: E402


class FakeClient:
    """Duck-types AidhClient.chat() with a canned reply (or a queue of replies)."""

    def __init__(self, reply):
        self._replies = reply if isinstance(reply, list) else [reply]
        self.calls: list[tuple[str, str]] = []
        self.model = "fake-model"

    def chat(self, system: str, user: str) -> str | None:
        self.calls.append((system, user))
        return self._replies.pop(0) if self._replies else None


# ---------- AidhClient.from_env ----------

def test_from_env_requires_all_three(monkeypatch):
    monkeypatch.delenv("AIDH_BASE_URL", raising=False)
    monkeypatch.delenv("AIDH_DOMAIN_ID", raising=False)
    monkeypatch.delenv("AIDH_MODEL", raising=False)
    assert AidhClient.from_env() is None

    monkeypatch.setenv("AIDH_BASE_URL", "https://aidh.example.com")
    monkeypatch.setenv("AIDH_DOMAIN_ID", "kumarip4")
    assert AidhClient.from_env() is None  # model still missing

    monkeypatch.setenv("AIDH_MODEL", "llama3.1:8b")
    client = AidhClient.from_env()
    assert client is not None
    assert client.base_url == "https://aidh.example.com"  # trailing slash stripped
    assert client.domain_id == "kumarip4"
    assert client.model == "llama3.1:8b"


# ---------- AidhClient.chat wire format + caching ----------

def test_chat_posts_ollama_generate_payload_with_bearer_domain_auth(monkeypatch):
    seen = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": " hello "}

    def fake_post(url, json, headers, timeout):
        seen["url"], seen["json"], seen["headers"], seen["timeout"] = url, json, headers, timeout
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    client = AidhClient(base_url="https://aidh.example.com", domain_id="kumarip4", model="llama3.1:8b")
    result = client.chat("sys prompt", "user prompt")

    assert result == "hello"  # stripped
    assert seen["url"] == "https://aidh.example.com/api/generate"
    assert seen["json"]["model"] == "llama3.1:8b"
    assert seen["json"]["stream"] is False
    assert seen["json"]["prompt"] == "sys prompt\n\nuser prompt"
    assert seen["headers"]["Authorization"] == "Bearer kumarip4:UNISYS"  # domain_id is the credential
    assert "domain_id" not in seen["json"]  # not sent in the body


def test_chat_caches_identical_calls(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "cached reply"}

    def fake_post(url, json, headers, timeout):
        calls.append(1)
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    client = AidhClient(base_url="https://aidh.example.com", domain_id="d", model="m")
    assert client.chat("sys", "user") == "cached reply"
    assert client.chat("sys", "user") == "cached reply"
    assert len(calls) == 1  # second call served from the in-memory cache


def test_chat_never_raises_on_network_failure(monkeypatch):
    def fake_post(*a, **kw):
        raise ConnectionError("no route to host")

    monkeypatch.setattr("requests.post", fake_post)
    client = AidhClient(base_url="https://aidh.example.com", domain_id="d", model="m")
    assert client.chat("sys", "user") is None  # never raises


# ---------- refine_field_descriptions ----------

def test_refine_field_descriptions_llm_none_is_noop():
    schema = {"type": "object", "properties": {"id": {"type": "string"}}}
    refine_field_descriptions(schema, [{"id": "abc"}], "ctx", None)
    assert "description" not in schema["properties"]["id"]


def test_refine_field_descriptions_applies_mapping():
    schema = {"type": "object", "properties": {
        "id": {"type": "string"},
        "email": {"type": "string", "description": "already described"},
    }}
    client = FakeClient('{"id": "Unique identifier for the user"}')
    refine_field_descriptions(schema, [{"id": "u1", "email": "a@b.com"}], "GET /users/{id} response", client)

    assert schema["properties"]["id"]["description"] == "Unique identifier for the user"
    assert schema["properties"]["email"]["description"] == "already described"  # untouched: already had one
    assert len(client.calls) == 1
    assert "id (string)" in client.calls[0][1]
    assert "email" not in client.calls[0][1]  # already-described fields aren't even asked about


def test_refine_field_descriptions_bad_reply_is_noop():
    schema = {"type": "object", "properties": {"id": {"type": "string"}}}
    client = FakeClient("not json at all")
    refine_field_descriptions(schema, [{"id": "abc"}], "ctx", client)
    assert "description" not in schema["properties"]["id"]


def test_refine_field_descriptions_tolerates_prose_around_json():
    schema = {"type": "object", "properties": {"id": {"type": "string"}}}
    client = FakeClient('Sure, here you go:\n```json\n{"id": "An identifier"}\n```')
    refine_field_descriptions(schema, [{"id": "abc"}], "ctx", client)
    assert schema["properties"]["id"]["description"] == "An identifier"


def test_refine_field_descriptions_recurses_into_nested_objects():
    schema = {"type": "object", "properties": {
        "address": {"type": "object", "properties": {"city": {"type": "string"}}},
    }}
    client = FakeClient('{"address": "Mailing address", "address.city": "City name"}')
    refine_field_descriptions(schema, [{"address": {"city": "Reno"}}], "ctx", client)
    assert schema["properties"]["address"]["description"] == "Mailing address"
    assert schema["properties"]["address"]["properties"]["city"]["description"] == "City name"


def test_refine_field_descriptions_tolerates_leading_slash_keys():
    # observed from a real AIDH (llama3.1:8b) reply: keys came back as "/id", "/name", ... instead
    # of "id", "name", ...
    schema = {"type": "object", "properties": {
        "id": {"type": "string"}, "name": {"type": "string"},
    }}
    client = FakeClient('{"/id": "internal identifier", "/name": "product name"}')
    refine_field_descriptions(schema, [{"id": "1", "name": "Widget"}], "ctx", client)
    assert schema["properties"]["id"]["description"] == "internal identifier"
    assert schema["properties"]["name"]["description"] == "product name"


def test_refine_field_descriptions_tolerates_slash_as_nesting_separator():
    schema = {"type": "object", "properties": {
        "address": {"type": "object", "properties": {"city": {"type": "string"}}},
    }}
    client = FakeClient('{"address/city": "City name"}')
    refine_field_descriptions(schema, [{"address": {"city": "Reno"}}], "ctx", client)
    assert schema["properties"]["address"]["properties"]["city"]["description"] == "City name"


# ---------- refine_operation_summary ----------

def make_ep(**overrides) -> EndpointSchema:
    base = dict(method="GET", path_template="/users/{id}", examples={200: {"id": "u1", "email": "a@b.com"}})
    base.update(overrides)
    return EndpointSchema(**base)


def test_refine_operation_summary_llm_none_returns_none():
    assert refine_operation_summary(make_ep(), None) is None


def test_refine_operation_summary_applies_parsed_reply():
    client = FakeClient('{"summary": "Get a user", "description": "Returns one user by id."}')
    result = refine_operation_summary(make_ep(), client)
    assert result == ("Get a user", "Returns one user by id.")


def test_refine_operation_summary_bad_reply_returns_none():
    client = FakeClient("nonsense")
    assert refine_operation_summary(make_ep(), client) is None


def test_refine_operation_summary_incomplete_reply_returns_none():
    client = FakeClient('{"summary": "Get a user"}')  # missing "description"
    assert refine_operation_summary(make_ep(), client) is None


# ---------- integration: llm=None leaves inferrer/spec_builder unchanged ----------

def test_infer_endpoint_llm_none_matches_default(monkeypatch):
    entries = [{"timestamp": "2026-01-01T00:00:00Z", "method": "GET", "path": "/x", "query": {},
                "request_body": None, "status": 200, "response_body": {"id": "1"}, "headers": {}}]
    assert infer_endpoint("GET", "/x", entries, None) == infer_endpoint("GET", "/x", entries)


def test_build_operation_uses_llm_summary_when_given():
    ep = make_ep()
    client = FakeClient('{"summary": "Get a user", "description": "Returns one user by id."}')
    op = build_operation(ep, client)
    assert op["summary"] == "Get a user"
    assert op["description"] == "Returns one user by id."


def test_build_operation_keeps_placeholder_when_llm_fails():
    ep = make_ep()
    client = FakeClient(None)
    op = build_operation(ep, client)
    assert op["summary"] == "GET /users/{id}"
    assert "description" not in op


# ---------- ContractWatcher: LLM refinement must never block diffing/breaking-change detection ----------
#
# Regression test for a real bug: with a slow (real-network) AIDH client, running LLM refinement inline
# in the rebuild loop stretched rebuilds to tens of seconds, letting a whole burst of traffic — including
# the very evidence needed to detect a field removal/rename — pile up into a single rebuild and collapse
# past it silently (an endpoint that first appears already missing a field is just "endpoint_added", not
# "field_removed"). ContractWatcher now always diffs the plain rule-based spec and refines asynchronously.

class SlowFakeClient:
    """Duck-types AidhClient.chat() (plus the .model attribute build_spec reads) with a deliberate
    delay, like a real network call would have."""

    def __init__(self, delay: float = 0.2):
        self.delay = delay
        self.model = "fake-model"
        self.calls = 0

    def chat(self, system: str, user: str) -> str | None:
        self.calls += 1
        time.sleep(self.delay)
        if "Fields:" in user:
            return '{"id": "an id"}'
        return '{"summary": "Slow summary", "description": "Slow description."}'


def _append_entries(path: Path, entries: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_process_does_not_block_on_slow_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher_module, "LLM_MIN_INTERVAL", 0.0)
    log = tmp_path / "live.jsonl"
    log.write_text("", encoding="utf-8")
    cw = ContractWatcher(log, tmp_path / "openapi.yaml", tmp_path / "changes.jsonl", llm=SlowFakeClient(0.5))
    cw.initialize()

    entries = [{"timestamp": "2026-01-01T00:00:00Z", "method": "GET", "path": "/x", "query": {},
                "request_body": None, "status": 200, "response_body": {"id": "1"}, "headers": {}}]
    _append_entries(log, entries)

    started = time.monotonic()
    cw.process()
    elapsed = time.monotonic() - started

    assert elapsed < 0.3  # must not wait for the 0.5s-per-call slow client
    assert cw.spec == build_from_entries(cw.entries)  # diffing state is the plain rule-based spec


def test_llm_pass_eventually_writes_refined_spec_to_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher_module, "LLM_MIN_INTERVAL", 0.0)
    log = tmp_path / "live.jsonl"
    log.write_text("", encoding="utf-8")
    spec_path = tmp_path / "openapi.yaml"
    cw = ContractWatcher(log, spec_path, tmp_path / "changes.jsonl", llm=SlowFakeClient(0.1))
    cw.initialize()

    entries = [{"timestamp": "2026-01-01T00:00:00Z", "method": "GET", "path": "/x", "query": {},
                "request_body": None, "status": 200, "response_body": {"id": "1"}, "headers": {}}]
    _append_entries(log, entries)
    cw.process()

    deadline = time.monotonic() + 3.0
    on_disk = None
    while time.monotonic() < deadline:
        on_disk = load_spec(spec_path)
        if on_disk and "x-llm-model" in on_disk.get("info", {}):
            break
        time.sleep(0.02)
    assert on_disk is not None and "x-llm-model" in on_disk["info"]


def test_new_entries_during_llm_pass_trigger_a_redo(tmp_path, monkeypatch):
    """If entries arrive while a pass is running, the worker redoes the pass on exit instead of
    silently leaving the newest data undescribed until the next rebuild happens to fire."""
    monkeypatch.setattr(watcher_module, "LLM_MIN_INTERVAL", 0.0)
    log = tmp_path / "live.jsonl"
    log.write_text("", encoding="utf-8")
    client = SlowFakeClient(0.15)
    cw = ContractWatcher(log, tmp_path / "openapi.yaml", tmp_path / "changes.jsonl", llm=client)
    cw.initialize()

    e1 = [{"timestamp": "2026-01-01T00:00:00Z", "method": "GET", "path": "/x", "query": {},
           "request_body": None, "status": 200, "response_body": {"id": "1"}, "headers": {}}]
    _append_entries(log, e1)
    cw.process()  # starts a background pass over 1 entry

    e2 = [{"timestamp": "2026-01-01T00:00:01Z", "method": "GET", "path": "/y", "query": {},
           "request_body": None, "status": 200, "response_body": {"id": "2"}, "headers": {}}]
    _append_entries(log, e2)
    cw.process()  # arrives mid-pass -> should mark _llm_pending instead of starting a second thread

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and (cw._llm_thread is not None or cw._llm_pending):
        time.sleep(0.02)
    assert cw._llm_thread is None and not cw._llm_pending  # settled: no pass left running or queued


# ---------- _merge_llm_descriptions: carrying AI text across plain rebuilds (anti-flicker) ----------
#
# Regression test for a real bug found via live testing: process() rebuilds the plain rule-based spec
# on every new batch of log lines (often every ~1s under steady traffic), while one LLM pass takes much
# longer — so the plain write was almost immediately stomping the enriched file the background thread
# had just written, making the dashboard's AI badge/descriptions flicker on and off instead of just
# "briefly" lagging as intended.

def test_merge_llm_descriptions_carries_forward_matching_fields():
    from watcher import _merge_llm_descriptions

    spec = {
        "info": {"title": "x"},
        "paths": {"/users": {"get": {
            "operationId": "get_users", "summary": "GET /users",
            "responses": {"200": {"description": "", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "new_field": {"type": "integer"}},
            }}}}},
        }}},
    }
    enriched = {
        "info": {"title": "x", "x-llm-model": "fake-model (AIDH)"},
        "paths": {"/users": {"get": {
            "operationId": "get_users", "summary": "List users", "description": "Returns all users.",
            "responses": {"200": {"description": "", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "user id"}},
            }}}}},
        }}},
    }
    merged = _merge_llm_descriptions(spec, enriched)

    assert merged["info"]["x-llm-model"] == "fake-model (AIDH)"
    op = merged["paths"]["/users"]["get"]
    assert op["summary"] == "List users" and op["description"] == "Returns all users."
    props = op["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    assert props["id"]["description"] == "user id"
    assert "description" not in props["new_field"]  # didn't exist in the enriched pass yet -> untouched

    # `spec` itself (process()'s diffing state) must come back unmutated
    assert "description" not in spec["paths"]["/users"]["get"]
    assert "x-llm-model" not in spec["info"]


def test_merge_llm_descriptions_no_cached_pass_is_a_noop():
    from watcher import _merge_llm_descriptions

    spec = {"info": {"title": "x"}, "paths": {}}
    assert _merge_llm_descriptions(spec, {}) == spec


def test_plain_rebuild_does_not_erase_llm_descriptions(tmp_path, monkeypatch):
    """Once an async LLM pass has written an enriched spec, a later plain rebuild (new traffic arriving
    before the next LLM pass completes) must carry its descriptions/x-llm-model forward rather than
    wiping them back out."""
    monkeypatch.setattr(watcher_module, "LLM_MIN_INTERVAL", 9999.0)  # only one pass runs in this test
    log = tmp_path / "live.jsonl"
    log.write_text("", encoding="utf-8")
    spec_path = tmp_path / "openapi.yaml"
    cw = ContractWatcher(log, spec_path, tmp_path / "changes.jsonl", llm=SlowFakeClient(0.05))
    cw.initialize()

    e1 = [{"timestamp": "2026-01-01T00:00:00Z", "method": "GET", "path": "/x", "query": {},
           "request_body": None, "status": 200, "response_body": {"id": "1"}, "headers": {}}]
    _append_entries(log, e1)
    cw.process()  # starts the one allowed async pass

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and cw._last_enriched is None:
        time.sleep(0.02)
    assert cw._last_enriched is not None  # the pass completed and cached its result

    # more traffic arrives; LLM_MIN_INTERVAL blocks a second async pass, so this rebuild is plain-only
    e2 = [{"timestamp": "2026-01-01T00:00:01Z", "method": "GET", "path": "/x", "query": {},
           "request_body": None, "status": 200, "response_body": {"id": "2"}, "headers": {}}]
    _append_entries(log, e2)
    cw.process()

    on_disk = load_spec(spec_path)
    assert "x-llm-model" in on_disk["info"]  # carried forward, not wiped by the plain rebuild
    assert cw.spec is not None and "x-llm-model" not in cw.spec.get("info", {})  # diffing state stays plain


# ---------- suggest_error_statuses (B4) ----------

def test_suggest_error_statuses_llm_none_returns_empty():
    assert suggest_error_statuses("GET", "/x", False, set(), 0.0, None) == []


def test_suggest_error_statuses_parses_valid_reply():
    client = FakeClient('{"suggestions": [{"status": 429, "reason": "rate limited"}]}')
    out = suggest_error_statuses("GET", "/x/{id}", False, {200, 404}, 0.0, client)
    assert out == [{"status": 429, "reason": "rate limited"}]


def test_suggest_error_statuses_excludes_already_known():
    client = FakeClient('{"suggestions": [{"status": 404, "reason": "already known, should be dropped"}, '
                        '{"status": 409, "reason": "conflict"}]}')
    out = suggest_error_statuses("GET", "/x", False, {200, 404}, 0.0, client)
    assert out == [{"status": 409, "reason": "conflict"}]


def test_suggest_error_statuses_rejects_disallowed_status():
    client = FakeClient('{"suggestions": [{"status": 999, "reason": "not a real status"}]}')
    assert suggest_error_statuses("GET", "/x", False, set(), 0.0, client) == []


def test_suggest_error_statuses_caps_at_max():
    many = [{"status": s, "reason": "r"} for s in (400, 401, 403, 404, 405, 409, 422)]
    client = FakeClient(json.dumps({"suggestions": many}))
    out = suggest_error_statuses("GET", "/x", False, set(), 0.0, client)
    assert len(out) == 3  # MAX_LLM_STATUS_SUGGESTIONS


def test_suggest_error_statuses_bad_reply_returns_empty():
    client = FakeClient("not json")
    assert suggest_error_statuses("GET", "/x", False, set(), 0.0, client) == []


def test_suggest_error_statuses_missing_reason_dropped():
    client = FakeClient('{"suggestions": [{"status": 429}]}')  # no "reason"
    assert suggest_error_statuses("GET", "/x", False, set(), 0.0, client) == []


# ---------- build_from_entries: B4 suggestions merge with (never override) the B2 rule matrix ----------

def test_build_from_entries_merges_llm_status_suggestions():
    entries = [{"timestamp": "2026-01-01T00:00:00Z", "method": "GET", "path": "/x", "query": {},
                "request_body": None, "status": 200, "response_body": {"id": 1}, "headers": {}}
              for _ in range(5)]
    client = FakeClient([
        '{}',  # field-description call for the 200 body (no fields worth describing here; harmless either way)
        '{"suggestions": [{"status": 429, "reason": "rate limited"}]}',  # B4 status-suggestion call
        '{"summary": "s", "description": "d"}',  # operation summary call
    ])
    spec = build_from_entries(entries, llm=client, infer_errors=True)
    resp = spec["paths"]["/x"]["get"]["responses"]
    assert resp["429"]["x-inferred"] is True
    assert resp["429"]["x-inferred-by"] == "llm"
    assert resp["429"]["x-evidence"] == "rate limited"
    assert resp["500"]["x-inferred-by"] == "rules"  # the rule matrix's own guess is untouched


def test_build_from_entries_llm_suggestion_never_overrides_rule_based_status():
    entries = [{"timestamp": "2026-01-01T00:00:00Z", "method": "GET", "path": "/x/{id}", "query": {},
                "request_body": None, "status": 200, "response_body": {"id": 1}, "headers": {}}
              for _ in range(5)]
    # the LLM "suggests" 404, which the B2 rule matrix already guessed (path has a param) -> must be
    # ignored, not double-counted or overriding the rule-based entry's evidence/inferred_by.
    client = FakeClient([
        '{}',
        '{"suggestions": [{"status": 404, "reason": "llm thinks this too"}]}',
        '{"summary": "s", "description": "d"}',
    ])
    spec = build_from_entries(entries, llm=client, infer_errors=True)
    resp = spec["paths"]["/x/{id}"]["get"]["responses"]
    assert resp["404"]["x-inferred-by"] == "rules"
    assert resp["404"]["x-evidence"] == "resource lookup by id"
