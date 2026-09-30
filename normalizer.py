"""normalizer.py — turn concrete paths into templates.   Owner: [Name 1]

Contract:
    "/users/12"                                   -> ("/users/{id}", {"id": "12"})
    "/products/598336e3-75d6-4ed4-ab1f-a9f2d10bd1d0" -> ("/products/{id}", {"id": "598336e3-..."})
    list[LogEntry] -> dict[EndpointKey, list[LogEntry]]
"""
from __future__ import annotations

import re

from models import EndpointKey, LogEntry

_INT_RE = re.compile(r"^[0-9]+$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_OBJECTID_RE = re.compile(r"^[0-9a-fA-F]{24}$")
# Long opaque token that contains at least one digit, e.g. "usr_000012", "ord-8f3k2a91".
_TOKEN_RE = re.compile(r"^(?=.*[0-9])[A-Za-z0-9_\-]{8,}$")
_VERSION_SEGMENT_RE = re.compile(r"^v(\d+)$", re.IGNORECASE)


def is_id_segment(segment: str) -> bool:
    """True if a path segment looks like an identifier: integer, UUID, 24-char hex (Mongo ObjectId),
    or a long token with digits (e.g. "usr_000012"). Plain words ("users", "me") -> False."""
    if not segment:
        return False
    return bool(
        _INT_RE.match(segment)
        or _UUID_RE.match(segment)
        or _OBJECTID_RE.match(segment)
        or _TOKEN_RE.match(segment)
    )


def id_param_type(values: list[str]) -> dict:
    """Infer a schema for observed path-param values:
    all ints -> {"type": "integer"}, all UUIDs -> {"type": "string", "format": "uuid"}, else {"type": "string"}."""
    if values and all(_INT_RE.match(v) for v in values):
        return {"type": "integer"}
    if values and all(_UUID_RE.match(v) for v in values):
        return {"type": "string", "format": "uuid"}
    return {"type": "string"}


def _singularize(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 3:
        return word[:-3] + "y"          # categories -> category
    if w.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]                # addresses -> address, boxes -> box
    if w.endswith("s") and not w.endswith("ss") and len(w) > 1:
        return word[:-1]                # users -> user
    return word


def _camel(word: str) -> str:
    """'line-items' / 'line_items' -> 'lineItem' base (already singularised)."""
    parts = [p for p in re.split(r"[-_.]", word) if p]
    if not parts:
        return "id"
    return parts[0].lower() + "".join(p[:1].upper() + p[1:].lower() for p in parts[1:])


def normalize_path(path: str) -> tuple[str, dict[str, str]]:
    """Replace ID-like segments with placeholders.

    Naming: a single ID uses "{id}". With several IDs, name each after the preceding
    segment, singularised: "/users/5/orders/9" -> "/users/{userId}/orders/{orderId}".
    Trailing slashes are stripped ("/users/" -> "/users"); "/" stays "/".
    Returns (template, {param_name: raw_value}).
    """
    path = (path or "/").split("?", 1)[0].split("#", 1)[0]
    segments = [s for s in path.split("/") if s]  # drops leading/trailing/double slashes
    if not segments:
        return "/", {}

    id_positions = [i for i, s in enumerate(segments) if is_id_segment(s)]
    params: dict[str, str] = {}
    out = list(segments)

    for i in id_positions:
        if len(id_positions) == 1:
            name = "id"
        else:
            prev = segments[i - 1] if i > 0 and not is_id_segment(segments[i - 1]) else ""
            name = _camel(_singularize(prev)) + "Id" if prev else "id"
        # Guarantee uniqueness: id, id2, id3 ...
        base, n = name, 2
        while name in params:
            name = f"{base}{n}"
            n += 1
        params[name] = segments[i]
        out[i] = "{" + name + "}"

    return "/" + "/".join(out), params


def extract_path_version(path: str) -> str | None:
    """The API version from a leading /v<N>/... segment ("v1", "v2"), or None (A5: an explicit version
    signal). Purely additive metadata: normalize_path already leaves a "v1"/"v2" segment as a literal
    part of the template (it isn't ID-shaped), so /v1/users/12 and /v2/users/12 stay two distinct
    templates as before — this just names the version so spec_builder can tag each operation with it."""
    segments = [s for s in (path or "").split("?", 1)[0].split("/") if s]
    if segments and (m := _VERSION_SEGMENT_RE.match(segments[0])):
        return f"v{m.group(1)}"
    return None


def group_by_endpoint(entries: list[LogEntry]) -> dict[EndpointKey, list[LogEntry]]:
    """Group entries by (METHOD, path_template). Preserves entry order within each group."""
    grouped: dict[EndpointKey, list[LogEntry]] = {}
    for entry in entries:
        template, _ = normalize_path(entry["path"])
        key: EndpointKey = (entry["method"].upper(), template)
        grouped.setdefault(key, []).append(entry)
    return grouped
