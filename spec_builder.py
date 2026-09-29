"""spec_builder.py — build, validate and write openapi.json + openapi.yaml.   Owner: [Name 2]

Contract:
    list[EndpointSchema]  ->  OpenAPI 3.0.3 dict  ->  openapi.json (main output) + openapi.yaml (same spec)
Validated with openapi-spec-validator. Prism must be able to serve the output.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any

import yaml

from models import EndpointSchema, JSONSchema

log = logging.getLogger(__name__)

OPENAPI_VERSION = "3.0.3"

# Which JSON Schema keywords belong to which type. Used when a single genson schema carries several
# types at once (e.g. {"type": ["array", "object"], "items": ..., "properties": ...}) and we must split
# it into anyOf branches.
_TYPE_KEYWORDS: dict[str, set[str]] = {
    "object": {"properties", "required", "additionalProperties", "patternProperties",
               "minProperties", "maxProperties"},
    "array": {"items", "minItems", "maxItems", "uniqueItems"},
    "string": {"format", "pattern", "minLength", "maxLength", "enum"},
    "integer": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"},
    "number": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"},
    "boolean": set(),
}
_ALL_TYPE_KEYWORDS = set().union(*_TYPE_KEYWORDS.values())
# Keywords OpenAPI 3.0 does not accept in a Schema Object.
_DROP_KEYWORDS = {"$schema", "$id", "patternProperties", "const", "examples", "contains",
                  "propertyNames", "dependencies", "if", "then", "else"}


# ---------------------------------------------------------------------------
# JSON Schema (genson) -> OpenAPI 3.0 Schema Object
# ---------------------------------------------------------------------------

def _make_nullable(schema: JSONSchema) -> JSONSchema:
    out = dict(schema)
    out["nullable"] = True
    return out


def _convert_children(schema: JSONSchema) -> JSONSchema:
    """Recurse into properties / items / additionalProperties / anyOf / oneOf / allOf / not."""
    out: JSONSchema = {}
    for key, value in schema.items():
        if key in _DROP_KEYWORDS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {str(k): to_openapi_schema(v) for k, v in value.items()}
        elif key == "items":
            if isinstance(value, list):  # tuple-style items: 3.0 has none -> anyOf of the positions
                branches = [to_openapi_schema(v) for v in value]
                out[key] = branches[0] if len(branches) == 1 else ({"anyOf": branches} if branches else {})
            elif isinstance(value, dict):
                out[key] = to_openapi_schema(value)
            else:
                out[key] = {}
        elif key == "additionalProperties" and isinstance(value, dict):
            out[key] = to_openapi_schema(value)
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, list):
            out[key] = [to_openapi_schema(v) for v in value]
        elif key == "not" and isinstance(value, dict):
            out[key] = to_openapi_schema(value)
        elif key == "required":
            if isinstance(value, list) and value:  # 3.0 requires minItems: 1
                out[key] = list(dict.fromkeys(str(v) for v in value))
        else:
            out[key] = copy.deepcopy(value)

    # OpenAPI 3.0 requires `items` when type is array (genson omits it for arrays that were always empty).
    if out.get("type") == "array" and "items" not in out:
        out["items"] = {}
    return out


def _collapse_anyof_nulls(schema: JSONSchema) -> JSONSchema:
    """{"anyOf": [{"type": "null"}, X, ...]} -> X+nullable (single) or anyOf of nullable branches."""
    branches = schema.get("anyOf")
    if not isinstance(branches, list):
        return schema
    has_null = any(_is_pure_null(b) for b in branches)
    if not has_null:
        return schema
    rest = [b for b in branches if not _is_pure_null(b)]
    others = {k: v for k, v in schema.items() if k != "anyOf"}
    if not rest:
        return {**others, "nullable": True}
    if len(rest) == 1:
        return {**others, **_make_nullable(rest[0])}
    return {**others, "anyOf": [_make_nullable(b) for b in rest]}


def _is_pure_null(schema: Any) -> bool:
    # after conversion {"type": "null"} becomes {"nullable": True}
    return isinstance(schema, dict) and (schema == {"nullable": True} or schema == {"type": "null"})


def to_openapi_schema(schema: JSONSchema) -> JSONSchema:
    """Convert a genson JSON Schema to an OpenAPI 3.0 schema object (recursive).

    - Drop "$schema".
    - {"type": ["string", "null"]} -> {"type": "string", "nullable": True}
    - {"type": "null"} alone -> {"nullable": True}  (3.0 has no null type)
    - Multi-type without null ({"type": ["integer", "string"]}) -> {"anyOf": [...]}
    - Recurse into properties, items, anyOf.
    """
    if not isinstance(schema, dict):
        return {}

    t = schema.get("type")

    # ---- type arrays: ["string", "null"], ["integer", "string"], ["array", "object", "null"] ----
    if isinstance(t, list):
        types = [x for x in dict.fromkeys(t) if isinstance(x, str)]  # dedupe, keep order
        nullable = "null" in types
        types = [x for x in types if x != "null"]
        # genson treats integer ⊂ number; if both appear keep just number
        if "integer" in types and "number" in types:
            types.remove("integer")
        base = {k: v for k, v in schema.items() if k != "type"}

        if not types:
            result = _convert_children({k: v for k, v in base.items() if k not in _ALL_TYPE_KEYWORDS})
            result["nullable"] = True
            return result
        if len(types) == 1:
            result = to_openapi_schema({**base, "type": types[0]})
            if nullable:
                result["nullable"] = True
            return result

        # several real types -> anyOf, each branch keeps only the keywords that apply to its type
        common = {k: v for k, v in base.items() if k not in _ALL_TYPE_KEYWORDS}
        branches = []
        for typ in types:
            own = {k: v for k, v in base.items() if k in _TYPE_KEYWORDS.get(typ, set())}
            branch = to_openapi_schema({"type": typ, **own})
            if nullable:
                branch["nullable"] = True
            branches.append(branch)
        result = _convert_children(common)
        result["anyOf"] = branches
        return result

    # ---- single null type ----
    if t == "null":
        rest = {k: v for k, v in schema.items() if k != "type" and k not in _ALL_TYPE_KEYWORDS}
        result = _convert_children(rest)
        result["nullable"] = True
        return result

    # ---- ordinary schema ----
    result = _convert_children(schema)
    return _collapse_anyof_nulls(result)


# ---------------------------------------------------------------------------
# Operations / spec assembly
# ---------------------------------------------------------------------------

_NON_WORD = re.compile(r"[^0-9A-Za-z]+")
_PLACEHOLDER_RE = re.compile(r"\{([^}/]+)\}")


def _operation_id(method: str, path_template: str) -> str:
    """GET /users/{id} -> get_users_by_id ; POST /orders -> post_orders ; GET / -> get_root."""
    parts: list[str] = []
    for seg in path_template.strip("/").split("/"):
        if not seg:
            continue
        m = _PLACEHOLDER_RE.fullmatch(seg)
        if m:
            parts.append("by_" + _NON_WORD.sub("_", m.group(1)).strip("_"))
        else:
            parts.append(_NON_WORD.sub("_", seg).strip("_"))
    name = "_".join(p for p in parts if p) or "root"
    return f"{method.lower()}_{name}"


def _status_description(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return f"Status {status}"


def _parameters(ep: EndpointSchema) -> list[dict[str, Any]]:
    params: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for p in ep.params:
        key = (p.name, p.location)
        if key in seen:
            continue
        seen.add(key)
        params.append({
            "name": p.name,
            "in": p.location,
            "required": True if p.location == "path" else bool(p.required),
            "schema": to_openapi_schema(p.schema) or {"type": "string"},
        })
    # Every {placeholder} in the template MUST be declared, or the spec is invalid.
    for name in _PLACEHOLDER_RE.findall(ep.path_template):
        if (name, "path") not in seen:
            seen.add((name, "path"))
            params.append({"name": name, "in": "path", "required": True, "schema": {"type": "string"}})
    return params


def build_operation(ep: EndpointSchema) -> dict[str, Any]:
    """One OpenAPI operation object: operationId, parameters, requestBody (if any),
    responses (application/json content + example per status; status with None body -> description only)."""
    op: dict[str, Any] = {
        "operationId": _operation_id(ep.method, ep.path_template),
        "summary": f"{ep.method.upper()} {ep.path_template}",
    }
    first_seg = next((s for s in ep.path_template.strip("/").split("/") if s and not s.startswith("{")), None)
    if first_seg:
        op["tags"] = [first_seg]

    params = _parameters(ep)
    if params:
        op["parameters"] = params

    if ep.request_schema is not None:
        op["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": to_openapi_schema(ep.request_schema)}},
        }

    responses: dict[str, Any] = {}
    for status in sorted(ep.responses):
        body_schema = ep.responses[status]
        resp: dict[str, Any] = {"description": _status_description(int(status))}
        if body_schema is not None:
            media: dict[str, Any] = {"schema": to_openapi_schema(body_schema)}
            if status in ep.examples and ep.examples[status] is not None:
                media["example"] = copy.deepcopy(ep.examples[status])
            resp["content"] = {"application/json": media}
        responses[str(status)] = resp  # keys MUST be strings for the validator / YAML
    if not responses:
        responses["default"] = {"description": "No responses observed"}
    op["responses"] = responses

    extras = {"x-sample-count": ep.sample_count}
    if ep.first_seen:
        extras["x-first-seen"] = ep.first_seen
    if ep.last_seen:
        extras["x-last-seen"] = ep.last_seen
    op.update(extras)
    return op


def build_spec(endpoints: list[EndpointSchema], title: str = "Inferred API", version: str = "0.1.0") -> dict[str, Any]:
    """Assemble the full spec. Empty `endpoints` must still give a VALID spec with `paths: {}`
    (demo step 1 starts from an empty spec)."""
    paths: dict[str, dict[str, Any]] = {}
    used_ids: set[str] = set()
    for ep in sorted(endpoints, key=lambda e: (e.path_template, e.method)):
        template = ep.path_template if ep.path_template.startswith("/") else "/" + ep.path_template
        op = build_operation(ep)
        # guarantee unique operationIds
        base_id, n = op["operationId"], 2
        while op["operationId"] in used_ids:
            op["operationId"] = f"{base_id}_{n}"
            n += 1
        used_ids.add(op["operationId"])
        paths.setdefault(template, {})[ep.method.lower()] = op

    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": title,
            "version": version,
            "description": "Generated automatically from raw HTTP logs.",
        },
        "paths": paths,
    }


# ---------------------------------------------------------------------------
# Validation + IO
# ---------------------------------------------------------------------------

def validate_spec(spec: dict[str, Any]) -> list[str]:
    """Validate with openapi_spec_validator. Returns a list of error messages; [] means valid. Never raises."""
    try:
        try:  # openapi-spec-validator >= 0.6
            from openapi_spec_validator import OpenAPIV30SpecValidator
            errors = OpenAPIV30SpecValidator(spec).iter_errors()
        except ImportError:  # older releases
            from openapi_spec_validator import openapi_v30_spec_validator
            errors = openapi_v30_spec_validator.iter_errors(spec)
        messages = []
        for err in errors:
            where = "/".join(str(p) for p in getattr(err, "absolute_path", []) or [])
            msg = getattr(err, "message", str(err))
            messages.append(f"{where}: {msg}" if where else msg)
        return messages
    except Exception as e:  # noqa: BLE001 — must never raise
        return [f"validator crashed: {type(e).__name__}: {e}"]


class _NoAliasDumper(yaml.SafeDumper):
    """safe_dump without &id001 anchors (shared dicts would otherwise become YAML aliases)."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def spec_paths(path: str | Path) -> tuple[Path, Path]:
    """(yaml_path, json_path) for a spec path: "out/openapi.yaml" -> (out/openapi.yaml, out/openapi.json),
    and "out/openapi.json" -> (out/openapi.yaml, out/openapi.json)."""
    p = Path(path)
    if p.suffix.lower() == ".json":
        return p.with_suffix(".yaml"), p
    return p, p.with_suffix(".json")


def _atomic_write_text(target: Path, text: str) -> None:
    """Write to a temp file in the same folder, fsync, then os.replace, so a reader (Prism, the dashboard)
    never sees a half-written file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # On Windows os.replace fails if another process (Prism) has the file open — retry briefly.
        for attempt in range(10):
            try:
                os.replace(tmp_name, target)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


def write_spec(spec: dict[str, Any], path: str | Path) -> tuple[Path, Path]:
    """Write the spec as openapi.json (the main output) and openapi.yaml side by side, each atomically.
    `path` may name either file; the other is written next to it. Returns (yaml_path, json_path)."""
    yaml_path, json_path = spec_paths(path)
    _atomic_write_text(json_path, json.dumps(spec, indent=2, ensure_ascii=False) + "\n")
    _atomic_write_text(yaml_path, yaml.dump(spec, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True))
    return yaml_path, json_path


def load_spec(path: str | Path) -> dict[str, Any] | None:
    """Load a spec from YAML/JSON. Returns None if the file doesn't exist."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8-sig") as f:
            data = yaml.safe_load(f)  # YAML is a superset of JSON
    except (OSError, yaml.YAMLError) as e:
        log.warning("could not load spec %s: %s", p, e)
        return None
    return data if isinstance(data, dict) else None
