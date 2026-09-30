"""Tests for score.py (A7/B6). Run from repo root: pytest -q"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import score as score_module  # noqa: E402
from score import run, score, score_error_coverage  # noqa: E402


def op(request_schema=None, responses=None) -> dict:
    o: dict = {"responses": responses or {}}
    if request_schema:
        o["requestBody"] = {"content": {"application/json": {"schema": request_schema}}}
    return o


def spec(paths: dict) -> dict:
    return {"paths": paths}


def resp200(schema: dict) -> dict:
    return {"200": {"content": {"application/json": {"schema": schema}}}}


BASE_SCHEMA = {"type": "object", "required": ["id"], "properties": {
    "id": {"type": "integer"}, "name": {"type": "string", "nullable": True},
}}


# ---------- score() ----------

def test_score_perfect_match_gives_1_0_everything():
    clean = spec({"/x": {"get": op(responses=resp200(BASE_SCHEMA))}})
    result = score(clean, clean)
    assert result["field_precision"] == 1.0
    assert result["field_recall"] == 1.0
    assert result["required_accuracy"] == 1.0
    assert result["nullable_accuracy"] == 1.0
    assert result["false_enum_count"] == 0


def test_score_missing_field_lowers_recall_not_precision():
    clean = spec({"/x": {"get": op(responses=resp200(BASE_SCHEMA))}})
    noisy_schema = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
    noisy = spec({"/x": {"get": op(responses=resp200(noisy_schema))}})
    result = score(clean, noisy)
    assert result["field_recall"] < 1.0
    assert result["field_precision"] == 1.0  # nothing extra was hallucinated


def test_score_extra_field_lowers_precision_not_recall():
    clean = spec({"/x": {"get": op(responses=resp200(BASE_SCHEMA))}})
    noisy_schema = dict(BASE_SCHEMA, properties={**BASE_SCHEMA["properties"], "extra": {"type": "string"}})
    noisy = spec({"/x": {"get": op(responses=resp200(noisy_schema))}})
    result = score(clean, noisy)
    assert result["field_precision"] < 1.0
    assert result["field_recall"] == 1.0


def test_score_required_mismatch_lowers_required_accuracy():
    clean = spec({"/x": {"get": op(responses=resp200(BASE_SCHEMA))}})
    noisy_schema = {"type": "object", "required": [], "properties": BASE_SCHEMA["properties"]}
    noisy = spec({"/x": {"get": op(responses=resp200(noisy_schema))}})
    result = score(clean, noisy)
    assert result["required_accuracy"] < 1.0


def test_score_nullable_mismatch_lowers_nullable_accuracy():
    clean = spec({"/x": {"get": op(responses=resp200(BASE_SCHEMA))}})
    props = dict(BASE_SCHEMA["properties"])
    props["name"] = {"type": "string"}  # nullable dropped
    noisy = spec({"/x": {"get": op(responses=resp200({**BASE_SCHEMA, "properties": props}))}})
    result = score(clean, noisy)
    assert result["nullable_accuracy"] < 1.0


def test_score_false_enum_counted():
    clean = spec({"/x": {"get": op(responses=resp200(BASE_SCHEMA))}})
    props = dict(BASE_SCHEMA["properties"])
    props["name"] = {**props["name"], "enum": ["a", "b"]}
    noisy = spec({"/x": {"get": op(responses=resp200({**BASE_SCHEMA, "properties": props}))}})
    result = score(clean, noisy)
    assert result["false_enum_count"] == 1


def test_score_ignores_x_inferred_responses():
    clean = spec({"/x": {"get": op(responses=resp200(BASE_SCHEMA))}})
    noisy_responses = resp200(BASE_SCHEMA)
    noisy_responses["404"] = {"x-inferred": True, "content": {"application/json": {
        "schema": {"type": "object", "properties": {"bogus": {"type": "string"}}}}}}
    noisy = spec({"/x": {"get": op(responses=noisy_responses)}})
    result = score(clean, noisy)
    assert result["field_precision"] == 1.0  # the inferred-only field never counts against precision


def test_score_endpoint_only_in_one_spec_is_not_double_counted():
    clean = spec({"/x": {"get": op(responses=resp200(BASE_SCHEMA))}, "/y": {"get": op(responses=resp200(BASE_SCHEMA))}})
    noisy = spec({"/x": {"get": op(responses=resp200(BASE_SCHEMA))}})
    result = score(clean, noisy)
    assert result["endpoints_shared"] == 1
    assert result["field_precision"] == 1.0 and result["field_recall"] == 1.0


# ---------- score_error_coverage() ----------

def test_error_coverage_full_and_partial():
    holdout = spec({"/x": {"get": op(responses={"200": {}, "404": {}, "500": {}})}})
    observed_only = spec({"/x": {"get": op(responses={"200": {}, "404": {}})}})
    with_inference = spec({"/x": {"get": op(responses={
        "200": {}, "404": {}, "500": {"x-inferred": True}})}})
    a = score_error_coverage(observed_only, holdout)
    b = score_error_coverage(with_inference, holdout)
    assert a["coverage"] < b["coverage"] == 1.0
    assert a["ground_truth_statuses"] == b["ground_truth_statuses"] == 3


# ---------- run() ----------

def test_run_end_to_end_with_real_logs(tmp_path):
    entries = [{"timestamp": "t", "method": "GET", "path": "/x", "query": {}, "request_body": None,
                "status": 200, "response_body": {"id": i}, "headers": {}} for i in range(20)]
    clean = tmp_path / "clean.jsonl"
    clean.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    noisy = tmp_path / "noisy.jsonl"
    noisy.write_text("\n".join(json.dumps(e) for e in entries[:15]) + "\n", encoding="utf-8")  # fewer samples
    result = run(clean, noisy)
    assert result["crash_count"] == 0
    assert result["endpoints_clean"] == result["endpoints_noisy"] == 1
    assert "field_precision" in result and "field_recall" in result


def test_run_reports_a_crash_without_raising(tmp_path, monkeypatch):
    def boom(path, infer_errors=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(score_module, "build_from_log", boom)
    result = run(tmp_path / "a.jsonl", tmp_path / "b.jsonl")
    assert result["crash_count"] == 1
    assert "boom" in result["clean_build_error"]


def test_run_with_holdout_reports_error_coverage(tmp_path):
    entries = [{"timestamp": "t", "method": "GET", "path": "/x/1", "query": {}, "request_body": None,
                "status": 200, "response_body": {"id": 1}, "headers": {}} for _ in range(15)]
    entries += [{"timestamp": "t", "method": "GET", "path": "/x/2", "query": {}, "request_body": None,
                "status": 404, "response_body": {"error": "nope"}, "headers": {}} for _ in range(5)]
    holdout = tmp_path / "holdout.jsonl"
    holdout.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    training = tmp_path / "training.jsonl"
    training.write_text("\n".join(json.dumps(e) for e in entries[:15]) + "\n", encoding="utf-8")  # no 404s
    result = run(training, training, holdout)
    assert "error_coverage_observed_only" in result and "error_coverage_with_inference" in result
    assert result["error_coverage_with_inference"]["coverage"] >= result["error_coverage_observed_only"]["coverage"]
