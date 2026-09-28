"""Tests for spec_builder.py. Run from repo root: pytest -q"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inferrer import infer_all, infer_schema  # noqa: E402
from models import EndpointSchema, ParamInfo  # noqa: E402
from normalizer import group_by_endpoint  # noqa: E402
from parser import read_logs  # noqa: E402
from spec_builder import (  # noqa: E402
    OPENAPI_VERSION, build_operation, build_spec, load_spec, to_openapi_schema, validate_spec, write_spec,
)

SAMPLE = ROOT / "sample_logs.jsonl"


@pytest.fixture(scope="module")
def sample_endpoints() -> list[EndpointSchema]:
    return infer_all(group_by_endpoint(read_logs(SAMPLE)))


@pytest.fixture(scope="module")
def sample_spec(sample_endpoints) -> dict:
    return build_spec(sample_endpoints)


def _walk(node):
    """Yield every dict inside a nested structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


# ---------- to_openapi_schema ----------

def test_drops_dollar_schema():
    assert "$schema" not in to_openapi_schema({"$schema": "http://json-schema.org/schema#", "type": "string"})


@pytest.mark.parametrize("types", [["string", "null"], ["null", "string"]])
def test_nullable_type_array(types):
    assert to_openapi_schema({"type": types}) == {"type": "string", "nullable": True}


def test_null_only():
    assert to_openapi_schema({"type": "null"}) == {"nullable": True}


def test_multi_type_to_anyof():
    out = to_openapi_schema({"type": ["integer", "string"]})
    assert out == {"anyOf": [{"type": "integer"}, {"type": "string"}]}


def test_multi_type_with_null_to_nullable_anyof():
    out = to_openapi_schema({"type": ["integer", "null", "string"]})
    assert out == {"anyOf": [{"type": "integer", "nullable": True}, {"type": "string", "nullable": True}]}


def test_multi_type_splits_keywords_per_branch():
    out = to_openapi_schema({"type": ["array", "object"], "items": {"type": "integer"},
                             "properties": {"a": {"type": "string"}}, "required": ["a"]})
    arr, obj = out["anyOf"]
    assert arr == {"type": "array", "items": {"type": "integer"}}
    assert obj == {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


def test_recurses_into_properties_and_items():
    s = infer_schema([{"email": "a@b.c", "tags": ["x", None], "addr": {"zip": None}},
                      {"email": None, "tags": [], "addr": {"zip": "123"}}])
    out = to_openapi_schema(s)
    assert out["properties"]["email"] == {"type": "string", "nullable": True}
    assert out["properties"]["tags"]["items"] == {"type": "string", "nullable": True}
    assert out["properties"]["addr"]["properties"]["zip"] == {"type": "string", "nullable": True}


def test_anyof_with_null_branch_collapses():
    s = infer_schema([{"a": None}, {"a": {"x": 1}}])
    assert s["properties"]["a"]["anyOf"][0] == {"type": "null"}  # genson shape we're converting
    a = to_openapi_schema(s)["properties"]["a"]
    assert a["nullable"] is True and a["type"] == "object" and "anyOf" not in a


def test_empty_array_gets_items():
    assert to_openapi_schema({"type": "array"}) == {"type": "array", "items": {}}


def test_empty_required_dropped():
    assert "required" not in to_openapi_schema({"type": "object", "properties": {}, "required": []})


def test_no_type_arrays_anywhere_in_sample_spec(sample_spec):
    for node in _walk(sample_spec["paths"]):
        assert not isinstance(node.get("type"), list), node
        assert node.get("type") != "null", node


# ---------- build_operation ----------

def _ep() -> EndpointSchema:
    return EndpointSchema(
        method="PUT", path_template="/users/{id}",
        params=[ParamInfo("id", "path", {"type": "integer"}, True),
                ParamInfo("dry_run", "query", {"type": "boolean"}, False)],
        request_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        responses={200: {"type": "object", "properties": {"id": {"type": "integer"}}}, 204: None},
        examples={200: {"id": 7}},
        sample_count=3,
    )


def test_build_operation_shape():
    op = build_operation(_ep())
    assert op["operationId"] == "put_users_by_id"
    assert {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}} in op["parameters"]
    assert {"name": "dry_run", "in": "query", "required": False, "schema": {"type": "boolean"}} in op["parameters"]
    assert op["requestBody"]["content"]["application/json"]["schema"]["required"] == ["name"]
    assert set(op["responses"]) == {"200", "204"}                     # string keys
    assert op["responses"]["200"]["content"]["application/json"]["example"] == {"id": 7}
    assert op["responses"]["204"] == {"description": "No Content"}     # empty body -> description only


def test_missing_path_param_is_added():
    ep = EndpointSchema(method="GET", path_template="/things/{thingId}", responses={200: None})
    op = build_operation(ep)
    assert op["parameters"] == [{"name": "thingId", "in": "path", "required": True, "schema": {"type": "string"}}]


def test_no_request_body_when_none():
    ep = EndpointSchema(method="GET", path_template="/x", responses={200: {"type": "string"}})
    assert "requestBody" not in build_operation(ep)


# ---------- build_spec + validation ----------

def test_empty_spec_is_valid():
    spec = build_spec([])
    assert spec["openapi"] == OPENAPI_VERSION
    assert spec["paths"] == {}
    assert validate_spec(spec) == []


def test_sample_spec_passes_validation(sample_spec):
    assert validate_spec(sample_spec) == []


def test_sample_spec_covers_all_endpoints(sample_endpoints, sample_spec):
    assert len(sample_endpoints) > 0
    for ep in sample_endpoints:
        op = sample_spec["paths"][ep.path_template][ep.method.lower()]
        assert set(op["responses"]) == {str(s) for s in ep.responses}


def test_sample_spec_has_one_example_per_json_response(sample_endpoints, sample_spec):
    for ep in sample_endpoints:
        op = sample_spec["paths"][ep.path_template][ep.method.lower()]
        for status, schema in ep.responses.items():
            resp = op["responses"][str(status)]
            if schema is None:
                assert "content" not in resp
            else:
                assert resp["content"]["application/json"]["example"] == ep.examples[status]


def test_sample_spec_params_and_bodies(sample_spec):
    paths = sample_spec["paths"]
    user_params = paths["/users/{id}"]["get"]["parameters"]
    assert {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}} in user_params
    assert any(p["in"] == "query" for p in paths["/users"]["get"]["parameters"])
    assert "requestBody" in paths["/users"]["post"]
    assert "requestBody" in paths["/orders"]["post"]


def test_operation_ids_unique(sample_spec):
    ids = [op["operationId"] for item in sample_spec["paths"].values() for op in item.values()]
    assert len(ids) == len(set(ids))


def test_validate_spec_reports_errors_and_never_raises():
    assert validate_spec({"openapi": "3.0.3"})  # missing info/paths
    bad = build_spec([])
    bad["paths"]["/x"] = {"get": {"responses": {"200": {}}}}  # response without description
    assert validate_spec(bad)
    assert validate_spec("not a dict")  # type: ignore[arg-type]


# ---------- write / load ----------

def test_write_and_load_roundtrip(tmp_path, sample_spec):
    out = tmp_path / "output" / "openapi.yaml"
    write_spec(sample_spec, out)
    text = out.read_text(encoding="utf-8")
    assert "&id" not in text and "*id" not in text  # no YAML aliases
    loaded = load_spec(out)
    assert loaded == json.loads(json.dumps(sample_spec))
    assert validate_spec(loaded) == []
    assert [p.name for p in out.parent.iterdir()] == ["openapi.yaml"]  # no temp files left behind


def test_write_overwrites_existing(tmp_path):
    out = tmp_path / "openapi.yaml"
    write_spec(build_spec([]), out)
    spec2 = build_spec([EndpointSchema(method="GET", path_template="/ping", responses={200: None})])
    write_spec(spec2, out)
    assert "/ping" in load_spec(out)["paths"]


def test_load_missing_returns_none(tmp_path):
    assert load_spec(tmp_path / "nope.yaml") is None


def test_load_json(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(build_spec([])), encoding="utf-8")
    assert load_spec(p)["openapi"] == OPENAPI_VERSION


def test_written_yaml_is_plain_safe_yaml(tmp_path, sample_spec):
    out = tmp_path / "openapi.yaml"
    write_spec(sample_spec, out)
    assert yaml.safe_load(out.read_text(encoding="utf-8"))["openapi"] == OPENAPI_VERSION
