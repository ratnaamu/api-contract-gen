"""Tests for llm_refine.py. Run from repo root: pytest -q

No real network calls: AidhClient.chat() is exercised against a monkeypatched `requests.post`, and the
refine_* functions are exercised against a small FakeClient so their prompt-building/JSON-parsing logic
is covered without depending on AidhClient's HTTP details.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inferrer import infer_endpoint  # noqa: E402
from llm_refine import (  # noqa: E402
    AidhClient, refine_field_descriptions, refine_operation_summary,
)
from models import EndpointSchema  # noqa: E402
from spec_builder import build_operation  # noqa: E402


class FakeClient:
    """Duck-types AidhClient.chat() with a canned reply (or a queue of replies)."""

    def __init__(self, reply):
        self._replies = reply if isinstance(reply, list) else [reply]
        self.calls: list[tuple[str, str]] = []

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
