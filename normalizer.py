"""normalizer.py — turn concrete paths into templates.   Owner: [Name 1]

Contract:
    "/users/12"                                   -> ("/users/{id}", {"id": "12"})
    "/products/598336e3-75d6-4ed4-ab1f-a9f2d10bd1d0" -> ("/products/{id}", {"id": "598336e3-..."})
    list[LogEntry] -> dict[EndpointKey, list[LogEntry]]
"""
from __future__ import annotations

from models import EndpointKey, LogEntry


def is_id_segment(segment: str) -> bool:
    """True if a path segment looks like an identifier: integer, UUID, 24-char hex (Mongo ObjectId),
    or a long token with digits (e.g. "usr_000012"). Plain words ("users", "me") -> False."""
    raise NotImplementedError


def id_param_type(values: list[str]) -> dict:
    """Infer a schema for observed path-param values:
    all ints -> {"type": "integer"}, all UUIDs -> {"type": "string", "format": "uuid"}, else {"type": "string"}."""
    raise NotImplementedError


def normalize_path(path: str) -> tuple[str, dict[str, str]]:
    """Replace ID-like segments with placeholders.

    Naming: a single ID uses "{id}". With several IDs, name each after the preceding
    segment, singularised: "/users/5/orders/9" -> "/users/{userId}/orders/{orderId}".
    Trailing slashes are stripped ("/users/" -> "/users"); "/" stays "/".
    Returns (template, {param_name: raw_value}).
    """
    raise NotImplementedError


def group_by_endpoint(entries: list[LogEntry]) -> dict[EndpointKey, list[LogEntry]]:
    """Group entries by (METHOD, path_template). Preserves entry order within each group."""
    raise NotImplementedError
