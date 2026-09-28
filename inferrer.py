"""inferrer.py — infer JSON schemas, required vs optional fields, and status codes.   Owner: [Name 2]

Contract:
    dict[EndpointKey, list[LogEntry]]  ->  list[EndpointSchema]
Uses genson. Output schemas are plain JSON Schema; spec_builder converts to OpenAPI 3.0.
"""
from __future__ import annotations

from typing import Any

from models import EndpointKey, EndpointSchema, JSONSchema, LogEntry, ParamInfo


def infer_schema(samples: list[Any]) -> JSONSchema | None:
    """Merge samples with genson.SchemaBuilder and return the schema (without "$schema").

    - Returns None if `samples` is empty or all None.
    - `required` = keys present in EVERY object sample (genson does this by default).
    - A field that is sometimes null yields {"type": ["string", "null"]} — leave it; spec_builder handles it.
    """
    raise NotImplementedError


def infer_params(template: str, entries: list[LogEntry]) -> list[ParamInfo]:
    """Path params (from normalizer.normalize_path on each entry) + query params.
    Query param `required` = present in every entry. Query values are strings in logs;
    type them as integer/number/boolean if every observed value parses that way."""
    raise NotImplementedError


def infer_endpoint(method: str, template: str, entries: list[LogEntry]) -> EndpointSchema:
    """Build the EndpointSchema for one endpoint.

    - request_schema: from request_body of entries with 2xx status only (4xx bodies are often invalid on purpose).
    - responses: one schema per distinct status code; None when every body for that status is null (e.g. 204).
    - examples: first non-null response_body per status.
    - sample_count / first_seen / last_seen from the entries.
    """
    raise NotImplementedError


def infer_all(grouped: dict[EndpointKey, list[LogEntry]]) -> list[EndpointSchema]:
    """infer_endpoint for every group, sorted by (path_template, method)."""
    raise NotImplementedError
