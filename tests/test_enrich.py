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
    assert inferrer.ENUM_MAX_VALUES == 8 and inferrer.ENUM_MIN_SAMPLES == 5
    assert inferrer.ENUM_MIN_REPEAT_RATIO == 2.0


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


def test_no_enum_below_min_samples():
    samples = [{"type": ["standard", "express"][i % 2]} for i in range(4)]
    assert "enum" not in field(samples, "type")


def test_enum_from_few_samples_when_values_repeat():
    # the mock-fidelity bug: a 6-sample endpoint left `status` as a bare string and Prism generated
    # lorem ipsum for it. 6 samples / 2 distinct values is clearly a closed set.
    samples = [{"status": ["submitted", "in_review"][i % 2]} for i in range(6)]
    assert field(samples, "status")["enum"] == ["in_review", "submitted"]


def test_no_enum_with_more_than_8_values():
    # "shipping_method", not "city": A4's guard rails deliberately exclude free-text-shaped names
    # (city/zip/name/etc.) from enum eligibility regardless of cardinality — see test_enum_guard_rails.
    # n=90/80 (not 40): A4 also requires distinct/total <= 0.1, so 8-9 distinct values need that many
    # samples to be eligible on cardinality grounds alone (independent of the >8-distinct-values cap
    # this test is about).
    samples = [{"shipping_method": f"method{i % 9}"} for i in range(90)]
    assert "enum" not in field(samples, "shipping_method")
    samples = [{"shipping_method": f"method{i % 8}"} for i in range(80)]
    assert len(field(samples, "shipping_method")["enum"]) == 8


def test_format_wins_over_enum():
    samples = [{"day": ["2026-01-01", "2026-01-02"][i % 2]} for i in range(30)]
    f = field(samples, "day")
    assert f["format"] == "date" and "enum" not in f


def test_nullable_enum_lists_null():
    samples = [{"s": ["a", "b", None][i % 3]} for i in range(30)]
    s = to_openapi_schema(field(samples, "s"))
    assert s["nullable"] is True and s["enum"] == ["a", "b", None]


# ---------- integer ranges ----------
# A4: a contract minimum/maximum is a claim consumers code against (see the review's `id` example —
# Prism would never generate one outside the observed range, and a strict consumer breaks on record
# #30). The observed range still lives on the schema, just as x-observed-range instead of a hard
# contract bound; the only bound actually asserted is `minimum: 0` when nothing negative was ever seen.

def test_integer_range_is_observed_only_not_a_contract_bound():
    samples = [{"pages": p} for p in (32, 48, 32)]
    f = field(samples, "pages")
    assert f["x-observed-range"] == [32, 48]
    assert f["minimum"] == 0        # every observed value was non-negative
    assert "maximum" not in f       # but the upper bound is not a contract claim


def test_no_range_for_a_single_observed_value():
    assert "x-observed-range" not in field([{"n": 5}, {"n": 5}], "n")


def test_no_range_for_numbers_or_booleans():
    assert "minimum" not in field([{"price": 1.5}, {"price": 2}], "price")
    assert "minimum" not in field([{"ok": True}, {"ok": False}], "ok")


# ---------- enum guard rails (A4) ----------

def test_enum_never_fires_on_a_free_text_field_name():
    # "city" would otherwise be a textbook false-enum: few distinct values, seen often, small set —
    # this is the exact bug the review found (address.city/address.zip in sample_logs.jsonl).
    samples = [{"city": ["Reno", "Lisbon", "Toronto"][i % 3]} for i in range(90)]
    assert "enum" not in field(samples, "city")


@pytest.mark.parametrize("name", ["name", "title", "street", "zip", "postal", "email",
                                  "message", "description", "url", "id"])
def test_enum_never_fires_on_any_excluded_name(name):
    samples = [{name: ["a", "b"][i % 2]} for i in range(60)]
    assert "enum" not in field(samples, name)


def test_enum_requires_values_to_repeat_not_just_a_cap():
    # 8 distinct values in 8 samples: under the cap, but nothing repeated -> looks like free text.
    samples = [{"code": f"c{i}"} for i in range(8)]
    assert "enum" not in field(samples, "code")
    # 8 distinct values in 16 samples (each seen twice) -> a closed set.
    samples = [{"code": f"c{i % 8}"} for i in range(16)]
    assert len(field(samples, "code")["enum"]) == 8


def test_reference_id_fields_can_enum_on_evidence():
    # office_id -> OFF-LON/OFF-MAN/OFF-EDI is a closed set; `id` itself never is.
    samples = [{"office_id": ["OFF-LON", "OFF-MAN", "OFF-EDI"][i % 3], "id": f"PA-{i:06d}"} for i in range(30)]
    assert field(samples, "office_id")["enum"] == ["OFF-EDI", "OFF-LON", "OFF-MAN"]
    assert "enum" not in field(samples, "id")


def test_enum_rejects_long_or_spaced_values():
    samples = [{"note": ["order shipped today", "order delayed"][i % 2]} for i in range(30)]
    assert "enum" not in field(samples, "note")


def test_enum_confidence_reported_when_it_fires():
    samples = [{"tier": ["gold", "silver"][i % 2]} for i in range(40)]
    f = field(samples, "tier")
    assert f["enum"] == ["gold", "silver"]
    assert 0 < f["x-enum-confidence"] <= 1.0


# ---------- presence / required / confidence (A3) ----------

def test_always_present_small_sample_is_required_but_low_confidence():
    samples = [{"id": i} for i in range(3)]
    s = enriched(samples)
    assert "id" in s["required"]
    assert s["properties"]["id"]["x-confidence"] == "low"


def test_always_present_large_sample_is_required_without_low_confidence_flag():
    samples = [{"id": i} for i in range(50)]
    s = enriched(samples)
    assert "id" in s["required"]
    assert "x-confidence" not in s["properties"]["id"]


def test_near_universal_presence_still_counts_as_required():
    # 99/100: genson's old binary rule would call this optional over one dropped sample; A3 doesn't.
    samples = [{"id": 1, "extra": "x"} for _ in range(99)] + [{"id": 1}]
    s = enriched(samples)
    assert "extra" in s["required"]
    assert "x-confidence" not in s["properties"]["extra"]


def test_mid_presence_field_is_optional_not_ambiguous():
    samples = [{"id": 1, "phone": "555"} for _ in range(15)] + [{"id": 1} for _ in range(5)]
    s = enriched(samples)
    assert "phone" not in s["required"]
    assert "x-ambiguity" not in s["properties"]["phone"]


def test_rare_field_flagged_ambiguous_and_optional():
    samples = [{"id": 1, "debug": "x"} for _ in range(5)] + [{"id": 1} for _ in range(20)]
    s = enriched(samples)
    assert "debug" not in s["required"]
    assert s["properties"]["debug"]["x-ambiguity"] == "rare_field"


def test_presence_reflects_sample_size_not_just_ratio():
    # Both are "always present", but 3/3 is weaker evidence than 300/300 — x-presence (a Wilson lower
    # bound) must say so even though the raw ratio is 1.0 either way.
    small = enriched([{"id": i} for i in range(3)])["properties"]["id"]["x-presence"]
    large = enriched([{"id": i} for i in range(300)])["properties"]["id"]["x-presence"]
    assert small < large


# ---------- type conflicts (A3) ----------

def test_type_conflict_collapses_to_majority_type():
    samples = [{"v": i} for i in range(96)] + [{"v": "oops"} for _ in range(4)]
    f = field(samples, "v")
    assert f["type"] == "integer"
    assert f["x-ambiguity"] == "type_conflict"
    assert f["x-observed-types"] == {"integer": 96, "string": 4}


def test_type_conflict_below_majority_threshold_stays_a_real_union():
    samples = [{"v": i} for i in range(90)] + [{"v": "x"} for _ in range(10)]
    f = field(samples, "v")
    assert isinstance(f["type"], list) and set(f["type"]) == {"integer", "string"}
    assert "x-ambiguity" not in f


# ---------- key-casing canonicalisation (A2) ----------

def test_casing_variants_merge_into_one_property():
    samples = [{"ID": i} if i == 0 else {"id": i} for i in range(20)]
    s = enriched(samples)
    assert set(s["properties"]) == {"id"}
    assert s["properties"]["id"]["x-aliases"] == ["id", "ID"]  # majority spelling first


def test_significant_casing_mix_is_flagged_ambiguous():
    samples = [{"ID": i} for i in range(6)] + [{"id": i} for i in range(14)]  # 30% minority
    s = enriched(samples)
    assert s["properties"]["id"]["x-ambiguity"] == "inconsistent_casing"


def test_rare_typo_spelling_not_flagged_ambiguous():
    samples = [{"ID": 1}] + [{"id": i} for i in range(99)]  # 1% minority
    s = enriched(samples)
    assert "x-aliases" in s["properties"]["id"]
    assert "x-ambiguity" not in s["properties"]["id"]


def test_no_casing_variants_no_aliases_key():
    s = enriched([{"id": i} for i in range(5)])
    assert "x-aliases" not in s["properties"]["id"]


# ---------- nesting, mixed types, OpenAPI conversion ----------

def test_nested_objects_and_arrays():
    samples = [{"items": [{"qty": q, "state": "packed"} for q in (1, 5)], "owner": {"mail": "a@b.co"}}
               for _ in range(12)]
    s = enriched(samples)
    item = s["properties"]["items"]["items"]["properties"]
    assert item["qty"]["x-observed-range"] == [1, 5]
    assert item["state"]["enum"] == ["packed"]                      # 24 values, 1 distinct
    assert s["properties"]["owner"]["properties"]["mail"]["format"] == "email"


def test_mixed_type_field_keeps_hints_on_the_right_branch():
    # 50/50 split -> a real union (neither type reaches the 95% majority that would collapse it).
    samples = [{"v": v} for v in [1, 7] * 10 + ["x", "y"] * 10]
    s = to_openapi_schema(field(samples, "v"))
    branches = {b["type"]: b for b in s["anyOf"]}
    assert branches["string"]["enum"] == ["x", "y"]
    assert branches["integer"]["x-observed-range"] == [1, 7]


def test_infer_endpoint_enriches_request_and_responses_and_spec_validates():
    entries = [{"timestamp": "2026-09-29T10:00:00Z", "method": "POST", "path": "/a", "query": {},
                "request_body": {"kind": ["x", "y"][i % 2], "n": i}, "status": 201,
                "response_body": {"id": f"598336e3-75d6-4ed4-ab1f-a9f2d10bd{i:03d}", "at": "2026-09-29T10:00:00Z"},
                "headers": {}} for i in range(20)]
    ep = infer_endpoint("POST", "/a", entries)
    assert ep.request_schema["properties"]["kind"]["enum"] == ["x", "y"]
    assert ep.request_schema["properties"]["n"]["x-observed-range"] == [0, 19]
    assert ep.responses[201]["properties"]["id"]["format"] == "uuid"
    assert ep.responses[201]["properties"]["at"]["format"] == "date-time"
    assert validate_spec(build_spec([ep])) == []


def test_infer_schema_itself_stays_plain_genson():
    assert infer_schema([{"email": "a@b.co"}]) == {"type": "object", "properties": {"email": {"type": "string"}},
                                                   "required": ["email"]}
