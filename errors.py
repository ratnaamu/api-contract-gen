"""errors.py — B1/B2/B3: learn the API's error envelope, infer plausible error statuses the traffic
never happened to show, and mine observed 400/422 bodies for validation-rule evidence.

Contract:
    list[EndpointSchema], dict[EndpointKey, list[LogEntry]]  ->  dict[EndpointKey, dict[int, InferredResponse]]
Pure functions over the same inferrer.py output and grouped entries the rest of the pipeline already
has — no new traffic-reading of its own. Merged into the spec by spec_builder.build_operation's
`inferred` parameter, each tagged `x-inferred: true` so a reader (and the dashboard, and diff_specs) can
tell an observed status from a guess at a glance. Opt-in (see main.py's --infer-errors): unlike the
presence/enum/version work in inferrer.py, a *guessed* status is a claim about traffic that was never
observed, so it's kept out of the default build and out of breaking-change detection entirely
(watcher.diff_specs skips anything x-inferred) rather than risking noise in either.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from inferrer import enrich_schema, infer_schema
from models import EndpointKey, EndpointSchema, JSONSchema, LogEntry

# RFC 9457 Problem Details — the fallback error envelope when nothing error-shaped was ever observed
# anywhere in the traffic, so there's at least a standard shape to offer instead of guessing one.
PROBLEM_DETAILS_SCHEMA: JSONSchema = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "format": "uri"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
    },
    "required": ["title", "status"],
}

_AUTH_HEADER_NAMES = {"authorization", "x-api-key", "cookie"}
_UNIQUENESS_FIELD_NAMES = {"email", "sku", "username"}
_STATE_FIELD_NAMES = {"status", "state"}
AUTH_EVIDENCE_MIN_RATE = 0.5  # B2: "Authorization ... on >= 50% of requests" gates the 401/403 rule


@dataclass
class InferredResponse:
    """One synthesized (never observed) response for a status this endpoint's traffic never showed."""
    schema: JSONSchema
    evidence: str                 # x-evidence: why this status is plausible, e.g. "resource lookup by id"
    confidence: str                # x-confidence: "medium" if the envelope itself was observed, else "low"
    example: Any = None            # a plausible instance of `schema`, if one could be synthesized
    inferred_by: str = "rules"     # x-inferred-by: "rules" (this module's B2 matrix) or "llm" (B4,
                                    # llm_refine.suggest_error_statuses — merged in by watcher.build_from_entries)


@dataclass
class EndpointEvidence:
    """Per-endpoint signals the B2 rule matrix reads. Computed from the grouped raw entries (auth rate,
    sibling methods) plus the already-inferred schema (path param, request body, response shape) —
    that's why this needs both, rather than living entirely on EndpointSchema."""
    has_path_param: bool
    has_request_body: bool
    auth_rate: float
    is_state_mutation: bool     # PATCH/PUT whose response carries a status/state-looking field
    sibling_methods: set[str]   # other HTTP methods observed on the same literal path template
    has_uniqueness_field: bool  # POST with a required email/sku/username-looking field


@dataclass
class ApiEvidence:
    """Signals that apply across the whole API, not just one endpoint."""
    uses_422: bool   # some endpoint, anywhere, returned 422 -> prefer it over 400 for validation errors
    uses_409: bool   # some endpoint, anywhere, returned 409 -> this API models state conflicts
    rate_limited: bool  # a 429 was observed anywhere. (B2 also lists X-RateLimit-*/Retry-After response
                        # headers as evidence, but LogEntry only captures REQUEST headers — parser.py's
                        # FIELD_ALIASES comment notes response headers are accepted on input but not
                        # kept — so an observed 429 is the only signal available here.)


def _is_auth_header(name: str) -> bool:
    return name.lower() in _AUTH_HEADER_NAMES


def learn_error_envelope(all_entries: list[LogEntry]) -> tuple[JSONSchema, bool]:
    """One schema inferred from every observed 4xx/5xx response body across ALL endpoints (most real
    APIs have one error shape: {error, message}, FastAPI's {detail}, Google-style {code, message,
    details}, ...). Returns (schema, observed) — observed=False means nothing error-shaped was ever
    logged anywhere, so `schema` is the RFC 9457 Problem Details fallback instead of a real inference."""
    bodies = [e.get("response_body") for e in all_entries
             if isinstance(e.get("status"), int) and e["status"] >= 400 and e.get("response_body") is not None]
    schema = enrich_schema(infer_schema(bodies), bodies) if bodies else None
    if schema is None:
        return dict(PROBLEM_DETAILS_SCHEMA), False
    return schema, True


def collect_api_evidence(all_entries: list[LogEntry]) -> ApiEvidence:
    statuses = {int(e["status"]) for e in all_entries if isinstance(e.get("status"), int)}
    return ApiEvidence(uses_422=422 in statuses, uses_409=409 in statuses, rate_limited=429 in statuses)


def _response_has_state_field(ep: EndpointSchema) -> bool:
    return any(isinstance(s, dict) and _STATE_FIELD_NAMES & set(s.get("properties") or {})
              for s in ep.responses.values())


def _has_uniqueness_field(ep: EndpointSchema) -> bool:
    if ep.method != "POST" or not ep.request_schema:
        return False
    return bool(_UNIQUENESS_FIELD_NAMES & set(ep.request_schema.get("required") or []))


def collect_evidence(
    endpoints: list[EndpointSchema], grouped: dict[EndpointKey, list[LogEntry]],
) -> dict[EndpointKey, EndpointEvidence]:
    """Per-endpoint evidence for the rule matrix, one entry per endpoint in `endpoints`."""
    by_path: dict[str, set[str]] = {}
    for method, template in grouped:
        by_path.setdefault(template, set()).add(method)

    out: dict[EndpointKey, EndpointEvidence] = {}
    for ep in endpoints:
        key = (ep.method, ep.path_template)
        entries = grouped.get(key, [])
        n = len(entries)
        auth_n = sum(1 for e in entries if any(_is_auth_header(k) for k in (e.get("headers") or {})))
        out[key] = EndpointEvidence(
            has_path_param="{" in ep.path_template,
            has_request_body=ep.request_schema is not None,
            auth_rate=(auth_n / n) if n else 0.0,
            is_state_mutation=ep.method in ("PATCH", "PUT") and _response_has_state_field(ep),
            sibling_methods=by_path.get(ep.path_template, set()) - {ep.method},
            has_uniqueness_field=_has_uniqueness_field(ep),
        )
    return out


def mine_validation_evidence(ep: EndpointSchema, entries: list[LogEntry]) -> list[dict[str, Any]]:
    """B3: for each observed 400/422 request, which (currently-required) field was missing from it —
    the evidence a validation rule actually exists, used to write a plausible 400/422 example for an
    endpoint that never happened to log one. [] if there's no request schema or no failures to learn from."""
    if not ep.request_schema:
        return []
    required = set(ep.request_schema.get("required") or [])
    findings = []
    for e in entries:
        if e.get("status") not in (400, 422):
            continue
        body = e.get("request_body")
        if not isinstance(body, dict):
            continue
        for field in required - set(body):
            findings.append({"field": field, "issue": "required", "status": e["status"]})
    return findings


def _synthetic_example(envelope: JSONSchema, message: str) -> dict[str, Any]:
    """A plausible instance of the (observed or Problem-Details-fallback) error envelope, with its
    message-shaped field (message/detail/title — whichever the envelope actually has) set to `message`."""
    example: dict[str, Any] = {}
    for name, sub in (envelope.get("properties") or {}).items():
        if not isinstance(sub, dict):
            continue
        if name in ("message", "detail", "title") and "string" in (sub.get("type") or ["string"]):
            example[name] = message
        elif sub.get("type") == "integer":
            example[name] = 400
        elif sub.get("type") == "string":
            example[name] = (sub.get("enum") or ["error"])[0]
        elif sub.get("type") == "boolean":
            example[name] = False
        elif sub.get("type") == "array":
            example[name] = []
        elif sub.get("type") == "object":
            example[name] = {}
    return example


def infer_error_responses(
    ep: EndpointSchema, evidence: EndpointEvidence, api: ApiEvidence,
    envelope: JSONSchema, envelope_observed: bool, mined: list[dict[str, Any]],
) -> dict[int, InferredResponse]:
    """The B2 rule matrix: statuses this endpoint plausibly returns but its own traffic never showed,
    each with the evidence that justified it. Never overrides an already-observed status — the caller
    (spec_builder.build_operation) only uses an inferred entry where `ep.responses` has none."""
    out: dict[int, InferredResponse] = {}
    base_confidence = "medium" if envelope_observed else "low"

    def add(status: int, reason: str, confidence: str = base_confidence, example: Any = None) -> None:
        if status not in ep.responses and status not in out:
            out[status] = InferredResponse(schema=envelope, evidence=reason, confidence=confidence, example=example)

    if evidence.has_path_param:
        add(404, "resource lookup by id")
    if evidence.has_request_body:
        status = 422 if api.uses_422 else 400
        missing = mined[0]["field"] if mined else None
        example = _synthetic_example(envelope, f"'{missing}' is required") if missing else None
        add(status, "request body validation", example=example)
    if evidence.auth_rate >= AUTH_EVIDENCE_MIN_RATE:
        add(401, "authenticated endpoint")
        add(403, "authenticated endpoint")
    if evidence.is_state_mutation or api.uses_409:
        add(409, "state transition")
    if evidence.has_uniqueness_field:
        add(409, "duplicate resource")
    if api.rate_limited:
        add(429, "rate-limited API")
    if evidence.sibling_methods:
        add(405, "method not allowed", confidence="low")
    add(500, "generic server error", confidence="low")
    return out
