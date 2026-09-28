"""inferrer.py — infer JSON schemas, required vs optional fields, and status codes.   Owner: [Name 2]

Contract:
    dict[EndpointKey, list[LogEntry]]  ->  list[EndpointSchema]
Uses genson. Output schemas are plain JSON Schema; spec_builder converts to OpenAPI 3.0.
"""
from __future__ import annotations

import re
from typing import Any

from genson import SchemaBuilder

from models import EndpointKey, EndpointSchema, JSONSchema, LogEntry, ParamInfo
from normalizer import id_param_type, normalize_path

_PLACEHOLDER_RE = re.compile(r"\{([^}/]+)\}")
_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_BOOL_VALUES = {"true", "false"}


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
    Query param `required` = present in every entry. Query values are strings in logs;
    type them as integer/number/boolean if every observed value parses that way."""
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
    query_values: dict[str, list[str]] = {}
    query_counts: dict[str, int] = {}
    for e in entries:
        q = e.get("query") or {}
        for name, value in q.items():
            query_values.setdefault(name, [])
            query_counts[name] = query_counts.get(name, 0) + 1
            if value is not None:
                query_values[name].append(str(value))
    total = len(entries)
    for name, vals in query_values.items():
        params.append(ParamInfo(name=name, location="query",
                                schema=_query_value_schema(vals),
                                required=total > 0 and query_counts[name] == total))
    return params


def infer_endpoint(method: str, template: str, entries: list[LogEntry]) -> EndpointSchema:
    """Build the EndpointSchema for one endpoint.

    - request_schema: from request_body of entries with 2xx status only (4xx bodies are often invalid on purpose).
    - responses: one schema per distinct status code; None when every body for that status is null (e.g. 204).
    - examples: first non-null response_body per status.
    - sample_count / first_seen / last_seen from the entries.
    """
    request_samples = [e.get("request_body") for e in entries if 200 <= int(e["status"]) < 300]

    by_status: dict[int, list[Any]] = {}
    for e in entries:
        by_status.setdefault(int(e["status"]), []).append(e.get("response_body"))

    responses: dict[int, JSONSchema | None] = {}
    examples: dict[int, Any] = {}
    for status in sorted(by_status):
        bodies = by_status[status]
        responses[status] = infer_schema(bodies)
        example = next((b for b in bodies if b is not None), None)
        if example is not None:
            examples[status] = example

    timestamps = sorted(t for t in (e.get("timestamp") for e in entries) if t)

    return EndpointSchema(
        method=method.upper(),
        path_template=template,
        params=infer_params(template, entries),
        request_schema=infer_schema(request_samples),
        responses=responses,
        examples=examples,
        sample_count=len(entries),
        first_seen=timestamps[0] if timestamps else None,
        last_seen=timestamps[-1] if timestamps else None,
    )


def infer_all(grouped: dict[EndpointKey, list[LogEntry]]) -> list[EndpointSchema]:
    """infer_endpoint for every group, sorted by (path_template, method)."""
    return [
        infer_endpoint(method, template, entries)
        for (method, template), entries in sorted(grouped.items(), key=lambda kv: (kv[0][1], kv[0][0]))
        if entries
    ]
