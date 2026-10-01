"""mock_proxy.py — B5: a thin stateful/validating/chaos layer in front of Prism.

Prism generates schema-valid dynamic data well, but only returns a non-2xx when the caller explicitly
asks for one (`Prefer: code=NNN`) — fine for deterministic tests, not "realistic" traffic for exercising
a frontend's error handling. This proxy sits in front of Prism on the port everything else (main.py, the
dashboard's "Try it") already points to — Prism itself moves to an internal port — and adds four things
a static mock can't:

1. Stateful 404s: tracks resource ids it has actually handed out (from a 2xx response body's "id"),
   per resource (path template with its trailing {param} stripped). A lookup by an id it has never seen
   for a resource it HAS learned some ids for returns the endpoint's real 404, instead of Prism
   happily generating a fake 200 for any id at all.
2. Real validation: request bodies are checked against the operation's request schema with
   `jsonschema`; on failure, the response is the endpoint's learned 400/422 envelope, naming the actual
   field that failed — not a random example.
3. Auth: an operation with `security` (spec_builder emits this when most of its traffic carried an
   auth header — see spec_builder.py) and a missing Authorization header on the incoming request gets
   the endpoint's 401.
4. Chaos: once a request passes all of the above, it rolls against each candidate error status's
   `x-observed-rate` (real traffic) or a small default for `x-inferred` statuses lacking one, and either
   serves that status's example directly or (most of the time) forwards to Prism untouched.
   `chaos_rate=0` disables this; header `X-Mock-Scenario: <status>` forces one status deterministically,
   skipping every other check — for a specific test, not a realistic simulation.

Everything the proxy serves — forwarded or synthesized — is logged in the same JSONL shape parser.py
expects, so mock traffic can feed back into the watcher (a nice closing demo beat: point `watch` at the
same log file the proxy writes to).
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from jsonschema import Draft7Validator

log = logging.getLogger(__name__)

SPEC_PATH = Path("output/openapi.yaml")
LOG_PATH = Path("live_logs.jsonl")
UPSTREAM_URL = "http://127.0.0.1:4011"  # Prism's real (internal) address — see main.py's wiring
DEFAULT_INFERRED_RATE = {"500": 0.01, "429": 0.005}  # x-inferred statuses with no x-observed-rate
CHAOS_RATE = 1.0  # multiplies every chaos roll's probability; 0 disables chaos entirely (--chaos 0)
STATIC_EXAMPLE_MIN_SAMPLES = 20  # below this many observed calls, 2xx bodies come from the recorded
                                 # example instead of Prism's generated data (see thin_success); 0 = never

app = FastAPI(title="Mock Proxy")
# A frontend served from another origin (the passport form on :8001, a dev server on :3000) must be
# able to call the mock directly — that is the whole point of it. Prism allows CORS by default; so do we.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["*"])

_state_lock = threading.Lock()
_known_ids: dict[str, set[str]] = {}          # resource prefix -> ids actually seen in a 2xx body
_spec_cache: dict[str, Any] = {"mtime": None, "spec": {}}

_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")


# ---------------------------------------------------------------------------
# Spec access (a small standalone reader — deliberately not shared with dashboard/app.py, so this
# module has no dependency on it and can run as its own process)
# ---------------------------------------------------------------------------

def load_spec() -> dict[str, Any]:
    """The current spec, re-read only when SPEC_PATH's mtime changes. {} if missing/unreadable."""
    try:
        mtime = SPEC_PATH.stat().st_mtime
    except OSError:
        return {}
    if _spec_cache["mtime"] != mtime:
        try:
            with SPEC_PATH.open("r", encoding="utf-8-sig") as f:
                _spec_cache["spec"] = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            log.warning("could not read spec %s: %s", SPEC_PATH, e)
            _spec_cache["spec"] = {}
        _spec_cache["mtime"] = mtime
    return _spec_cache["spec"]


def _template_regex(template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^{}]+\})", template)
    return re.compile("^" + "".join(r"(?P<p%d>[^/]+)" % i if p.startswith("{") else re.escape(p)
                                    for i, p in enumerate(parts)) + "$")


def match_operation(spec: dict[str, Any], method: str, path: str) -> tuple[str, dict[str, Any], dict[str, str]] | None:
    """(template, operation, path_params) for the path template matching `method`+`path`, or None."""
    for template, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        op = item.get(method.lower())
        if not isinstance(op, dict):
            continue
        m = _template_regex(template).match(path)
        if m:
            return template, op, m.groupdict()
    return None


def resource_prefix(template: str) -> str:
    """"/users/{id}" -> "/users"; "/users" -> "/users" (a collection endpoint is its own resource)."""
    return _PLACEHOLDER_RE.sub("", template).rstrip("/") or "/"


def response_content(op: dict[str, Any], status: Any) -> dict[str, Any]:
    resp = (op.get("responses") or {}).get(str(status))
    return resp if isinstance(resp, dict) else {}


def response_example(op: dict[str, Any], status: Any) -> Any:
    return ((response_content(op, status).get("content") or {}).get("application/json") or {}).get("example")


def request_schema(op: dict[str, Any]) -> dict[str, Any] | None:
    schema = ((op.get("requestBody") or {}).get("content") or {}).get("application/json", {}).get("schema")
    return schema if isinstance(schema, dict) else None


# ---------------------------------------------------------------------------
# State: which ids have actually been handed out, per resource
# ---------------------------------------------------------------------------

def record_ids(prefix: str, body: Any) -> None:
    if isinstance(body, dict) and isinstance(body.get("id"), (str, int)) and not isinstance(body.get("id"), bool):
        with _state_lock:
            _known_ids.setdefault(prefix, set()).add(str(body["id"]))


def is_unknown_id(prefix: str, value: str) -> bool:
    """True only if we've learned SOME ids for this resource and `value` isn't one of them — an empty/
    unseen resource means we can't judge realism yet, so it's treated as "not unknown" (bootstrap)."""
    with _state_lock:
        known = _known_ids.get(prefix)
    return bool(known) and value not in known


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validation_failure(op: dict[str, Any], body: Any) -> dict[str, str] | None:
    """{"field", "message"} for the first schema violation in `body`, or None if it validates (or
    there's no request schema to check against)."""
    schema = request_schema(op)
    if schema is None:
        return None
    error = next(iter(Draft7Validator(schema).iter_errors(body)), None)
    if error is None:
        return None
    if error.path:
        field = ".".join(str(p) for p in error.path)
    else:
        m = re.search(r"'([^']+)'", error.message)
        field = m.group(1) if m else "body"
    return {"field": field, "message": error.message}


def error_body(op: dict[str, Any], status: int, fallback_message: str) -> Any:
    """The endpoint's own example for `status` if the spec has one (observed or x-inferred); otherwise
    a minimal generic envelope, so the proxy never crashes just because a status was never learned.
    For 401/403/404/scenario-forced/chaos responses this static example IS the point (a real-looking
    "not found"/"unauthorized" body) — but never call this for a validation failure, where the whole
    point is to say what's wrong with THIS request; use validation_error_body instead."""
    example = response_example(op, status)
    if example is not None:
        return example
    return {"error": "validation_error" if status in (400, 422) else "error", "message": fallback_message}


def validation_error_body(op: dict[str, Any], status: int, failure: dict[str, str]) -> Any:
    """Unlike error_body, this always reflects the ACTUAL failing field/message for this request — a
    static spec example would silently hide which field failed and just repeat the same canned body
    for every failure, defeating the point of validating at all. Borrows the envelope's own field names
    (message/detail/title) when the spec has an example to match its shape; otherwise a generic one."""
    example = response_example(op, status)
    message = f"'{failure['field']}': {failure['message']}"
    if isinstance(example, dict):
        for key in ("message", "detail", "title"):
            if key in example:
                return {**example, key: message}
    return {"error": "validation_error", "message": message, "field": failure["field"]}


# ---------------------------------------------------------------------------
# Chaos
# ---------------------------------------------------------------------------

def _chaos_rate(resp: dict[str, Any]) -> float:
    if isinstance(resp.get("x-observed-rate"), (int, float)):
        return float(resp["x-observed-rate"])
    return 0.0  # an x-inferred status uses DEFAULT_INFERRED_RATE by status code instead (see roll_chaos)


def roll_chaos(op: dict[str, Any]) -> int | None:
    """A status to synthesize instead of forwarding to Prism, chosen at random by each candidate
    status's real or default rate (scaled by CHAOS_RATE) — or None to forward normally."""
    if CHAOS_RATE <= 0:
        return None
    for status_str, resp in (op.get("responses") or {}).items():
        if not isinstance(resp, dict) or status_str in ("200", "201", "202", "204"):
            continue
        rate = _chaos_rate(resp) if not resp.get("x-inferred") else DEFAULT_INFERRED_RATE.get(status_str, 0.0)
        if rate > 0 and random.random() < rate * CHAOS_RATE:
            return int(status_str)
    return None


# ---------------------------------------------------------------------------
# Traffic logging (same JSONL shape parser.py reads)
# ---------------------------------------------------------------------------

def thin_success(op: dict[str, Any], path_params: dict[str, str]) -> tuple[int, Any] | None:
    """(status, body) to serve from the recorded example when the operation rests on fewer than
    STATIC_EXAMPLE_MIN_SAMPLES observed calls, else None (forward to Prism). Picks the lowest 2xx that
    has an example; if the body carries an "id" and the request addressed one, the example's id is
    swapped for the requested one so `GET /applications/PA-000026` answers about PA-000026."""
    if STATIC_EXAMPLE_MIN_SAMPLES <= 0:
        return None
    try:
        samples = int(op.get("x-sample-count", 0))
    except (TypeError, ValueError):
        samples = 0
    if samples >= STATIC_EXAMPLE_MIN_SAMPLES:
        return None
    for status in sorted(op.get("responses") or {}, key=lambda s: str(s)):
        if not str(status).startswith("2"):
            continue
        example = response_example(op, status)
        if example is None:
            continue
        body = json.loads(json.dumps(example))  # deep copy; never mutate the cached spec
        if isinstance(body, dict) and "id" in body and path_params:
            body["id"] = list(path_params.values())[-1]
        return int(status), body
    return None


def log_traffic(method: str, path: str, query: dict[str, str], status: int, req_body: Any, resp_body: Any,
                headers: dict[str, str]) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "method": method.upper(), "path": path, "query": query, "request_body": req_body,
        "status": status, "response_body": resp_body, "headers": headers,
    }
    try:
        with _state_lock, LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("could not append mock traffic to %s: %s", LOG_PATH, e)


def _json_response(status: int, body: Any) -> Response:
    return Response(content=json.dumps(body), status_code=status, media_type="application/json")


# ---------------------------------------------------------------------------
# The proxy route — one catch-all, since routing is spec-driven, not FastAPI-path-driven
# ---------------------------------------------------------------------------

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request) -> Response:
    full_path = "/" + path
    method = request.method
    query = dict(request.query_params)
    raw = await request.body()
    req_body: Any = None
    if raw:
        try:
            req_body = json.loads(raw)
        except ValueError:
            req_body = None
    headers = {k: v for k, v in request.headers.items()}

    spec = load_spec()
    match = match_operation(spec, method, full_path)
    if match is None:
        resp = {"error": "not_found", "message": f"no mock route for {method} {full_path}"}
        log_traffic(method, full_path, query, 404, req_body, resp, headers)
        return _json_response(404, resp)
    template, op, path_params = match
    prefix = resource_prefix(template)

    scenario = request.headers.get("x-mock-scenario")
    if scenario and scenario.isdigit() and str(int(scenario)) in (op.get("responses") or {}):
        status = int(scenario)
        body = error_body(op, status, f"forced by X-Mock-Scenario: {status}")
        log_traffic(method, full_path, query, status, req_body, body, headers)
        return _json_response(status, body)

    if op.get("security") and not request.headers.get("authorization"):
        body = error_body(op, 401, "missing or invalid Authorization header")
        log_traffic(method, full_path, query, 401, req_body, body, headers)
        return _json_response(401, body)

    if req_body is not None or request_schema(op) is not None:
        failure = validation_failure(op, req_body)
        if failure:
            status = 422 if "422" in (op.get("responses") or {}) else 400
            body = validation_error_body(op, status, failure)
            log_traffic(method, full_path, query, status, req_body, body, headers)
            return _json_response(status, body)

    if path_params:
        value = list(path_params.values())[-1]
        if is_unknown_id(prefix, value):
            body = error_body(op, 404, f"no such resource: {value}")
            log_traffic(method, full_path, query, 404, req_body, body, headers)
            return _json_response(404, body)

    chaos_status = roll_chaos(op)
    if chaos_status is not None:
        body = error_body(op, chaos_status, f"chaos-injected {chaos_status}")
        log_traffic(method, full_path, query, chaos_status, req_body, body, headers)
        return _json_response(chaos_status, body)

    # Thin evidence: serve the recorded example rather than Prism's generated data. With one or two
    # observed samples the schema has no enums yet, so Prism (-d) would fill `status`/`full_name` with
    # lorem ipsum — the real body seen in the logs is a far better stand-in until the evidence grows.
    thin = thin_success(op, path_params)
    if thin is not None:
        status, body = thin
        record_ids(prefix, body)
        log_traffic(method, full_path, query, status, req_body, body, headers)
        return _json_response(status, body)

    # Normal path: forward to Prism untouched.
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            upstream = await client.request(method, UPSTREAM_URL + full_path, params=query,
                                            content=raw or None,
                                            headers={k: v for k, v in headers.items() if k.lower() != "host"})
    except httpx.HTTPError as e:
        body = {"error": "upstream_unavailable", "message": str(e)}
        log_traffic(method, full_path, query, 502, req_body, body, headers)
        return _json_response(502, body)

    resp_body: Any = None
    try:
        resp_body = upstream.json()
    except ValueError:
        pass
    record_ids(prefix, resp_body)
    log_traffic(method, full_path, query, upstream.status_code, req_body, resp_body, headers)
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"))
