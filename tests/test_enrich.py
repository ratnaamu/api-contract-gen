"""Schema enrichment for realistic mock data: format, enum and integer ranges from the observed values."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import inferrer  # noqa: E402
from inferrer import detect_format, enrich_schema, infer_endpoint, infer_schema  # noqa: E402
from spec_builder import build_spec, to_openapi_schema, validate_spec  # noqa: E402


def enriched(samples: list) -> dict:
    return enrich_schema(infer_schema(samples), samples)


def field(samples: list, name: str) -> dict:
    return enriched(samples)["properties"][name]


def test_thresholds():
    assert inferrer.ENUM_MAX_VALUES == 8 and inferrer.ENUM_MIN_SAMPLES == 20


# ---------- format ----------

@pytest.mark.parametrize("values, fmt", [
    (["ana@example.com", "b.c@d.co.uk"], "email"),
    (["1990-05-01", "2001-12-31"], "date"),
    (["2026-09-29T10:00:00Z", "2026-09-29T10:00:00.123+02:00"], "date-time"),
    (["598336e3-75d6-4ed4-ab1f-a9f2d10bd1d0"], "uuid"),
    (["https://example.com/a?b=1", "http://x.io"], "uri"),
])
def test_formats_detected(values, fmt):
    assert detect_format(values) == fmt


@pytest.mark.parametrize("values", [
    ["ana@example.com", "not an email"],       # one value doesn't match -> no format
    ["1990-02-30"],                             # not a real date
    ["2026-09-29"] + ["2026-09-29T10:00:00Z"],  # mixed date / date-time
    ["ftp://x"], ["hello"], [],
])
def test_no_format_unless_every_value_matches(values):
    assert detect_format(values) is None


def test_format_added_to_schema():
    samples = [{"email": f"u{i}@example.com", "dob": "1990-01-01"} for i in range(3)]
    assert field(samples, "email")["format"] == "email"
    assert field(samples, "dob")["format"] == "date"


# ---------- enum ----------

def test_enum_for_small_fixed_set_seen_often():
    samples = [{"type": ["standard", "express"][i % 2]} for i in range(20)]
    assert field(samples, "type")["enum"] == ["express", "standard"]


def test_no_enum_below_20_samples():
    samples = [{"type": ["standard", "express"][i % 2]} for i in range(19)]
    assert "enum" not in field(samples, "type")


def test_no_enum_with_more_than_8_values():
    samples = [{"city": f"city{i % 9}"} for i in range(40)]
    assert "enum" not in field(samples, "city")
    samples = [{"city": f"city{i % 8}"} for i in range(40)]
    assert len(field(samples, "city")["enum"]) == 8


def test_format_wins_over_enum():
    samples = [{"day": ["2026-01-01", "2026-01-02"][i % 2]} for i in range(30)]
    f = field(samples, "day")
    assert f["format"] == "date" and "enum" not in f


def test_nullable_enum_lists_null():
    samples = [{"s": ["a", "b", None][i % 3]} for i in range(30)]
    s = to_openapi_schema(field(samples, "s"))
    assert s["nullable"] is True and s["enum"] == ["a", "b", None]


# ---------- integer ranges ----------

def test_integer_min_max():
    samples = [{"pages": p} for p in (32, 48, 32)]
    assert (field(samples, "pages")["minimum"], field(samples, "pages")["maximum"]) == (32, 48)


def test_no_range_for_numbers_or_booleans():
    assert "minimum" not in field([{"price": 1.5}, {"price": 2}], "price")
    assert "minimum" not in field([{"ok": True}, {"ok": False}], "ok")


# ---------- nesting, mixed types, OpenAPI conversion ----------

def test_nested_objects_and_arrays():
    samples = [{"items": [{"qty": q, "state": "packed"} for q in (1, 5)], "owner": {"mail": "a@b.co"}}
               for _ in range(12)]
    s = enriched(samples)
    item = s["properties"]["items"]["items"]["properties"]
    assert (item["qty"]["minimum"], item["qty"]["maximum"]) == (1, 5)
    assert item["state"]["enum"] == ["packed"]                      # 24 values, 1 distinct
    assert s["properties"]["owner"]["properties"]["mail"]["format"] == "email"


def test_mixed_type_field_keeps_hints_on_the_right_branch():
    samples = [{"v": v} for v in [1, 7] * 10 + ["x", "y"] * 10]
    s = to_openapi_schema(field(samples, "v"))
    branches = {b["type"]: b for b in s["anyOf"]}
    assert branches["string"]["enum"] == ["x", "y"]
    assert (branches["integer"]["minimum"], branches["integer"]["maximum"]) == (1, 7)


def test_infer_endpoint_enriches_request_and_responses_and_spec_validates():
    entries = [{"timestamp": "2026-09-29T10:00:00Z", "method": "POST", "path": "/a", "query": {},
                "request_body": {"kind": ["x", "y"][i % 2], "n": i}, "status": 201,
                "response_body": {"id": f"598336e3-75d6-4ed4-ab1f-a9f2d10bd{i:03d}", "at": "2026-09-29T10:00:00Z"},
                "headers": {}} for i in range(20)]
    ep = infer_endpoint("POST", "/a", entries)
    assert ep.request_schema["properties"]["kind"]["enum"] == ["x", "y"]
    assert (ep.request_schema["properties"]["n"]["minimum"], ep.request_schema["properties"]["n"]["maximum"]) == (0, 19)
    assert ep.responses[201]["properties"]["id"]["format"] == "uuid"
    assert ep.responses[201]["properties"]["at"]["format"] == "date-time"
    assert validate_spec(build_spec([ep])) == []


def test_infer_schema_itself_stays_plain_genson():
    assert infer_schema([{"email": "a@b.co"}]) == {"type": "object", "properties": {"email": {"type": "string"}},
                                                   "required": ["email"]}
