"""quality.py — A6: a quality report next to the spec, so graceful degradation has somewhere to say
"I'm less sure about this" instead of nothing.

Contract:
    (built spec dict, lines_read, skipped-by-reason)  ->  dict  (written as output/quality.json)
Walks the FINAL spec (not EndpointSchema) for its x-ambiguity/x-confidence/x-presence annotations —
those are already exactly what shipped, so the report can point at precisely what a reader would see
in output/openapi.yaml, with no separate bookkeeping of its own to drift out of sync.
"""
from __future__ import annotations

from typing import Any

_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")

LOW_CONFIDENCE_MIN_SAMPLES = 10   # matches inferrer.LOW_CONFIDENCE_MAX_N
MEDIUM_CONFIDENCE_MIN_SAMPLES = 30


def _walk_ambiguities(schema: Any, location: str, out: list[dict[str, Any]]) -> None:
    """Collect every x-ambiguity found under `schema`, with a dotted `location` pointing at it."""
    if not isinstance(schema, dict):
        return
    ambiguity = schema.get("x-ambiguity")
    if ambiguity is not None:
        out.append({"location": location, "ambiguity": ambiguity})
    props = schema.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            _walk_ambiguities(sub, f"{location}.{name}" if location else name, out)
    items = schema.get("items")
    if isinstance(items, dict):
        _walk_ambiguities(items, f"{location}[]" if location else "[]", out)
    for branch in schema.get("anyOf") or []:
        _walk_ambiguities(branch, location, out)


def _has_low_confidence_field(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    if schema.get("x-confidence") == "low":
        return True
    props = schema.get("properties")
    if isinstance(props, dict) and any(_has_low_confidence_field(v) for v in props.values()):
        return True
    if _has_low_confidence_field(schema.get("items")):
        return True
    return any(_has_low_confidence_field(b) for b in schema.get("anyOf") or [])


def _operation_schemas(op: dict[str, Any]) -> list[tuple[str, Any]]:
    """[(location_prefix, schema)] — prefixed the same way diff_specs locations are ("request.body",
    "response.200.body"), so two fields that happen to share a name in the request and a response
    don't look like duplicates of each other in the ambiguity list."""
    schemas = []
    req = ((op.get("requestBody") or {}).get("content") or {}).get("application/json", {}).get("schema")
    if req is not None:
        schemas.append(("request.body", req))
    for status, resp in (op.get("responses") or {}).items():
        if isinstance(resp, dict) and not resp.get("x-inferred"):  # a guess has no confidence of its own
            s = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
            if s is not None:
                schemas.append((f"response.{status}.body", s))
    return schemas


# Ambiguity kinds serious enough to drag a whole endpoint's confidence down to "low" on their own.
# "rare_field" is deliberately excluded: most real APIs have a handful of genuinely optional/debug
# fields, and treating every one as "low confidence" would make the pill useless by making it universal.
_SERIOUS_AMBIGUITY_KINDS = {"type_conflict", "possible_version_mix", "inconsistent_casing"}


def _ambiguity_kinds(value: Any) -> set[str]:
    """The kind(s) an x-ambiguity value carries — a plain string ("rare_field"), a dict
    ({"kind": "possible_version_mix", ...}), or a list combining more than one of either."""
    if isinstance(value, dict):
        return {value.get("kind")}
    if isinstance(value, list):
        return {k for v in value for k in _ambiguity_kinds(v)}
    return {value}


def _is_serious(found: list[dict[str, Any]]) -> bool:
    return any(_ambiguity_kinds(f["ambiguity"]) & _SERIOUS_AMBIGUITY_KINDS for f in found)


def endpoint_confidence(op: dict[str, Any], found: list[dict[str, Any]]) -> str:
    """high/medium/low — a coarse per-endpoint summary of A3's field-level presence confidence, for a
    single pill in the dashboard: low if any field's evidence was thin or a SERIOUS ambiguity was found
    (not a routine rare/optional field), otherwise scaled by how much traffic this endpoint has seen."""
    if _is_serious(found) or any(_has_low_confidence_field(s) for _, s in _operation_schemas(op)):
        return "low"
    n = op.get("x-sample-count", 0)
    if n < LOW_CONFIDENCE_MIN_SAMPLES:
        return "low"
    if n < MEDIUM_CONFIDENCE_MIN_SAMPLES:
        return "medium"
    return "high"


def build_quality_report(spec: dict[str, Any], lines_read: int, skipped: dict[str, int]) -> dict[str, Any]:
    """The full report: parse-rate summary, per-endpoint sample count/confidence/ambiguities, and the
    flat ambiguity list the dashboard's quality card expands into."""
    total_skipped = sum(skipped.values())
    total_lines = lines_read + total_skipped
    endpoints: list[dict[str, Any]] = []
    all_ambiguities: list[dict[str, Any]] = []

    for path, item in sorted((spec.get("paths") or {}).items()):
        if not isinstance(item, dict):
            continue
        for method in _HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            found: list[dict[str, Any]] = []
            for prefix, schema in _operation_schemas(op):
                _walk_ambiguities(schema, prefix, found)
            confidence = endpoint_confidence(op, found)
            endpoints.append({
                "method": method.upper(), "path": path,
                "sample_count": op.get("x-sample-count", 0),
                "confidence": confidence,
                "ambiguities": found,
            })
            for f in found:
                all_ambiguities.append({"method": method.upper(), "path": path, **f})

    return {
        "lines_read": lines_read,
        "lines_skipped": total_skipped,
        "skipped_by_reason": dict(sorted(skipped.items())),
        "parse_rate": round(lines_read / total_lines, 4) if total_lines else 1.0,
        "endpoint_count": len(endpoints),
        "ambiguity_count": len(all_ambiguities),
        "endpoints": endpoints,
        "ambiguities": all_ambiguities,
    }
