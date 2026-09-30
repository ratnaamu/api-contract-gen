"""llm_refine.py — optional LLM-based refinement of inferred schemas and operation summaries.   Owner: [Name 2]

Uses the AIDH gateway (Unisys-hosted local LLMs, Ollama-compatible `POST {base_url}/api/generate`) to
add human-readable descriptions the rule-based inferrer/spec_builder cannot produce on their own: what
a field or endpoint *means*, not just its shape.

Entirely optional and additive:
  - every entry point (inferrer.infer_all/infer_endpoint, spec_builder.build_spec/build_operation) takes
    an `llm: AidhClient | None = None` parameter; None (the default, used by every existing caller and
    test) means byte-identical behaviour to before this module existed;
  - never raises: any network, timeout or bad-JSON failure just logs a warning and leaves the rule-based
    schema/summary untouched, so a flaky or misconfigured LLM can never break spec generation;
  - responses are cached in memory per AidhClient instance, so a `watch` session doesn't re-ask the same
    question for an endpoint that hasn't changed since the last rebuild.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from models import EndpointSchema, JSONSchema, JSONValue

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0
MAX_FIELDS_PER_CALL = 40  # bounds prompt size/latency; fields beyond this are simply left undescribed
MAX_DESCRIPTION_LEN = 300
MAX_SUMMARY_LEN = 120
MAX_OP_DESCRIPTION_LEN = 500

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

@dataclass
class AidhClient:
    """Thin client for the AIDH gateway: POST {base_url}/api/generate (Ollama's non-chat, single-prompt
    endpoint). AIDH_DOMAIN_ID is the credential, not a routing hint: it's sent as
    `Authorization: Bearer {domain_id}:UNISYS`, not in the request body."""

    base_url: str
    domain_id: str
    model: str
    timeout: float = DEFAULT_TIMEOUT
    _cache: dict[str, str | None] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_env(cls) -> "AidhClient | None":
        """Build a client from AIDH_BASE_URL / AIDH_DOMAIN_ID / AIDH_MODEL. None if any is unset."""
        base_url = os.environ.get("AIDH_BASE_URL", "").strip().rstrip("/")
        domain_id = os.environ.get("AIDH_DOMAIN_ID", "").strip()
        model = os.environ.get("AIDH_MODEL", "").strip()
        if not (base_url and domain_id and model):
            return None
        return cls(base_url=base_url, domain_id=domain_id, model=model)

    def chat(self, system: str, user: str) -> str | None:
        """One completion; returns the model's text, or None on any failure (never raises). `system`
        and `user` are joined into a single prompt: /api/generate has no separate system-role concept."""
        key = f"{system}\x00{user}"
        if key in self._cache:
            return self._cache[key]
        text = self._call(system, user)
        self._cache[key] = text
        return text

    def _call(self, system: str, user: str) -> str | None:
        try:
            import requests
        except ImportError:
            log.warning("AIDH refinement skipped: the `requests` package is not installed")
            return None
        payload = {
            "model": self.model,
            "prompt": f"{system}\n\n{user}",
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.domain_id}:UNISYS",
            "Content-Type": "application/json",
        }
        try:
            r = requests.post(f"{self.base_url}/api/generate", json=payload, headers=headers, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            text = data.get("response")
            return text.strip() if isinstance(text, str) and text.strip() else None
        except Exception as e:  # noqa: BLE001 — a refinement failure must never break spec generation
            log.warning("AIDH call failed: %s: %s", type(e).__name__, e)
            return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object out of an LLM reply, tolerating surrounding prose or ```json fences."""
    for candidate in (text, (m.group(0) if (m := _JSON_OBJECT_RE.search(text)) else None)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


# ---------------------------------------------------------------------------
# Field-level description refinement (used by inferrer.py)
# ---------------------------------------------------------------------------

def _type_of(schema: JSONSchema) -> str:
    t = schema.get("type")
    if isinstance(t, list):
        return " | ".join(str(x) for x in t)
    return str(t) if t else ("anyOf" if "anyOf" in schema else "object")


def _normalize_field_key(key: str) -> str:
    """Canonicalize a field path for tolerant matching: strip a leading "/" or "." (some models key
    their reply "/id" instead of "id") and treat "/" as the same nesting separator as "."
    ("address/city" == "address.city"). "[]" (our marker for array items) is left as-is."""
    return key.strip().lstrip("/.").replace("/", ".")


def _collect_undescribed_fields(
    schema: Any, samples: list[JSONValue], prefix: str, out: list[dict[str, Any]],
) -> None:
    """Walk `schema`, collecting {path, schema, example} for properties without a description yet.
    `schema` entries are the actual nested dicts (not copies), so a caller can set ["description"]
    on them directly once the LLM has answered. Stops once MAX_FIELDS_PER_CALL are collected."""
    if not isinstance(schema, dict) or len(out) >= MAX_FIELDS_PER_CALL:
        return
    props = schema.get("properties")
    if isinstance(props, dict):
        objects = [s for s in samples if isinstance(s, dict)]
        example_obj = objects[0] if objects else None
        for name, sub in props.items():
            if len(out) >= MAX_FIELDS_PER_CALL:
                return
            if not isinstance(sub, dict):
                continue
            path = f"{prefix}.{name}" if prefix else name
            if not sub.get("description"):
                example = example_obj.get(name) if isinstance(example_obj, dict) else None
                out.append({"path": path, "schema": sub, "example": example})
            sub_samples = [o.get(name) for o in objects if isinstance(o.get(name), (dict, list))]
            _collect_undescribed_fields(sub, sub_samples, path, out)
    items = schema.get("items")
    if isinstance(items, dict):
        arr_values = [x for s in samples if isinstance(s, list) for x in s]
        _collect_undescribed_fields(items, arr_values, f"{prefix}[]" if prefix else "[]", out)
    for branch in schema.get("anyOf") or []:
        _collect_undescribed_fields(branch, samples, prefix, out)


def refine_field_descriptions(
    schema: JSONSchema | None, samples: list[JSONValue], context: str, llm: AidhClient | None,
) -> None:
    """Add a one-line "description" (in place) to properties of `schema` that don't have one, inferred
    from field name + type + an observed example. No-op if `llm` is None or nothing needs describing."""
    if llm is None or not isinstance(schema, dict):
        return
    fields: list[dict[str, Any]] = []
    _collect_undescribed_fields(schema, [s for s in samples if s is not None], "", fields)
    if not fields:
        return

    lines = []
    for f in fields:
        example = f["example"]
        example_txt = f", example: {json.dumps(example, ensure_ascii=False)[:120]}" if example is not None else ""
        lines.append(f"- {f['path']} ({_type_of(f['schema'])}){example_txt}")

    system = (
        "You write short, factual, one-line descriptions for fields of a JSON API, inferred only from "
        "each field's name, type and an example value. Never invent business meaning the name/example "
        "doesn't support; if genuinely unclear, describe it generically (e.g. \"internal identifier\"). "
        "Reply with ONLY a JSON object mapping each field path to its description string, nothing else."
    )
    user = f"Context: {context}\nFields:\n" + "\n".join(lines)
    text = llm.chat(system, user)
    if not text:
        return
    mapping = _extract_json_object(text)
    if not isinstance(mapping, dict):
        log.warning("AIDH reply for %s was not a JSON object, ignoring: %.200r", context, text)
        return
    # Match tolerantly: models sometimes key their reply "/id" or "address/city" instead of the
    # "id" / "address.city" we asked for, so look up by a normalized key if the exact one misses.
    by_normalized = {_normalize_field_key(k): v for k, v in mapping.items() if isinstance(k, str)}
    applied = 0
    for f in fields:
        desc = mapping.get(f["path"])
        if desc is None:
            desc = by_normalized.get(_normalize_field_key(f["path"]))
        if isinstance(desc, str) and desc.strip():
            f["schema"]["description"] = desc.strip()[:MAX_DESCRIPTION_LEN]
            applied += 1
    if applied:
        log.info("AIDH: described %d/%d field(s) for %s", applied, len(fields), context)


# ---------------------------------------------------------------------------
# Operation-level summary/description refinement (used by spec_builder.py)
# ---------------------------------------------------------------------------

def refine_operation_summary(ep: EndpointSchema, llm: AidhClient | None) -> tuple[str, str] | None:
    """Ask the LLM for a short (summary, description) pair for one endpoint, inferred from its
    method/path and an example response. Returns None on any failure, missing config, or a reply
    that doesn't parse — callers should keep the rule-based summary in that case."""
    if llm is None:
        return None
    example = next(iter(ep.examples.values()), None)
    example_txt = json.dumps(example, ensure_ascii=False)[:800] if example is not None else "(none observed)"
    system = (
        "You write concise OpenAPI operation summaries and descriptions for a REST endpoint, inferred "
        "only from its HTTP method, path and an example response body. Reply with ONLY a JSON object: "
        '{"summary": "<=8 words, no trailing period", "description": "one plain sentence"}.'
    )
    user = f"{ep.method} {ep.path_template}\nExample response: {example_txt}"
    text = llm.chat(system, user)
    if not text:
        return None
    data = _extract_json_object(text)
    if not isinstance(data, dict):
        log.warning("AIDH reply for %s %s was not a JSON object, ignoring: %.200r",
                    ep.method, ep.path_template, text)
        return None
    summary, description = data.get("summary"), data.get("description")
    if isinstance(summary, str) and summary.strip() and isinstance(description, str) and description.strip():
        log.info("AIDH: refined summary for %s %s: %r", ep.method, ep.path_template, summary.strip())
        return summary.strip()[:MAX_SUMMARY_LEN], description.strip()[:MAX_OP_DESCRIPTION_LEN]
    log.warning("AIDH reply for %s %s missing summary/description, ignoring: %.200r",
                ep.method, ep.path_template, text)
    return None


# ---------------------------------------------------------------------------
# B4: LLM error-status suggestions — a bonus on top of errors.py's rule-based B2 matrix, never a
# replacement. Kept in this module (not errors.py, which stays pure/rule-based with zero LLM
# dependency) so the two can be tested and reasoned about independently; the merge happens where both
# are already available together, in watcher.build_from_entries.
# ---------------------------------------------------------------------------

ALLOWED_LLM_STATUSES = (400, 401, 403, 404, 405, 409, 422, 429, 500, 502, 503)
MAX_LLM_STATUS_SUGGESTIONS = 3


def suggest_error_statuses(
    method: str, path_template: str, has_request_body: bool, observed_statuses: set[int],
    auth_rate: float, llm: AidhClient | None,
) -> list[dict[str, Any]]:
    """Ask the LLM for up to MAX_LLM_STATUS_SUGGESTIONS additional plausible status codes for one
    endpoint, beyond whatever errors.py's rule matrix and the real traffic already cover. Returns [] on
    any failure, missing config, or a reply that doesn't parse — this is additive by construction (the
    caller merges these in without ever overriding a rule-based or observed entry), so "no suggestions"
    is a completely safe, silent fallback. Each item is {"status": int, "reason": str}."""
    if llm is None:
        return []
    allowed_txt = ", ".join(str(s) for s in ALLOWED_LLM_STATUSES)
    system = (
        "You suggest additional plausible HTTP error status codes for a REST endpoint that aren't "
        f"already known for it, choosing ONLY from this fixed set: {allowed_txt}. Reply with ONLY a "
        'JSON object: {"suggestions": [{"status": <code from the set>, "reason": "<one short line>"}, '
        f"...]}}, at most {MAX_LLM_STATUS_SUGGESTIONS} entries. Never suggest a status already known."
    )
    user = (f"{method} {path_template}\nAlready known statuses: {sorted(observed_statuses)}\n"
           f"Has a request body: {has_request_body}\n"
           f"Auth header seen on {round(auth_rate * 100)}% of requests")
    text = llm.chat(system, user)
    if not text:
        return []
    data = _extract_json_object(text)
    suggestions = data.get("suggestions") if isinstance(data, dict) else None
    if not isinstance(suggestions, list):
        log.warning("AIDH status-suggestion reply for %s %s was not a JSON object, ignoring: %.200r",
                    method, path_template, text)
        return []
    seen = set(observed_statuses)
    out: list[dict[str, Any]] = []
    for item in suggestions:
        if not isinstance(item, dict) or len(out) >= MAX_LLM_STATUS_SUGGESTIONS:
            continue
        try:
            status = int(item.get("status"))
        except (TypeError, ValueError):
            continue
        reason = item.get("reason")
        if status not in ALLOWED_LLM_STATUSES or status in seen or not isinstance(reason, str) or not reason.strip():
            continue
        seen.add(status)
        out.append({"status": status, "reason": reason.strip()[:200]})
    if out:
        log.info("AIDH: suggested %d extra status(es) for %s %s: %s",
                 len(out), method, path_template, [o["status"] for o in out])
    return out
