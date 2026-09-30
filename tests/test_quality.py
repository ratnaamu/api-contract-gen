"""Tests for quality.py (A6). Run from repo root: pytest -q"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quality import build_quality_report, endpoint_confidence  # noqa: E402


def op(**overrides) -> dict:
    base = {"x-sample-count": 50, "responses": {"200": {"content": {"application/json": {"schema": {}}}}}}
    base.update(overrides)
    return base


def spec_with(paths: dict) -> dict:
    return {"openapi": "3.0.3", "info": {}, "paths": paths}


# ---------- skip-reason / parse-rate summary ----------

def test_parse_rate_and_skip_tally():
    report = build_quality_report(spec_with({}), lines_read=180, skipped={"invalid_json": 15, "no_status": 5})
    assert report["lines_read"] == 180
    assert report["lines_skipped"] == 20
    assert report["skipped_by_reason"] == {"invalid_json": 15, "no_status": 5}
    assert report["parse_rate"] == 0.9


def test_parse_rate_with_nothing_read_is_1():
    report = build_quality_report(spec_with({}), lines_read=0, skipped={})
    assert report["parse_rate"] == 1.0


# ---------- ambiguity location prefixing ----------

def test_ambiguity_location_distinguishes_request_and_response():
    schema_with_ambiguity = {"type": "object", "properties": {
        "coupon_code": {"type": "string", "x-ambiguity": "rare_field"},
    }}
    p = spec_with({"/orders": {"post": op(
        requestBody={"content": {"application/json": {"schema": schema_with_ambiguity}}},
        responses={"201": {"content": {"application/json": {"schema": schema_with_ambiguity}}}},
    )}})
    report = build_quality_report(p, 10, {})
    locations = {a["location"] for a in report["ambiguities"]}
    assert locations == {"request.body.coupon_code", "response.201.body.coupon_code"}


def test_ambiguity_in_nested_object_and_array():
    schema = {"type": "object", "properties": {
        "address": {"type": "object", "properties": {
            "line2": {"type": "string", "x-ambiguity": "rare_field"},
        }},
        "tags": {"type": "array", "items": {"type": "string", "x-ambiguity": "rare_field"}},
    }}
    p = spec_with({"/x": {"get": op(responses={"200": {"content": {"application/json": {"schema": schema}}}})}})
    report = build_quality_report(p, 10, {})
    locations = {a["location"] for a in report["ambiguities"]}
    assert locations == {"response.200.body.address.line2", "response.200.body.tags[]"}


def test_x_inferred_responses_excluded_from_ambiguity_scan():
    schema = {"type": "object", "properties": {"x": {"type": "string", "x-ambiguity": "rare_field"}}}
    p = spec_with({"/x": {"get": op(responses={
        "200": {"content": {"application/json": {"schema": {}}}},
        "404": {"x-inferred": True, "content": {"application/json": {"schema": schema}}},
    })}})
    report = build_quality_report(p, 10, {})
    assert report["ambiguities"] == []


# ---------- endpoint_confidence ----------

def test_rare_field_alone_does_not_force_low_confidence():
    found = [{"location": "x", "ambiguity": "rare_field"}]
    assert endpoint_confidence(op(**{"x-sample-count": 50}), found) == "high"


def test_serious_ambiguity_forces_low_confidence_regardless_of_sample_count():
    found = [{"location": "x", "ambiguity": "type_conflict"}]
    assert endpoint_confidence(op(**{"x-sample-count": 500}), found) == "low"


def test_version_mix_dict_shaped_ambiguity_forces_low_confidence():
    found = [{"location": "", "ambiguity": {"kind": "possible_version_mix", "clusters": 2, "renames": []}}]
    assert endpoint_confidence(op(**{"x-sample-count": 500}), found) == "low"


def test_low_confidence_field_forces_low_regardless_of_ambiguity():
    schema = {"type": "object", "properties": {"id": {"type": "integer", "x-confidence": "low"}}}
    o = op(responses={"200": {"content": {"application/json": {"schema": schema}}}}, **{"x-sample-count": 500})
    assert endpoint_confidence(o, []) == "low"


def test_confidence_scales_with_sample_count_absent_ambiguity():
    assert endpoint_confidence(op(**{"x-sample-count": 5}), []) == "low"
    assert endpoint_confidence(op(**{"x-sample-count": 15}), []) == "medium"
    assert endpoint_confidence(op(**{"x-sample-count": 50}), []) == "high"


# ---------- end-to-end shape ----------

def test_endpoints_sorted_and_all_http_methods_covered():
    p = spec_with({
        "/b": {"get": op()},
        "/a": {"post": op(), "get": op()},
    })
    report = build_quality_report(p, 10, {})
    assert [(e["path"], e["method"]) for e in report["endpoints"]] == [
        ("/a", "GET"), ("/a", "POST"), ("/b", "GET"),
    ]
    assert report["endpoint_count"] == 3
