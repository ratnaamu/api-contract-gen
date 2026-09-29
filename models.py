"""Shared data types. Every module imports from here so interfaces stay in sync.

Change this file only with a heads-up to the whole team.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

JSONValue = Any  # dict | list | str | int | float | bool | None
JSONSchema = dict[str, Any]  # a JSON Schema (draft-07-ish) or OpenAPI 3.0 schema object
EndpointKey = tuple[str, str]  # (METHOD, path_template), e.g. ("GET", "/users/{id}")


class LogEntry(TypedDict):
    """One parsed log line. Exactly the log format from the project spec."""
    timestamp: str                    # ISO 8601, e.g. "2026-09-01T09:00:15.394000Z"
    method: str                       # upper-case HTTP method
    path: str                         # raw path, no query string, e.g. "/users/12"
    query: dict[str, str]             # query params (values are strings)
    request_body: JSONValue | None
    status: int
    response_body: JSONValue | None
    headers: dict[str, str]           # request headers


@dataclass
class ParamInfo:
    """A path or query parameter observed in logs."""
    name: str
    location: Literal["path", "query"]
    schema: JSONSchema                # e.g. {"type": "integer"} or {"type": "string", "format": "uuid"}
    required: bool                    # path params: always True; query: seen in every request


@dataclass
class EndpointSchema:
    """Everything inferred for one (method, path_template). Output of inferrer, input of spec_builder."""
    method: str
    path_template: str                                         # "/users/{id}"
    params: list[ParamInfo] = field(default_factory=list)
    request_schema: JSONSchema | None = None                   # None if no request bodies seen
    responses: dict[int, JSONSchema | None] = field(default_factory=dict)  # status -> body schema (None = empty body)
    examples: dict[int, JSONValue] = field(default_factory=dict)           # status -> one real response body
    sample_count: int = 0
    first_seen: str | None = None                              # timestamp
    last_seen: str | None = None


ChangeKind = Literal[
    "endpoint_added", "endpoint_removed",
    "status_added", "status_removed",
    "field_added", "field_removed",
    "type_changed", "became_required", "became_optional",
    "field_renamed",
]


@dataclass
class SpecChange:
    """One difference between two specs. Output of watcher.diff_specs, shown on the dashboard."""
    kind: ChangeKind
    method: str
    path: str
    location: str          # e.g. "response.200.body.email", "request.body.items", "" for endpoint-level
    detail: str            # human-readable, e.g. "type integer -> string"
    breaking: bool
    detected_at: str       # ISO timestamp
