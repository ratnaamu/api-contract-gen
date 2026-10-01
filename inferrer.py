"""inferrer.py — infer JSON schemas, required vs optional fields, and status codes.   Owner: [Name 2]

Contract:
    dict[EndpointKey, list[LogEntry]]  ->  list[EndpointSchema]
Uses genson for the structural merge (properties/types/nesting). Output schemas are plain JSON Schema;
spec_builder converts to OpenAPI 3.0.

Body schemas are then enriched from the observed values (rule-based, no AI). Two things happen here,
in one presence-aware pass (see `_annotate`), because both need the same walk over schema + real values:

  1. Realistic-mock hints, as before: "format" when EVERY observed value matches (email/date/date-time/
     uuid/uri); "enum" for a genuinely categorical string field; an observed numeric range.

  2. Evidence-graded presence, replacing genson's binary "required" (present in literally every sample):
     each field's `required`/`optional` status is now a function of its presence RATIO and the sample
     size `n` it was decided from — a field present in 199/200 samples is `required` (evidence, not
     one dropped line, should demote it), while a field present in 2/2 samples is *also* `required` but
     flagged `x-confidence: "low"` (too little evidence to be sure). A field observed with more than one
     JSON type is only left as a real union (`anyOf`) when no single type covers ≥95% of samples;
     otherwise the minority type is treated as noise (`x-ambiguity: "type_conflict"`) and dropped.
     `x-presence` reports a Wilson lower-bound on the ratio (not the raw ratio), so a field that's
     "always present" in 3 samples scores lower than one that's "always present" in 300 — the same
     3/3-vs-300/300 problem the raw ratio can't distinguish. Numeric ranges move to `x-observed-range`
     rather than a contract `minimum`/`maximum` (Prism-only realism is a mock-variant concern, not a
     contract claim — see spec_builder's mock-spec output). Property keys are canonicalised
     (lower_snake) before any of this runs, so `ID`/`Id`/`id` merge into one field instead of three,
     with `x-aliases` recording the spellings seen.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from math import sqrt
from typing import Any

from genson import SchemaBuilder

from llm_refine import AidhClient, refine_field_descriptions
from models import EndpointKey, EndpointSchema, JSONSchema, LogEntry, ParamInfo
from normalizer import extract_path_version, id_param_type, normalize_path

_PLACEHOLDER_RE = re.compile(r"\{([^}/]+)\}")
_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_BOOL_VALUES = {"true", "false"}

ENUM_MAX_VALUES = 8
ENUM_MIN_SAMPLES = 5           # was 20: a 1-sample endpoint gave Prism a bare `string` for `status`
ENUM_MIN_REPEAT_RATIO = 2.0    # total/distinct: every value must have been seen ~twice on average, so
                               # 5 samples with 5 distinct values is still free text, 5/2 is a category
ENUM_CONFIDENT_SAMPLES = 20    # x-enum-confidence reaches 1.0 around 3x this
ENUM_MAX_VALUE_LEN = 24        # a value longer than this looks like free text, not a category

# Presence-based required/optional decision (A3): gates are checked against the RAW presence ratio
# (matches "present in >=98% of n" literally); n additionally decides the confidence label.
REQUIRED_PRESENCE_RATIO = 0.98
LOW_CONFIDENCE_MAX_N = 10        # required but n below this -> x-confidence: "low"
RARE_FIELD_MAX_RATIO = 0.50      # below this presence ratio...
RARE_FIELD_MIN_N = 20            # ...with at least this many samples -> x-ambiguity: "rare_field"
TYPE_CONFLICT_MAJORITY_RATIO = 0.95  # one observed type must cover at least this share to "win"
ALIAS_MINORITY_RATIO = 0.10      # a minority key spelling above this share -> x-ambiguity: "inconsistent_casing"

# Field names that read as free text, not a closed category, even if a small sample makes them look
# like one (the exact bug this guards against: `address.city`/`address.zip` becoming a 5-value enum).
_FREE_TEXT_NAMES = {"name", "title", "street", "city", "zip", "postal", "email", "message",
                    "description", "url", "id"}

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_URI_RE = re.compile(r"^https?://[^\s/?#]+[^\s]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([Zz]|[+-]\d{2}:?\d{2})?$")
_SNAKE_BOUNDARY_1_RE = re.compile(r"(.)([A-Z][a-z]+)")   # ...aB(lower) -> ..._B(lower); keeps runs of
_SNAKE_BOUNDARY_2_RE = re.compile(r"([a-z0-9])([A-Z])")  # capitals (acronyms like "ID") as one token


def _canonical_key(key: str) -> str:
    """lower_snake_case form of a key, so casing/camelCase variants of the same field merge into one
    genson property instead of several ("ID", "Id", "id" -> "id"; "birthDate" -> "birth_date"; an
    acronym run like "UserID" -> "user_id", not "user_i_d")."""
    s = _SNAKE_BOUNDARY_1_RE.sub(r"\1_\2", key)
    s = _SNAKE_BOUNDARY_2_RE.sub(r"\1_\2", s)
    return s.replace("-", "_").replace(" ", "_").lower().strip("_") or key


def _canonicalize_keys(value: Any, alias_stats: dict[str, dict[str, int]], path: str = "") -> Any:
    """Recursively rewrite dict keys to their canonical spelling. Returns a NEW structure; `value` is
    untouched. `alias_stats[dotted_path]` tallies every original spelling seen for that field, keyed by
    path so "id" at different nesting depths is tracked separately — used afterwards to flag
    `x-ambiguity: "inconsistent_casing"` on genuinely mixed fields (see `_annotate_aliases`)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            canon = _canonical_key(str(k))
            field_path = f"{path}.{canon}" if path else canon
            counts = alias_stats.setdefault(field_path, {})
            counts[str(k)] = counts.get(str(k), 0) + 1
            out[canon] = _canonicalize_keys(v, alias_stats, field_path)
        return out
    if isinstance(value, list):
        return [_canonicalize_keys(x, alias_stats, path) for x in value]
    return value


def infer_schema(samples: list[Any]) -> JSONSchema | None:
    """Merge samples with genson.SchemaBuilder and return the schema (without "$schema").

    - Returns None if `samples` is empty or all None.
    - Property keys are canonicalised first (see `_canonicalize_keys`) so casing variants of the same
      field merge into one property instead of several.
    - `required` here is still genson's own binary rule (present in every sample); `enrich_schema`
      (called next by every real caller) replaces it with the evidence-graded version — this function
      stays a plain, predictable genson wrapper other than the canonicalisation.
    - A field that is sometimes null yields {"type": ["string", "null"]} — leave it; spec_builder handles it.
    """
    values = [s for s in samples if s is not None]  # a None top-level body means "no body", not a null value
    if not values:
        return None
    builder = SchemaBuilder()
    for v in values:
        builder.add_object(_canonicalize_keys(v, {}))
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


def _wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for successes/n, so a ratio backed by few samples
    (3/3) scores lower than the same ratio backed by many (300/300) — used for `x-presence` instead of
    the raw ratio, which can't tell those two cases apart."""
    if n <= 0:
        return 0.0
    p = successes / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (center - margin) / denom)


def _value_type(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "null"


def _add_ambiguity(schema: dict, kind: str) -> None:
    """Set schema["x-ambiguity"], combining with any ambiguity already flagged on this field rather
    than overwriting it (a field can be both a rare field AND have inconsistent casing, say)."""
    existing = schema.get("x-ambiguity")
    if existing is None:
        schema["x-ambiguity"] = kind
    elif isinstance(existing, list):
        if kind not in existing:
            existing.append(kind)
    elif existing != kind:
        schema["x-ambiguity"] = [existing, kind]


def _resolve_type_conflict(schema: dict, values: list[Any]) -> None:
    """genson merges every observed type into schema["type"] (a list) with no sense of how often each
    one occurred. If one type actually covers TYPE_CONFLICT_MAJORITY_RATIO+ of the real samples, treat
    the rest as noise (a truncated body typed as a string among mostly-real objects, one stray "7" among
    integers) and collapse to just that type; otherwise leave the real union (anyOf) as today."""
    t = schema.get("type")
    if not isinstance(t, list):
        return
    real_types = [x for x in t if x != "null"]
    if len(real_types) < 2:
        return
    counts: dict[str, int] = {}
    for v in values:
        vt = _value_type(v)
        if vt in real_types:
            counts[vt] = counts.get(vt, 0) + 1
    total = sum(counts.values())
    if total == 0:
        return
    majority_type, majority_n = max(counts.items(), key=lambda kv: kv[1])
    if majority_n / total >= TYPE_CONFLICT_MAJORITY_RATIO:
        _add_ambiguity(schema, "type_conflict")
        schema["x-observed-types"] = counts
        schema["type"] = [majority_type, "null"] if "null" in t else majority_type


def _looks_categorical(values: list[str]) -> bool:
    """A value that has spaces or runs long reads as free text, not a closed category."""
    return all(v and " " not in v and len(v) <= ENUM_MAX_VALUE_LEN for v in values)


def _is_free_text_name(name: str) -> bool:
    if not name:
        return False
    lname = name.lower()
    # `id` is always free text; a `*_id` reference (office_id -> OFF-LON/OFF-MAN/...) is NOT blocked by
    # name — the repeat-ratio gate in _annotate_string decides on evidence whether it's a closed set.
    return lname in _FREE_TEXT_NAMES


def _annotate_string(schema: dict, strings: list[str], name: str) -> None:
    if not strings:
        return
    fmt = detect_format(strings)
    if fmt:
        schema["format"] = fmt
        return  # format wins over enum, as before
    total, distinct = len(strings), len(set(strings))
    eligible = (
        not _is_free_text_name(name)
        and total >= ENUM_MIN_SAMPLES
        and distinct <= ENUM_MAX_VALUES
        and total / distinct >= ENUM_MIN_REPEAT_RATIO
        and _looks_categorical(strings)
    )
    if not eligible:
        return
    enum: list[Any] = sorted(set(strings))
    if schema.get("nullable"):
        enum.append(None)  # OpenAPI 3.0: a nullable enum must list null to allow it
    schema["enum"] = enum
    schema["x-enum-confidence"] = round(min(1.0, total / (ENUM_CONFIDENT_SAMPLES * 3)), 2)


def _annotate_range(schema: dict, ints: list[int]) -> None:
    if not ints:
        return
    lo, hi = min(ints), max(ints)
    if lo == hi:
        return  # a single observed value is not a range worth claiming, in the contract or out of it
    schema["x-observed-range"] = [lo, hi]
    if lo >= 0:
        schema["minimum"] = 0  # the only range hint kept in the contract itself: never negative


def _decide_presence(sub: dict, n: int, present: int, null: int, name: str, required_out: list[str]) -> None:
    """Evidence-graded required/optional/nullable for one field, from how often it was present (out of
    `n` samples where its parent object existed) and how often it was null. See the module docstring's
    decision table."""
    if present == 0:
        return  # never observed under this parent at all; not required, nothing else to say
    ratio = present / n
    sub["x-presence"] = round(_wilson_lower_bound(present, n), 4)
    if ratio >= REQUIRED_PRESENCE_RATIO:
        required_out.append(name)
        if n < LOW_CONFIDENCE_MAX_N:
            sub["x-confidence"] = "low"
    elif ratio < RARE_FIELD_MAX_RATIO and n >= RARE_FIELD_MIN_N:
        _add_ambiguity(sub, "rare_field")
    if null > 0:
        sub["nullable"] = True
        sub["x-null-rate"] = round(null / n, 4)


def _annotate_object_properties(schema: dict, objects: list[dict]) -> None:
    n = len(objects)
    if n == 0:
        return
    required: list[str] = []
    for name, sub in (schema.get("properties") or {}).items():
        if not isinstance(sub, dict):
            continue
        present = sum(1 for o in objects if name in o)
        null = sum(1 for o in objects if name in o and o[name] is None)
        _decide_presence(sub, n, present, null, name, required)
        sub_values = [o[name] for o in objects if o.get(name) is not None]
        _annotate(sub, sub_values, name)
    schema["required"] = required


def _annotate(schema: Any, values: list[Any], name: str = "") -> None:
    """Walk `schema` (genson output) alongside the real `values` reaching this node, in one pass:
    type-conflict resolution, then presence/required/nullable for object properties, then format/enum/
    range for the resolved leaf type. Mutates `schema` in place. `name` is the field name this node is
    reached through (used only for the enum free-text-name guard); "" at the root / inside an array."""
    if not isinstance(schema, dict) or not values:
        return
    for branch in schema.get("anyOf") or []:
        _annotate(branch, values, name)  # each branch only looks at the values of its own type
    _resolve_type_conflict(schema, values)
    types = _types(schema)
    if "object" in types:
        objects = [v for v in values if isinstance(v, dict)]
        _annotate_object_properties(schema, objects)
    if "array" in types and isinstance(schema.get("items"), dict):
        _annotate(schema["items"], [x for v in values if isinstance(v, list) for x in v if x is not None], name)
    if "string" in types:
        _annotate_string(schema, [v for v in values if isinstance(v, str)], name)
    if "integer" in types and "number" not in types:
        _annotate_range(schema, [v for v in values if isinstance(v, int) and not isinstance(v, bool)])


def _annotate_aliases(schema: Any, alias_stats: dict[str, dict[str, int]], path: str = "") -> None:
    """Second pass over the (already-canonicalised) schema: for each property whose canonical key
    absorbed more than one original spelling, record them (`x-aliases`) and flag genuinely mixed usage
    (`x-ambiguity: "inconsistent_casing"`) when the minority spelling isn't just a rare typo."""
    if not isinstance(schema, dict):
        return
    for branch in schema.get("anyOf") or []:
        _annotate_aliases(branch, alias_stats, path)
    props = schema.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            field_path = f"{path}.{name}" if path else name
            counts = alias_stats.get(field_path)
            if counts and len(counts) > 1 and isinstance(sub, dict):
                spellings = sorted(counts.items(), key=lambda kv: -kv[1])
                sub["x-aliases"] = [s for s, _ in spellings]
                total = sum(counts.values())
                minority = total - spellings[0][1]
                if minority / total > ALIAS_MINORITY_RATIO:
                    _add_ambiguity(sub, "inconsistent_casing")
            _annotate_aliases(sub, alias_stats, field_path)
    items = schema.get("items")
    if isinstance(items, dict):
        _annotate_aliases(items, alias_stats, path)


def enrich_schema(schema: JSONSchema | None, samples: list[Any]) -> JSONSchema | None:
    """Add format / enum / observed-range / presence-graded required+nullable (in place) from the
    observed `samples`, and flag key-casing aliases absorbed by `infer_schema`'s canonicalisation."""
    if schema is None:
        return None
    values = [s for s in samples if s is not None]
    alias_stats: dict[str, dict[str, int]] = {}
    canon_values = [_canonicalize_keys(v, alias_stats) for v in values]
    _annotate(schema, canon_values)
    _annotate_aliases(schema, alias_stats)
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


# ---------------------------------------------------------------------------
# Version-mix detection (A5) — used when NO explicit version signal (path prefix or header) was found
# for the endpoint: two API versions sharing one path/method (e.g. `date_of_birth` renamed to
# `birth_date`, plus a new `emergency_contact` field) otherwise merge silently into one superset
# schema. `_name_tokens`/`_name_similarity` are also used by watcher.py's incremental rename detection
# (imported from here) so both modes agree on what counts as "the same field, renamed".
# ---------------------------------------------------------------------------

VERSION_MIX_MIN_CLUSTER_RATIO = 0.20  # each candidate cluster must cover at least this share of samples
VERSION_MIX_MAX_CLUSTERS = 4          # don't chase more than a handful of candidate clusters


def _name_tokens(name: str) -> set[str]:
    """"date_of_birth" -> {"date", "of", "birth"}; "birthDate" -> {"birth", "date"}."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return {t.lower() for t in re.split(r"[^A-Za-z0-9]+", spaced) if t}


def _name_similarity(a: str, b: str) -> float:
    ta, tb = _name_tokens(a), _name_tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _propose_renames(cluster_a: frozenset[str], cluster_b: frozenset[str], objects: list[dict]) -> list[list[str]]:
    """Greedy name+type matching between two clusters' exclusive fields (same heuristic as watcher.py's
    incremental rename pairing): a field in `cluster_a` pairs with one in `cluster_b` only if they share
    a name token and agree on observed type. Fields with no match (like a genuinely new field such as
    `emergency_contact`) are simply left unpaired."""
    def type_of(field: str) -> str | None:
        return next((_value_type(o[field]) for o in objects if o.get(field) is not None), None)

    cand = [(x, y) for x in cluster_a for y in cluster_b
            if _name_similarity(x, y) > 0 and type_of(x) == type_of(y) and type_of(x) is not None]
    cand.sort(key=lambda xy: -_name_similarity(*xy))
    used_a: set[str] = set()
    used_b: set[str] = set()
    renames: list[list[str]] = []
    for x, y in cand:
        if x in used_a or y in used_b:
            continue
        used_a.add(x)
        used_b.add(y)
        renames.append([x, y])
    return renames


def detect_version_mix(samples: list[Any]) -> dict[str, Any] | None:
    """Look for two-or-more field-set clusters within `samples` that never share a field and each cover
    a real share of the data — the "15 v1 + 15 v2 bodies merge into one schema" bug, when no explicit
    version signal (path prefix / header) was found for this endpoint. Fields present in every sample
    are the common core and don't count toward a cluster's signature. Returns None for one consistent
    shape, or {"kind": "possible_version_mix", "clusters": N, "renames": [[old, new], ...]}."""
    objects = [s for s in samples if isinstance(s, dict)]
    n = len(objects)
    if n == 0:
        return None
    key_sets = [set(o.keys()) for o in objects]
    always_present = set.intersection(*key_sets) if key_sets else set()
    reduced = [frozenset(ks - always_present) for ks in key_sets]
    counts: dict[frozenset, int] = {}
    for r in reduced:
        if r:
            counts[r] = counts.get(r, 0) + 1
    big = sorted((sig for sig, c in counts.items() if c / n >= VERSION_MIX_MIN_CLUSTER_RATIO),
                key=lambda sig: -counts[sig])[:VERSION_MIX_MAX_CLUSTERS]
    if len(big) < 2:
        return None
    for i in range(len(big)):  # a shared field between two "clusters" means this isn't a clean split
        for j in range(i + 1, len(big)):
            if big[i] & big[j]:
                return None
    return {
        "kind": "possible_version_mix",
        "clusters": len(big),
        "renames": _propose_renames(big[0], big[1], objects),
    }


_VERSION_HEADER_NAMES = {"accept-version", "x-api-version"}
_AUTH_HEADER_NAMES = {"authorization", "x-api-key", "cookie"}


def _header_version(entries: list[LogEntry]) -> str | None:
    """The majority Accept-Version/X-API-Version header value across `entries`, or None (A5's other
    explicit version signal, alongside a /v<N>/ path prefix)."""
    counts: dict[str, int] = {}
    for e in entries:
        for k, v in (e.get("headers") or {}).items():
            if k.lower() in _VERSION_HEADER_NAMES and isinstance(v, str) and v.strip():
                counts[v.strip()] = counts.get(v.strip(), 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None


def _auth_rate(entries: list[LogEntry]) -> float:
    """Fraction of `entries` carrying an Authorization/X-API-Key/Cookie header — observed evidence
    (not a guess) used to emit `security`/`securitySchemes` (spec_builder) and, opt-in, to justify a
    401/403 guess (errors.py's B2 rule matrix)."""
    if not entries:
        return 0.0
    n = sum(1 for e in entries if any(k.lower() in _AUTH_HEADER_NAMES for k in (e.get("headers") or {})))
    return n / len(entries)


def infer_endpoint(method: str, template: str, entries: list[LogEntry], llm: AidhClient | None = None) -> EndpointSchema:
    """Build the EndpointSchema for one endpoint.

    - request_schema: from request_body of entries with 2xx status only (4xx bodies are often invalid on purpose).
    - responses: one schema per distinct status code; None when every body for that status is null (e.g. 204).
    - examples: first non-null response_body per status.
    - body schemas get format / enum / minimum+maximum from the observed values (enrich_schema).
    - sample_count / first_seen / last_seen from the entries.
    - auth_rate: fraction of entries carrying an Authorization/X-API-Key/Cookie header (drives
      spec_builder's always-on `security`/`securitySchemes` emission and, opt-in, errors.py's 401/403 guess).
    - request_required: False if some 2xx request had no body at all despite a request_schema existing
      (drives spec_builder's `requestBody.required`, previously always hardcoded True).
    - api_version: an explicit /v<N>/ path prefix or Accept-Version/X-API-Version header (A5), else None.
      When there IS an explicit signal, request/response schemas are trusted as one consistent shape for
      that version and are NOT scanned for a version mix — the two "versions" here are actually just one,
      by construction. Only when there's no explicit signal do request/response schemas get scanned for
      an unflagged mix (detect_version_mix): two field-set clusters sharing one path/method with no
      signal to tell them apart is exactly the silent-merge bug A5 targets.
    - if `llm` is given, fields without an obvious rule-based meaning also get an LLM-written
      "description" (see llm_refine.refine_field_descriptions); `llm=None` (the default) leaves this
      step out entirely, so behaviour is unchanged for every caller that doesn't opt in.
    """
    request_samples = [e.get("request_body") for e in entries if 200 <= int(e["status"]) < 300]
    api_version = extract_path_version(template) or _header_version(entries)
    request_required = bool(request_samples) and all(s is not None for s in request_samples)

    by_status: dict[int, list[Any]] = {}
    for e in entries:
        by_status.setdefault(int(e["status"]), []).append(e.get("response_body"))

    responses: dict[int, JSONSchema | None] = {}
    examples: dict[int, Any] = {}
    for status in sorted(by_status):
        bodies = by_status[status]
        schema = enrich_schema(infer_schema(bodies), bodies)
        if schema is not None and api_version is None:
            mix = detect_version_mix(bodies)
            if mix:
                schema["x-ambiguity"] = mix
        responses[status] = schema
        example = next((b for b in bodies if b is not None), None)
        if example is not None:
            examples[status] = example

    timestamps = sorted(t for t in (e.get("timestamp") for e in entries) if t)

    request_schema = enrich_schema(infer_schema(request_samples), request_samples)
    if request_schema is not None and api_version is None:
        mix = detect_version_mix(request_samples)
        if mix:
            request_schema["x-ambiguity"] = mix
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
        api_version=api_version,
        auth_rate=_auth_rate(entries),
        request_required=request_required,
    )


def infer_all(grouped: dict[EndpointKey, list[LogEntry]], llm: AidhClient | None = None) -> list[EndpointSchema]:
    """infer_endpoint for every group, sorted by (path_template, method)."""
    return [
        infer_endpoint(method, template, entries, llm)
        for (method, template), entries in sorted(grouped.items(), key=lambda kv: (kv[0][1], kv[0][0]))
        if entries
    ]
