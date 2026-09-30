"""Tests for errors.py (B1/B2/B3) and its spec_builder wiring. Run from repo root: pytest -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from errors import (  # noqa: E402
    PROBLEM_DETAILS_SCHEMA, collect_api_evidence, collect_evidence, infer_error_responses,
    learn_error_envelope, mine_validation_evidence,
)
from inferrer import infer_endpoint  # noqa: E402
from models import EndpointSchema  # noqa: E402
from normalizer import group_by_endpoint  # noqa: E402
from spec_builder import build_operation, build_spec, validate_spec  # noqa: E402


def entry(method="GET", path="/x", status=200, req=None, resp=None, headers=None):
    return {"timestamp": "", "method": method, "path": path, "query": {}, "request_body": req,
            "status": status, "response_body": resp, "headers": headers or {}}


# ---------- learn_error_envelope (B1) ----------

def test_learn_error_envelope_infers_from_observed_errors():
    entries = [entry(status=404, resp={"error": "not_found", "message": "no such thing"}),
              entry(status=400, resp={"error": "bad_request", "message": "missing field"})]
    schema, observed = learn_error_envelope(entries)
    assert observed is True
    assert set(schema["properties"]) == {"error", "message"}


def test_learn_error_envelope_falls_back_to_problem_details():
    entries = [entry(status=200, resp={"id": 1})]
    schema, observed = learn_error_envelope(entries)
    assert observed is False
    assert schema == PROBLEM_DETAILS_SCHEMA


def test_learn_error_envelope_ignores_error_status_with_no_body():
    entries = [entry(status=404, resp=None)]
    schema, observed = learn_error_envelope(entries)
    assert observed is False


# ---------- collect_api_evidence ----------

def test_collect_api_evidence_flags():
    entries = [entry(status=422), entry(status=200)]
    api = collect_api_evidence(entries)
    assert api.uses_422 is True and api.uses_409 is False and api.rate_limited is False

    api2 = collect_api_evidence([entry(status=409), entry(status=429)])
    assert api2.uses_409 is True and api2.rate_limited is True


# ---------- collect_evidence ----------

def test_collect_evidence_auth_rate_and_path_param():
    entries = [entry(method="GET", path="/users/1", headers={"Authorization": "Bearer x"}) for _ in range(3)]
    entries += [entry(method="GET", path="/users/1")]  # 3/4 have auth
    ep = infer_endpoint("GET", "/users/{id}", entries)
    ev = collect_evidence([ep], group_by_endpoint(entries))[("GET", "/users/{id}")]
    assert ev.has_path_param is True
    assert ev.auth_rate == pytest.approx(0.75)


def test_collect_evidence_sibling_methods():
    entries = ([entry(method="GET", path="/users/1") for _ in range(3)]
              + [entry(method="DELETE", path="/users/1", status=204) for _ in range(3)])
    grouped = group_by_endpoint(entries)
    eps = [infer_endpoint(m, t, e) for (m, t), e in grouped.items()]
    evidence = collect_evidence(eps, grouped)
    assert evidence[("GET", "/users/{id}")].sibling_methods == {"DELETE"}
    assert evidence[("DELETE", "/users/{id}")].sibling_methods == {"GET"}


def test_collect_evidence_state_mutation_and_uniqueness_field():
    patch_entries = [entry(method="PATCH", path="/orders/1", status=200, resp={"status": "shipped"})
                     for _ in range(3)]
    ep_patch = infer_endpoint("PATCH", "/orders/{id}", patch_entries)
    post_entries = [entry(method="POST", path="/users", status=201, req={"email": "a@b.co"})
                    for _ in range(3)]
    ep_post = infer_endpoint("POST", "/users", post_entries)
    grouped = {("PATCH", "/orders/{id}"): patch_entries, ("POST", "/users"): post_entries}
    evidence = collect_evidence([ep_patch, ep_post], grouped)
    assert evidence[("PATCH", "/orders/{id}")].is_state_mutation is True
    assert evidence[("POST", "/users")].has_uniqueness_field is True


# ---------- mine_validation_evidence (B3) ----------

def test_mine_validation_evidence_finds_missing_required_field():
    entries = [entry(method="POST", path="/users", status=201, req={"email": "a@b.co", "name": "x"})
              for _ in range(5)]
    entries.append(entry(method="POST", path="/users", status=400, req={"name": "x"}))  # missing email
    ep = infer_endpoint("POST", "/users", entries)
    findings = mine_validation_evidence(ep, entries)
    assert {"field": "email", "issue": "required", "status": 400} in findings


def test_mine_validation_evidence_empty_without_request_schema():
    ep = infer_endpoint("GET", "/x", [entry()])
    assert mine_validation_evidence(ep, [entry()]) == []


# ---------- infer_error_responses (B2 rule matrix) ----------

def make_ep(**overrides) -> EndpointSchema:
    base = dict(method="GET", path_template="/x", responses={200: {"type": "object"}})
    base.update(overrides)
    return EndpointSchema(**base)


def _evidence(**kw):
    from errors import EndpointEvidence
    base = dict(has_path_param=False, has_request_body=False, auth_rate=0.0,
               is_state_mutation=False, sibling_methods=set(), has_uniqueness_field=False)
    base.update(kw)
    return EndpointEvidence(**base)


def _api(**kw):
    from errors import ApiEvidence
    base = dict(uses_422=False, uses_409=False, rate_limited=False)
    base.update(kw)
    return ApiEvidence(**base)


ENVELOPE = {"type": "object", "properties": {"error": {"type": "string"}, "message": {"type": "string"}}}


def test_path_param_infers_404():
    ep = make_ep(path_template="/x/{id}")
    out = infer_error_responses(ep, _evidence(has_path_param=True), _api(), ENVELOPE, True, [])
    assert out[404].evidence == "resource lookup by id"


def test_request_body_infers_400_or_422_depending_on_api():
    ep = make_ep()
    out_400 = infer_error_responses(ep, _evidence(has_request_body=True), _api(uses_422=False), ENVELOPE, True, [])
    assert 400 in out_400 and 422 not in out_400
    out_422 = infer_error_responses(ep, _evidence(has_request_body=True), _api(uses_422=True), ENVELOPE, True, [])
    assert 422 in out_422 and 400 not in out_422


def test_request_body_example_uses_mined_evidence():
    ep = make_ep()
    mined = [{"field": "email", "issue": "required", "status": 400}]
    out = infer_error_responses(ep, _evidence(has_request_body=True), _api(), ENVELOPE, True, mined)
    assert out[400].example is not None
    assert "email" in out[400].example["message"]


def test_high_auth_rate_infers_401_and_403():
    ep = make_ep()
    out = infer_error_responses(ep, _evidence(auth_rate=0.9), _api(), ENVELOPE, True, [])
    assert 401 in out and 403 in out


def test_low_auth_rate_does_not_infer_401():
    ep = make_ep()
    out = infer_error_responses(ep, _evidence(auth_rate=0.1), _api(), ENVELOPE, True, [])
    assert 401 not in out


def test_state_mutation_or_api_uses_409_infers_409():
    ep = make_ep()
    assert 409 in infer_error_responses(ep, _evidence(is_state_mutation=True), _api(), ENVELOPE, True, [])
    assert 409 in infer_error_responses(ep, _evidence(), _api(uses_409=True), ENVELOPE, True, [])


def test_uniqueness_field_infers_409_duplicate():
    ep = make_ep()
    out = infer_error_responses(ep, _evidence(has_uniqueness_field=True), _api(), ENVELOPE, True, [])
    assert out[409].evidence == "duplicate resource"


def test_rate_limited_api_infers_429():
    ep = make_ep()
    out = infer_error_responses(ep, _evidence(), _api(rate_limited=True), ENVELOPE, True, [])
    assert 429 in out


def test_sibling_methods_infers_405_low_confidence():
    ep = make_ep()
    out = infer_error_responses(ep, _evidence(sibling_methods={"POST"}), _api(), ENVELOPE, True, [])
    assert out[405].confidence == "low"


def test_500_always_inferred():
    ep = make_ep()
    out = infer_error_responses(ep, _evidence(), _api(), ENVELOPE, True, [])
    assert 500 in out and out[500].confidence == "low"


def test_never_overrides_an_observed_status():
    ep = make_ep(path_template="/x/{id}", responses={200: {}, 404: {"type": "object"}})
    out = infer_error_responses(ep, _evidence(has_path_param=True), _api(), ENVELOPE, True, [])
    assert 404 not in out  # already observed -> not re-inferred


def test_low_confidence_when_envelope_not_observed():
    ep = make_ep(path_template="/x/{id}")
    out = infer_error_responses(ep, _evidence(has_path_param=True), _api(), ENVELOPE, False, [])
    assert out[404].confidence == "low"


# ---------- spec_builder wiring ----------

def test_build_operation_merges_inferred_statuses():
    ep = make_ep(path_template="/x/{id}")
    inferred = infer_error_responses(ep, _evidence(has_path_param=True), _api(), ENVELOPE, True, [])
    op = build_operation(ep, inferred=inferred)
    assert op["responses"]["404"]["x-inferred"] is True
    assert op["responses"]["404"]["x-evidence"] == "resource lookup by id"
    assert op["responses"]["200"].get("x-inferred") is None  # observed status untouched


def test_build_operation_without_inferred_param_is_unchanged():
    ep = make_ep()
    op = build_operation(ep)
    assert all("x-inferred" not in r for r in op["responses"].values())


def test_build_spec_threads_inferred_by_endpoint_key():
    ep = make_ep(method="GET", path_template="/x/{id}")
    inferred = {("GET", "/x/{id}"): infer_error_responses(ep, _evidence(has_path_param=True), _api(), ENVELOPE, True, [])}
    spec = build_spec([ep], inferred=inferred)
    assert spec["paths"]["/x/{id}"]["get"]["responses"]["404"]["x-inferred"] is True
    assert validate_spec(spec) == []


def test_request_body_required_reflects_evidence():
    always = make_ep(request_schema={"type": "object"}, request_required=True)
    sometimes = make_ep(request_schema={"type": "object"}, request_required=False)
    assert build_operation(always)["requestBody"]["required"] is True
    assert build_operation(sometimes)["requestBody"]["required"] is False


def test_security_emitted_when_auth_rate_high():
    ep = make_ep(auth_rate=0.9)
    op = build_operation(ep)
    assert op["security"] == [{"bearerAuth": []}]
    spec = build_spec([ep])
    assert spec["components"]["securitySchemes"]["bearerAuth"]["type"] == "http"


def test_no_security_when_auth_rate_low():
    ep = make_ep(auth_rate=0.1)
    op = build_operation(ep)
    assert "security" not in op
    spec = build_spec([ep])
    assert "components" not in spec
