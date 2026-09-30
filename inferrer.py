"""inferrer.py — infer JSON schemas, required vs optional fields, and status codes.   Owner: [Name 2]

Contract:
    dict[EndpointKey, list[LogEntry]]  ->  list[EndpointSchema]
Uses genson. Output schemas are plain JSON Schema; spec_builder converts to OpenAPI 3.0.

Body schemas are then enriched from the observed values (rule-based, no AI) so the Prism mock's
dynamic mode (-d) generates realistic data:
  - "format" when EVERY observed value matches: email, date, date-time, uuid, uri
  - "enum" for strings with at most ENUM_MAX_VALUES distinct values, seen at least ENUM_MIN_SAMPLES times
  - "minimum"/"maximum" for integers, from the observed range
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from genson import SchemaBuilder

from llm_refine import AidhClient, refine_field_descriptions
from models import EndpointKey, EndpointSchema, JSONSchema, LogEntry, ParamInfo
from normalizer import id_param_type, normalize_path

_PLACEHOLDER_RE = re.compile(r"\{([^}/]+)\}")
_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_BOOL_VALUES = {"true", "false"}

ENUM_MAX_VALUES = 8
ENUM_MIN_SAMPLES = 20

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_URI_RE = re.compile(r"^https?://[^\s/?#]+[^\s]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([Zz]|[+-]\d{2}:?\d{2})?$")


def infer_schema(samples: list[Any]) -> JSONSchema | None:
    """Merge samples with genson.SchemaBuilder and return the schema (without "$schema").

    - Returns None if `samples` is empty or all None.
    - `required` = keys present in EVERY object sample (genson does this by default).
    - A field that is sometimes null yields {"type": ["string", "null"]} — leave it; spec_builder handles it.
    """
    values = [s for s in samples if s is not None]  # a None top-level body means "no body", not a null value
    if not values:
        return None
    builder = SchemaBuilder()
    for v in values:
        builder.add_object(v)
    schema = builder.to_schema()
    schema.pop("$schema", None)
    return schema


# ---------------------------------------------------------------------------
# Enrichment for realistic mock data
# ---------------------------------------------------------------------------

def _is_date(v: str) -> bool:
    if not _DATE_RE.match(v):
        return False
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _is_datetime(v: str) -> bool:
    if not _DATETIME_RE.match(v):
        return False
    try:
        datetime.fromisoformat(re.sub(r"[Zz]$", "+00:00", v.replace(" ", "T").replace("t", "T")))
    except ValueError:
        return False
    return True


_FORMATS: list[tuple[str, Any]] = [  # checked in this order; the first one EVERY value matches wins
    ("uuid", _UUID_RE.match),
    ("date-time", _is_datetime),
    ("date", _is_date),
    ("email", _EMAIL_RE.match),
    ("uri", _URI_RE.match),
]


def detect_format(values: list[str]) -> str | None:
    """The string format all values share (email, date, date-time, uuid, uri), or None."""
    if not values:
        return None
    for name, check in _FORMATS:
        if all(check(v) for v in values):
            return name
    return None


def _types(schema: JSONSchema) -> list[str]:
    t = schema.get("type")
    return [t] if isinstance(t, str) else [x for x in (t or []) if isinstance(x, str)]


def _enrich(schema: Any, values: list[Any]) -> None:
    if not isinstance(schema, dict) or not values:
        return
    for branch in schema.get("anyOf") or []:
        _enrich(branch, values)  # each branch only looks at the values of its own type
    types = _types(schema)
    if "object" in types:
        objects = [v for v in values if isinstance(v, dict)]
        for name, sub in (schema.get("properties") or {}).items():
            _enrich(sub, [o[name] for o in objects if o.get(name) is not None])
    if "array" in types and isinstance(schema.get("items"), dict):
        _enrich(schema["items"], [x for v in values if isinstance(v, list) for x in v if x is not None])
    if "string" in types:
        strings = [v for v in values if isinstance(v, str)]
        fmt = detect_format(strings)
        if fmt:
            schema["format"] = fmt
        elif len(strings) >= ENUM_MIN_SAMPLES and len(set(strings)) <= ENUM_MAX_VALUES:
            enum: list[Any] = sorted(set(strings))
            if "null" in types:
                enum.append(None)  # OpenAPI 3.0: a nullable enum must list null to allow it
            schema["enum"] = enum
    if "integer" in types and "number" not in types:
        ints = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
        if ints:
            schema["minimum"], schema["maximum"] = min(ints), max(ints)


def enrich_schema(schema: JSONSchema | None, samples: list[Any]) -> JSONSchema | None:
    """Add format / enum / minimum+maximum to `schema` (in place) from the observed `samples`."""
    if schema is not None:
        _enrich(schema, [s for s in samples if s is not None])
    return schema


def _query_value_schema(values: list[str]) -> JSONSchema:
    """Type query values: integer / number / boolean if EVERY value parses that way, else string."""
    vals = [str(v).strip() for v in values]
    if not vals:
        return {"type": "string"}
    if all(_INT_RE.match(v) for v in vals):
        return {"type": "integer"}
    if all(_is_number(v) for v in vals):
        return {"type": "number"}
    if all(v.lower() in _BOOL_VALUES for v in vals):
        return {"type": "boolean"}
    return {"type": "string"}


def _is_number(v: str) -> bool:
    try:
        f = float(v)
    except ValueError:
        return False
    return f == f and f not in (float("inf"), float("-inf"))  # reject nan/inf


def infer_params(template: str, entries: list[LogEntry]) -> list[ParamInfo]:
    """Path params (from normalizer.normalize_path on each entry) + query params.
    Query param types and `required` come from 2xx entries when there are any (like request bodies:
    rejected requests often carry invalid values on purpose, e.g. ?limit=all -> 400); `required` =
    present in every such entry. Params seen only in failed requests are still listed, typed from those.
    Query values are strings in logs; type them as integer/number/boolean if every value parses that way."""
    params: list[ParamInfo] = []

    # --- path params, in template order ---
    path_names = _PLACEHOLDER_RE.findall(template)
    path_values: dict[str, list[str]] = {n: [] for n in path_names}
    for e in entries:
        _, raw = normalize_path(e.get("path", ""))
        for name in path_names:
            if name in raw:
                path_values[name].append(raw[name])
    for name in path_names:
        params.append(ParamInfo(name=name, location="path",
                                schema=id_param_type(path_values[name]), required=True))

    # --- query params, in first-seen order ---
    ok = [e for e in entries if _is_2xx(e)]
    basis = ok or entries

    def collect(rows: list[LogEntry]) -> tuple[dict[str, list[str]], dict[str, int]]:
        values: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        for e in rows:
            for name, value in (e.get("query") or {}).items():
                values.setdefault(name, [])
                counts[name] = counts.get(name, 0) + 1
                if value is not None:
                    values[name].append(str(value))
        return values, counts

    all_values, _ = collect(entries)
    basis_values, basis_counts = collect(basis)
    total = len(basis)
    for name in all_values:  # first-seen order over all entries
        vals = basis_values.get(name, all_values[name])
        params.append(ParamInfo(name=name, location="query",
                                schema=_query_value_schema(vals),
                                required=total > 0 and basis_counts.get(name, 0) == total))
    return params


def _is_2xx(entry: LogEntry) -> bool:
    try:
        return 200 <= int(entry["status"]) < 300
    except (KeyError, TypeError, ValueError):
        return False


def infer_endpoint(method: str, template: str, entries: list[LogEntry], llm: AidhClient | None = None) -> EndpointSchema:
    """Build the EndpointSchema for one endpoint.

    - request_schema: from request_body of entries with 2xx status only (4xx bodies are often invalid on purpose).
    - responses: one schema per distinct status code; None when every body for that status is null (e.g. 204).
    - examples: first non-null response_body per status.
    - body schemas get format / enum / minimum+maximum from the observed values (enrich_schema).
    - sample_count / first_seen / last_seen from the entries.
    - if `llm` is given, fields without an obvious rule-based meaning also get an LLM-written
      "description" (see llm_refine.refine_field_descriptions); `llm=None` (the default) leaves this
      step out entirely, so behaviour is unchanged for every caller that doesn't opt in.
    """
    request_samples = [e.get("request_body") for e in entries if 200 <= int(e["status"]) < 300]

    by_status: dict[int, list[Any]] = {}
    for e in entries:
        by_status.setdefault(int(e["status"]), []).append(e.get("response_body"))

    responses: dict[int, JSONSchema | None] = {}
    examples: dict[int, Any] = {}
    for status in sorted(by_status):
        bodies = by_status[status]
        responses[status] = enrich_schema(infer_schema(bodies), bodies)
        example = next((b for b in bodies if b is not None), None)
        if example is not None:
            examples[status] = example

    timestamps = sorted(t for t in (e.get("timestamp") for e in entries) if t)

    request_schema = enrich_schema(infer_schema(request_samples), request_samples)
    if llm is not None:
        refine_field_descriptions(request_schema, request_samples, f"{method.upper()} {template} request body", llm)
        for status, schema in responses.items():
            refine_field_descriptions(schema, by_status[status], f"{method.upper()} {template} {status} response body", llm)

    return EndpointSchema(
        method=method.upper(),
        path_template=template,
        params=infer_params(template, entries),
        request_schema=request_schema,
        responses=responses,
        examples=examples,
        sample_count=len(entries),
        first_seen=timestamps[0] if timestamps else None,
        last_seen=timestamps[-1] if timestamps else None,
    )


def infer_all(grouped: dict[EndpointKey, list[LogEntry]], llm: AidhClient | None = None) -> list[EndpointSchema]:
    """infer_endpoint for every group, sorted by (path_template, method)."""
    return [
        infer_endpoint(method, template, entries, llm)
        for (method, template), entries in sorted(grouped.items(), key=lambda kv: (kv[0][1], kv[0][0]))
        if entries
    ]
