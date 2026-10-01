"""watcher.py — continuous mode: watch the log file, re-infer, diff the spec, restart Prism.   Owner: [Name 3]

Contract:
    log file changes  ->  output/openapi.yaml (rewritten)  +  output/changes.jsonl (appended SpecChange rows)
The dashboard only reads those two files, so dashboard and watcher can be built/run independently.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from errors import InferredResponse, collect_api_evidence, collect_evidence, infer_error_responses, learn_error_envelope, mine_validation_evidence
from inferrer import _name_similarity, infer_all  # name-similarity now shared with inferrer's A5 rename matching
from llm_refine import AidhClient, suggest_error_statuses
from models import LogEntry, SpecChange
from normalizer import group_by_endpoint, normalize_path
from parser import read_new_logs
from quality import build_quality_report
from spec_builder import build_spec, validate_spec, write_spec

DEFAULT_SPEC_PATH = Path("output/openapi.yaml")
DEFAULT_CHANGES_PATH = Path("output/changes.jsonl")
DEFAULT_PRISM_PORT = 4010

# An endpoint + status code is "warming up" until it has this many samples. Everything seen while
# warming up is part of its baseline: no field_added and no became_required/became_optional is reported.
# (Early samples can all happen to include a field that is really optional.) Request bodies use the
# endpoint's 2xx count, query parameters its total count.
MIN_SAMPLES_FOR_REQUIRED = 10
# A response field that was required (after warm-up) is reported as removed (BREAKING) once it has been
# missing from this many responses in a row. Occasional absences only show up as non-breaking
# became_optional. Nested fields only count responses where their parent object is present.
REMOVAL_STREAK = 10
# Request bodies get the same traffic-based evidence (successful 2xx requests only). A formerly required
# request field missing from REMOVAL_STREAK successful requests in a row is field_removed (BREAKING).
# A request field that first appears *after* warm-up is reported once the evidence is in:
#  - present in every successful request since it appeared, NEW_REQUIRED_STREAK times -> field_added,
#    BREAKING "new required field" (clients that don't send it are now rejected);
#  - missing from a successful request after it appeared -> field_added, ok "new optional field".
# New response fields are additive: field_added, ok, as soon as they are seen.
NEW_REQUIRED_STREAK = 10
# Rename detection: a (formerly required) field that goes missing while a new field of the same type
# appears under the same parent at the same time (first seen within RENAME_WINDOW samples of the old
# field's first absence, and present ever since) is one BREAKING field_renamed once the old field has been
# missing REMOVAL_STREAK times, instead of field_removed + field_added. If several fields change at once,
# pairs must share a word in their names (date_of_birth ~ birth_date); a lone candidate pair needs none.
RENAME_WINDOW = 2
_REQUEST = "request"  # stats scope for request bodies, next to the status codes of responses

_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
_IS_WINDOWS = os.name == "nt"


def _say(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def build_from_entries(entries: list[LogEntry], llm: AidhClient | None = None, infer_errors: bool = False) -> dict[str, Any]:
    """Full pipeline on in-memory entries: normalizer.group_by_endpoint -> inferrer.infer_all -> spec_builder.build_spec.
    `llm=None` and `infer_errors=False` (the defaults) match every existing caller/test exactly; pass an
    AidhClient for LLM-written descriptions (llm_refine.py), or infer_errors=True to also guess plausible
    error statuses this traffic never happened to show (errors.py; --infer-errors).

    When BOTH are given, each endpoint also gets B4's LLM status suggestions (llm_refine.
    suggest_error_statuses) merged in on top of errors.py's rule-based guesses — additive only: an LLM
    suggestion never overrides a rule-based or observed status, just fills in anything neither caught,
    tagged x-inferred-by: "llm" so it's visibly distinct from the rule matrix's "rules"."""
    grouped = group_by_endpoint(entries)
    endpoints = infer_all(grouped, llm)
    inferred = None
    if infer_errors:
        envelope, envelope_observed = learn_error_envelope(entries)
        api_evidence = collect_api_evidence(entries)
        evidence = collect_evidence(endpoints, grouped)
        inferred = {
            (ep.method, ep.path_template): infer_error_responses(
                ep, evidence[(ep.method, ep.path_template)], api_evidence, envelope, envelope_observed,
                mine_validation_evidence(ep, grouped[(ep.method, ep.path_template)]),
            )
            for ep in endpoints
        }
        if llm is not None:
            for ep in endpoints:
                key = (ep.method, ep.path_template)
                already = set(ep.responses) | set(inferred[key])
                for s in suggest_error_statuses(ep.method, ep.path_template, ep.request_schema is not None,
                                                already, ep.auth_rate, llm):
                    inferred[key][s["status"]] = InferredResponse(
                        schema=envelope, evidence=s["reason"], confidence="low", inferred_by="llm")
    return build_spec(endpoints, llm=llm, inferred=inferred)


# ---------------------------------------------------------------------------
# Spec diff
# ---------------------------------------------------------------------------

def _operations(spec: dict[str, Any] | None) -> dict[tuple[str, str], dict[str, Any]]:
    """{(METHOD, path): operation} for every operation in the spec."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    paths = (spec or {}).get("paths") or {}
    if not isinstance(paths, dict):
        return out
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in _HTTP_METHODS:
            op = item.get(method)
            if isinstance(op, dict):
                out[(method.upper(), str(path))] = op
    return out


def _branches(schema: Any) -> dict[str, dict[str, Any]]:
    """Map each concrete (non-null) type of a schema to the sub-schema describing it.
    Handles OpenAPI anyOf/oneOf branches and raw JSON-Schema type lists. {} / untyped -> {} ("unknown")."""
    if not isinstance(schema, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key in ("anyOf", "oneOf"):
        if isinstance(schema.get(key), list):
            for branch in schema[key]:
                for t, sub in _branches(branch).items():
                    out.setdefault(t, sub)
    t = schema.get("type")
    if isinstance(t, str) and t != "null":
        out.setdefault(t, schema)
    elif isinstance(t, list):
        for x in t:
            if isinstance(x, str) and x != "null":
                out.setdefault(x, schema)
    return out


def _fmt_types(types: dict[str, Any]) -> str:
    return "|".join(sorted(types)) or "any"


FieldPath = tuple[str, ...]  # ("address", "city"), ("[]", "email") — "[]" = array items
_StatsKey = tuple[str, str, str]  # (METHOD, path_template, status as str)


def _fmt_path(path: FieldPath) -> str:
    """("address", "city") -> ".address.city" ; ("[]", "email") -> "[].email" (matches diff locations)."""
    return "".join("[]" if c == "[]" else "." + c for c in path)


def _present_paths(value: Any, prefix: FieldPath = ()) -> set[FieldPath]:
    """Every field path present in one JSON body (a key holding null still counts as present)."""
    out: set[FieldPath] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            p = prefix + (str(k),)
            out.add(p)
            out |= _present_paths(v, p)
    elif isinstance(value, list) and value:
        p = prefix + ("[]",)
        out.add(p)
        for item in value:
            out |= _present_paths(item, p)
    return out


def _required_paths(schema: Any, prefix: FieldPath = ()) -> set[FieldPath]:
    """Field paths marked required in an OpenAPI schema (nested required fields included)."""
    out: set[FieldPath] = set()
    br = _branches(schema)
    if "object" in br:
        obj = br["object"]
        req = set(obj.get("required") or [])
        for name, sub in (obj.get("properties") or {}).items():
            p = prefix + (str(name),)
            if name in req:
                out.add(p)
            out |= _required_paths(sub, p)
    if "array" in br:
        out |= _required_paths(br["array"].get("items"), prefix + ("[]",))
    return out


def _schema_at(spec: dict[str, Any], method: str, path: str, scope: str, field: FieldPath) -> Any:
    """Sub-schema of `field` in the request body (scope "request") or the response for status `scope`."""
    op = _operations(spec).get((method, path))
    if op is None:
        return None
    if scope == _REQUEST:
        schema: Any = _json_schema(op.get("requestBody"))
    else:
        schema = _json_schema({str(k): v for k, v in (op.get("responses") or {}).items()}.get(scope))
    for part in field:
        if schema is None:
            return None
        if part == "[]":
            arr = _branches(schema).get("array")
            schema = arr.get("items") if arr else None
        else:
            obj = _branches(schema).get("object")
            schema = (obj.get("properties") or {}).get(part) if obj else None
    return schema


_Field = tuple[str, str, str, FieldPath]  # (METHOD, path, scope, field)


class TrafficStats:
    """What the traffic says, beyond the accumulated spec: per (endpoint, status) sample counts, per-field
    "missing in a row" streaks, which fields are new since warm-up and whether they have been present ever
    since. Scopes are response status codes plus "request" (request bodies of successful requests).

    `mark(spec)` snapshots the state that belongs to `spec` and commits the decisions the last diff made;
    diff_specs(old, new, stats) uses the marked snapshot for the old side. ContractWatcher calls mark()
    after every accepted rebuild.
    """

    def __init__(self, min_samples: int = MIN_SAMPLES_FOR_REQUIRED, removal_streak: int = REMOVAL_STREAK,
                 new_required_streak: int = NEW_REQUIRED_STREAK, rename_window: int = RENAME_WINDOW) -> None:
        self.min_samples = min_samples
        self.removal_streak = removal_streak
        self.new_required_streak = new_required_streak
        self.rename_window = rename_window
        self.counts: dict[_StatsKey, int] = {}             # responses per (METHOD, path, status)
        self._marked_counts: dict[_StatsKey, int] = {}
        self._req_counts: dict[tuple[str, str], int] = {}  # successful requests with a body, per endpoint
        self._clock: dict[_StatsKey, int] = {}             # tracked bodies per key (sample index)
        self._known: dict[_StatsKey, set[FieldPath]] = {}
        self._streak: dict[_StatsKey, dict[FieldPath, int]] = {}
        self._missing_since: dict[_StatsKey, dict[FieldPath, int]] = {}  # index where the current streak began
        self._first_seen: dict[_StatsKey, dict[FieldPath, int]] = {}
        # fields first seen after warm-up -> times present since (None once it was missing)
        self._new: dict[_StatsKey, dict[FieldPath, int | None]] = {}
        self._new_seen: dict[_Field, int] = {}          # times a new field was present (never reset)
        self._marked_new_seen: dict[_Field, int] = {}
        self.ever_required: set[_Field] = set()
        self.reported_removed: set[_Field] = set()
        self.decided: dict[_Field, str] = {}   # new field -> "required" | "optional" | "added" | "renamed" | "baseline"
        self.held_optional: set[_Field] = set()  # became_optional postponed while a rename is pending
        self._staged_decided: dict[_Field, str] = {}
        self._staged_hold: set[_Field] = set()
        self._staged_release: set[_Field] = set()

    # --- feeding ---------------------------------------------------------
    def add(self, entries: list[LogEntry]) -> None:
        for e in entries:
            template, _ = normalize_path(e.get("path", ""))
            method, status = str(e["method"]).upper(), str(int(e["status"]))
            key: _StatsKey = (method, template, status)
            before = self.counts.get(key, 0)
            self.counts[key] = before + 1
            if e.get("response_body") is not None:
                self._track(key, e["response_body"], warm=before >= self.min_samples)
            if status.startswith("2") and e.get("request_body") is not None:
                req_before = self._req_counts.get((method, template), 0)
                self._track((method, template, _REQUEST), e["request_body"], warm=req_before >= self.min_samples)
                self._req_counts[(method, template)] = req_before + 1

    def _track(self, key: _StatsKey, body: Any, warm: bool) -> None:
        index = self._clock.get(key, 0)
        self._clock[key] = index + 1
        present = _present_paths(body) | {()}
        known = self._known.setdefault(key, set())
        first = {f for f in present - known if f}
        known |= present
        first_seen = self._first_seen.setdefault(key, {})
        for f in first:
            first_seen[f] = index
        streak = self._streak.setdefault(key, {})
        since = self._missing_since.setdefault(key, {})
        for f in known:
            if f and f[:-1] in present:  # only count samples where the parent is there
                if f in present:
                    streak[f] = 0
                    since.pop(f, None)
                else:
                    if not streak.get(f):
                        since[f] = index
                    streak[f] = streak.get(f, 0) + 1
        if warm and first:
            new = self._new.setdefault(key, {})
            for f in first:
                new[f] = 0
        new = self._new.get(key)
        if new:
            for f, n in new.items():
                if f in present:
                    self._new_seen[key + (f,)] = self._new_seen.get(key + (f,), 0) + 1
                if n is not None and f[:-1] in present:
                    new[f] = n + 1 if f in present else None

    # --- the diff stages decisions, mark() commits them --------------------
    def begin_diff(self) -> None:
        self._staged_decided, self._staged_hold, self._staged_release = {}, set(), set()

    def stage_decision(self, item: _Field, decision: str) -> None:
        self._staged_decided[item] = decision

    def stage_hold(self, item: _Field) -> None:
        self._staged_hold.add(item)

    def stage_release(self, item: _Field) -> None:
        self._staged_release.add(item)

    def is_decided(self, item: _Field) -> bool:
        return item in self.decided or item in self._staged_decided

    def held(self) -> set[_Field]:
        return (self.held_optional | self._staged_hold) - self._staged_release

    def mark(self, spec: dict[str, Any]) -> None:
        self._marked_counts = dict(self.counts)
        self._marked_new_seen = dict(self._new_seen)
        for (m, p), op in _operations(spec).items():
            for status, resp in (op.get("responses") or {}).items():
                status = str(status)
                if self.counts.get((m, p, status), 0) < self.min_samples:
                    continue
                for f in _required_paths(_json_schema(resp)):
                    self.ever_required.add((m, p, status, f))
            req = _json_schema(op.get("requestBody"))
            if req is not None and self._req_counts.get((m, p), 0) >= self.min_samples:
                for f in _required_paths(req):
                    self.ever_required.add((m, p, _REQUEST, f))
        self.reported_removed = self.removed_now(spec)
        self.decided.update(self._staged_decided)
        self.held_optional = self.held()
        self.begin_diff()

    # --- queries ---------------------------------------------------------
    def _count(self, counts: dict[_StatsKey, int], method: str, path: str, scope: str) -> int:
        """scope: a status code ("201"), "2xx" (request bodies come from 2xx entries) or "*" (all)."""
        if scope == "*":
            return sum(n for (m, p, _), n in counts.items() if m == method and p == path)
        if scope == "2xx":
            return sum(n for (m, p, s), n in counts.items() if m == method and p == path and s.startswith("2"))
        return counts.get((method, path, scope), 0)

    def samples(self, method: str, path: str, scope: str) -> int:
        return self._count(self.counts, method, path, scope)

    def was_warm(self, method: str, path: str, scope: str) -> bool:
        """Did the endpoint/scope have enough samples when the *old* spec was built?"""
        return self._count(self._marked_counts, method, path, scope) >= self.min_samples

    def missing_streak(self, method: str, path: str, status: str, field: FieldPath) -> int:
        return self._streak.get((method, path, status), {}).get(field, 0)

    def removed_now(self, spec: dict[str, Any]) -> set[_Field]:
        ops = _operations(spec)
        return {r for r in self.ever_required
                if (r[0], r[1]) in ops and self.missing_streak(*r) >= self.removal_streak}

    def new_fields(self) -> list[tuple[_Field, int | None]]:
        """Fields first seen after warm-up, with how often they were present since (None = went missing)."""
        return [((m, p, scope, f), n) for (m, p, scope), new in self._new.items() for f, n in new.items()]

    def under_new_field(self, item: _Field) -> bool:
        """Is this field nested inside another field that is itself new? (Only the top-most is reported.)"""
        new = self._new.get(item[:3], {})
        f = item[3]
        return any(f[:i] in new for i in range(1, len(f)))

    def nested_warm(self, item: _Field) -> bool:
        """Warm-up for fields inside a *new* object: their required/optional status is only trusted once
        every new ancestor had MIN_SAMPLES_FOR_REQUIRED samples when the old spec was built."""
        new = self._new.get(item[:3], {})
        f = item[3]
        return all(self._marked_new_seen.get(item[:3] + (f[:i],), 0) >= self.min_samples
                   for i in range(1, len(f)) if f[:i] in new)

    def rename_pairs(self, spec: dict[str, Any]) -> list[tuple[_Field, _Field, bool]]:
        """(old, new, complete) rename candidates. complete = the old field has now been missing
        REMOVAL_STREAK times in a row; before that the pair is pending."""
        pairs: list[tuple[_Field, _Field, bool]] = []
        keys = set(self._new) | {r[:3] for r in self.ever_required}
        for key in sorted(keys):
            m, p, scope = key
            since = self._missing_since.get(key, {})
            first_seen = self._first_seen.get(key, {})
            olds = [r[3] for r in self.ever_required
                    if r[:3] == key and r not in self.reported_removed and r[3] in since]
            news = [f for f, n in self._new.get(key, {}).items()
                    if n is not None and not self.is_decided(key + (f,)) and not self.under_new_field(key + (f,))]
            if not olds or not news:
                continue

            def types(f: FieldPath) -> frozenset[str]:
                return frozenset(_branches(_schema_at(spec, m, p, scope, f)))

            cand = [(x, y) for x in olds for y in news
                    if x[:-1] == y[:-1] and x[-1] != "[]" and y[-1] != "[]"
                    and abs(first_seen.get(y, -10**9) - since[x]) <= self.rename_window
                    and types(x) and types(x) == types(y)]
            per_old: dict[FieldPath, int] = {}
            per_new: dict[FieldPath, int] = {}
            for x, y in cand:
                per_old[x] = per_old.get(x, 0) + 1
                per_new[y] = per_new.get(y, 0) + 1
            used_old: set[FieldPath] = set()
            used_new: set[FieldPath] = set()
            for x, y in sorted(cand, key=lambda xy: (-_name_similarity(xy[0][-1], xy[1][-1]), xy)):
                if x in used_old or y in used_new:
                    continue
                if _name_similarity(x[-1], y[-1]) > 0 or (per_old[x] == 1 and per_new[y] == 1):
                    used_old.add(x)
                    used_new.add(y)
                    pairs.append((key + (x,), key + (y,), self.missing_streak(*key, x) >= self.removal_streak))
        return pairs

    def warming(self) -> dict[_StatsKey, int]:
        """(METHOD, path, status) -> samples, for everything still below the threshold."""
        return {k: n for k, n in self.counts.items() if n < self.min_samples}


class _Differ:
    def __init__(self, detected_at: str, stats: TrafficStats | None = None) -> None:
        self.detected_at = detected_at
        self.stats = stats
        self.changes: list[SpecChange] = []
        self.method = ""
        self.path = ""
        self.old_ops: dict[tuple[str, str], dict[str, Any]] = {}
        self.new_spec: dict[str, Any] = {}
        self._pairs: list[tuple[_Field, _Field, bool]] | None = None

    def pairs(self) -> list[tuple[_Field, _Field, bool]]:
        if self._pairs is None:
            self._pairs = self.stats.rename_pairs(self.new_spec) if self.stats is not None else []
        return self._pairs

    def emit(self, kind: str, location: str, detail: str, breaking: bool) -> None:
        self.changes.append(SpecChange(kind=kind, method=self.method, path=self.path, location=location,
                                       detail=detail, breaking=breaking, detected_at=self.detected_at))

    def trusted(self, scope: str) -> bool:
        """Is required/optional information trustworthy for this endpoint + scope? (No stats -> yes.)"""
        return self.stats is None or self.stats.was_warm(self.method, self.path, scope)

    # --- schemas ---------------------------------------------------------
    def schema(self, old: Any, new: Any, loc: str, side: str, track_required: bool,
               scope: str = "", fpath: FieldPath = ()) -> None:
        ob, nb = _branches(old), _branches(new)
        if ob and nb and set(ob) != set(nb):
            self.emit("type_changed", loc, f"type {_fmt_types(ob)} -> {_fmt_types(nb)}", True)
            return  # children of a re-typed field are not meaningful to compare
        if "object" in ob and "object" in nb:
            self.object(ob["object"], nb["object"], loc, side, track_required, scope, fpath)
        if "array" in ob and "array" in nb:
            self.schema(ob["array"].get("items"), nb["array"].get("items"), loc + "[]", side, track_required,
                        scope, fpath + ("[]",))

    def object(self, old: dict[str, Any], new: dict[str, Any], loc: str, side: str, track_required: bool,
               scope: str = "", fpath: FieldPath = ()) -> None:
        op = old.get("properties") or {}
        np_ = new.get("properties") or {}
        oreq = set(old.get("required") or [])
        nreq = set(new.get("required") or [])
        for name in op:
            if name not in np_:
                self.emit("field_removed", f"{loc}.{name}", "field removed", True)
        for name in np_:
            if name not in op:
                if not track_required:
                    continue  # still warming up: fields seen now are part of the baseline, not changes
                if self.stats is not None:
                    continue  # continuous mode: traffic_changes() reports new fields once the evidence is in
                required = name in nreq
                # A new *required* request field breaks existing clients; anything else is additive.
                self.emit("field_added", f"{loc}.{name}",
                          "new required field" if required else "new optional field",
                          side == "request" and required)
        for name in op:
            if name not in np_:
                continue
            item = (self.method, self.path, scope, fpath + (name,))
            if track_required and (self.stats is None or self.stats.nested_warm(item)):
                was, now = name in oreq, name in nreq
                if not was and now:
                    self.emit("became_required", f"{loc}.{name}", "optional -> required", True)
                elif was and not now:
                    if self.stats is not None and any(o == item and not done for o, _, done in self.pairs()):
                        self.stats.stage_hold(item)  # maybe half of a rename: decide once the evidence is in
                    else:
                        self.emit("became_optional", f"{loc}.{name}", _optional_detail(side), False)
            self.schema(op[name], np_[name], f"{loc}.{name}", side, track_required, scope, fpath + (name,))

    # --- operations ------------------------------------------------------
    def params(self, old_op: dict[str, Any], new_op: dict[str, Any]) -> None:
        def index(op: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
            return {(str(p.get("in")), str(p.get("name"))): p
                    for p in op.get("parameters") or [] if isinstance(p, dict)}

        old, new = index(old_op), index(new_op)
        trusted = self.trusted("*")
        for key, p in old.items():
            if key not in new:
                self.emit("field_removed", f"request.{key[0]}.{key[1]}", "parameter removed", True)
        for key, p in new.items():
            loc = f"request.{key[0]}.{key[1]}"
            if key not in old:
                if not trusted:
                    continue  # warming up: part of the baseline
                required = bool(p.get("required"))
                self.emit("field_added", loc, "new required parameter" if required else "new optional parameter",
                          required)
                continue
            op_, np_ = old[key], p
            ob, nb = _branches(op_.get("schema")), _branches(np_.get("schema"))
            if ob and nb and set(ob) != set(nb):
                self.emit("type_changed", loc, f"parameter type {_fmt_types(ob)} -> {_fmt_types(nb)}", True)
            if not trusted:
                continue
            was, now = bool(op_.get("required")), bool(np_.get("required"))
            if not was and now:
                self.emit("became_required", loc, "optional -> required", True)
            elif was and not now:
                self.emit("became_optional", loc, "required -> optional", False)

    def request_body(self, old_op: dict[str, Any], new_op: dict[str, Any]) -> None:
        o = _json_schema(old_op.get("requestBody"))
        n = _json_schema(new_op.get("requestBody"))
        if o is None and n is None:
            return
        trusted = self.trusted("2xx")
        if o is None:
            if trusted:
                self.emit("field_added", "request.body", "request body added", False)
        elif n is None:
            self.emit("field_removed", "request.body", "request body removed", True)
        else:
            self.schema(o, n, "request.body", "request", trusted, _REQUEST)

    def responses(self, old_op: dict[str, Any], new_op: dict[str, Any]) -> None:
        # x-inferred responses (errors.py; opt-in --infer-errors) are a guess, not traffic — excluded
        # entirely so they can never appear as a status_added/status_removed/schema change as the
        # evidence behind a guess shifts between rebuilds (e.g. auth_rate crossing 50%).
        old = {str(k): v for k, v in (old_op.get("responses") or {}).items() if not _is_inferred(v)}
        new = {str(k): v for k, v in (new_op.get("responses") or {}).items() if not _is_inferred(v)}
        for status in old:
            if status not in new:
                self.emit("status_removed", f"response.{status}", f"status {status} no longer returned",
                          status.startswith("2"))
        for status in new:
            if status not in old:
                self.emit("status_added", f"response.{status}", f"new status {status}", False)
        for status in old:
            if status not in new:
                continue
            o, n = _json_schema(old[status]), _json_schema(new[status])
            loc = f"response.{status}.body"
            if o is None and n is None:
                continue
            trusted = self.trusted(status)
            if o is None:
                if trusted:
                    self.emit("field_added", loc, "response body added", False)
            elif n is None:
                self.emit("field_removed", loc, "response body removed", True)
            else:
                self.schema(o, n, loc, "response", trusted, status)

    # --- changes decided from traffic ----------------------------------------------------------
    def traffic_changes(self, new: dict[str, Any]) -> None:
        """Changes that need the traffic, not just two specs (continuous mode only). Each is reported once,
        for the top-most field only, and only for endpoints present in both specs:
        - field_renamed (BREAKING): a pending rename pair whose old field is now missing REMOVAL_STREAK times.
        - field_removed (BREAKING): a formerly required field missing REMOVAL_STREAK times, not renamed.
          A same-rebuild became_optional for it (or anything under it) is dropped.
        - field_added for fields new since warm-up: responses ok at once; requests BREAKING "new required
          field" after NEW_REQUIRED_STREAK successful requests that all had it, ok "new optional field" as
          soon as one didn't. Fields in a pending rename wait.
        - became_optional that was held for a pending rename that did not happen after all."""
        stats = self.stats
        if stats is None:
            return
        both = {k for k in _operations(new) if k in self.old_ops}
        pairs = [pr for pr in self.pairs() if pr[0][:2] in both]
        pending_new = {y for _, y, done in pairs if not done}
        pending_old = {x for x, _, done in pairs if not done}

        # renames
        renamed: dict[_Field, _Field] = {x: y for x, y, done in pairs if done}
        for x in sorted(_top_most(set(renamed)), key=_stats_order):
            y = renamed[x]
            self.method, self.path = x[0], x[1]
            detail = f"{x[3][-1]} appears to be renamed to {y[3][-1]}"
            if x[3][:-1]:
                detail += f" (in {_fmt_path(x[3][:-1]).lstrip('.')})"
            self.emit("field_renamed", _stats_location(x[2], x[3]), detail, True)
            stats.stage_decision(y, "renamed")

        # removals (not renamed, not inside something renamed)
        def inside(item: _Field, group: set[_Field]) -> bool:
            return any(g[:3] == item[:3] and item[3][:len(g[3])] == g[3] for g in group)

        removed = {r for r in stats.removed_now(new) - stats.reported_removed
                   if r[:2] in both and not inside(r, set(renamed))}
        removed = _top_most(removed)
        for m, p, scope, field in sorted(removed, key=_stats_order):
            self.method, self.path = m, p
            if scope == _REQUEST:
                detail = f"not sent in the last {stats.removal_streak}+ successful requests"
            else:
                detail = f"missing from the last {stats.removal_streak}+ responses"
            self.emit("field_removed", _stats_location(scope, field), detail, True)

        gone = set(renamed) | removed
        gone_locs = {(g[0], g[1], _stats_location(g[2], g[3])) for g in gone}

        def covered(c: SpecChange) -> bool:
            return c.kind == "became_optional" and any(
                c.method == m and c.path == p and (c.location == loc or c.location.startswith((loc + ".", loc + "[]")))
                for m, p, loc in gone_locs)

        self.changes = [c for c in self.changes if not covered(c)]

        # new fields
        for item, times in sorted(stats.new_fields(), key=lambda t: _stats_order(t[0])):
            m, p, scope, field = item
            if (m, p) not in both or stats.is_decided(item) or stats.under_new_field(item) or item in pending_new:
                continue
            if not stats.was_warm(m, p, "2xx" if scope == _REQUEST else scope):
                stats.stage_decision(item, "baseline")  # appeared while the batch crossed warm-up
                continue
            self.method, self.path = m, p
            loc = _stats_location(scope, field)
            if scope != _REQUEST:
                self.emit("field_added", loc, "new field", False)
                stats.stage_decision(item, "added")
            elif times is None:
                self.emit("field_added", loc, "new optional field", False)
                stats.stage_decision(item, "optional")
            elif times >= stats.new_required_streak:
                self.emit("field_added", loc,
                          f"new required field: sent in every successful request since it appeared "
                          f"({stats.new_required_streak}+); clients that don't send it are rejected", True)
                stats.stage_decision(item, "required")

        # became_optional held for a rename that didn't happen
        for item in sorted(stats.held(), key=_stats_order):
            if item in pending_old:
                continue
            stats.stage_release(item)
            if item in gone or inside(item, gone) or item in stats.reported_removed:
                continue
            m, p, scope, field = item
            self.method, self.path = m, p
            self.emit("became_optional", _stats_location(scope, field),
                      _optional_detail("request" if scope == _REQUEST else "response"), False)


def _optional_detail(side: str) -> str:
    return "required -> optional (absent from some responses)" if side == "response" else "required -> optional"


def _stats_location(scope: str, field: FieldPath) -> str:
    base = "request.body" if scope == _REQUEST else f"response.{scope}.body"
    return base + _fmt_path(field)


def _stats_order(r: tuple[str, str, str, FieldPath]) -> tuple:
    return (r[1], r[0], r[2], r[3])


def _top_most(items: set[tuple[str, str, str, FieldPath]]) -> set[tuple[str, str, str, FieldPath]]:
    """Drop entries whose field is nested under another entry of the same endpoint + scope."""
    return {r for r in items
            if not any(o[:3] == r[:3] and len(o[3]) < len(r[3]) and r[3][:len(o[3])] == o[3] for o in items)}


def _json_schema(obj: Any) -> dict[str, Any] | None:
    """Schema of the application/json content of a requestBody / response object, or None."""
    if not isinstance(obj, dict):
        return None
    content = obj.get("content") or {}
    media = content.get("application/json") if isinstance(content, dict) else None
    if not isinstance(media, dict):
        return None
    schema = media.get("schema")
    return schema if isinstance(schema, dict) else None


def _is_inferred(response_obj: Any) -> bool:
    """True for a response entry errors.py guessed (x-inferred: true) rather than one observed in
    traffic — see _Differ.responses, which excludes these from diffing entirely."""
    return isinstance(response_obj, dict) and response_obj.get("x-inferred") is True


def diff_specs(old: dict[str, Any] | None, new: dict[str, Any],
               stats: TrafficStats | None = None) -> list[SpecChange]:
    """Compare two OpenAPI dicts. `old=None` means every endpoint is 'endpoint_added'.

    Breaking (breaking=True):
      endpoint_removed, field_removed (request or response), type_changed anywhere (incl. parameters),
      became_required, status_removed (2xx only), new *required* request field/parameter.
    Non-breaking: endpoint_added, status_added, new optional field/parameter, became_optional.
    Walks nested properties/items; `location` is a dotted path like "response.200.body.address.city"
    (array items add "[]", parameters are "request.query.limit" / "request.path.id").

    With `stats` (continuous mode), field_added and required/optional changes are only reported for an
    endpoint + status that had >= MIN_SAMPLES_FOR_REQUIRED samples when `old` was built (before that,
    fields are part of the baseline). Type changes are always reported. A formerly required response
    field missing from REMOVAL_STREAK responses in a row is reported as field_removed (BREAKING), and the
    same for request fields over successful requests. A request field that first appears after warm-up
    and is then in every successful request (NEW_REQUIRED_STREAK+) is reported as newly required (BREAKING).
    Without stats the diff is spec-only: no warm-up, and removal needs the field to leave the spec.
    """
    d = _Differ(_now_iso(), stats)
    old_ops, new_ops = _operations(old), _operations(new)
    d.old_ops, d.new_spec = old_ops, new
    if stats is not None:
        stats.begin_diff()
    order = lambda k: (k[1], k[0])  # noqa: E731 — sort by path, then method

    for key in sorted(old_ops.keys() - new_ops.keys(), key=order):
        d.method, d.path = key
        d.emit("endpoint_removed", "", "endpoint removed", True)
    for key in sorted(new_ops.keys() - old_ops.keys(), key=order):
        d.method, d.path = key
        d.emit("endpoint_added", "", "new endpoint", False)
    for key in sorted(old_ops.keys() & new_ops.keys(), key=order):
        d.method, d.path = key
        o, n = old_ops[key], new_ops[key]
        d.params(o, n)
        d.request_body(o, n)
        d.responses(o, n)
    d.traffic_changes(new)
    return d.changes


def append_changes(changes: list[SpecChange], path: str | Path = DEFAULT_CHANGES_PATH) -> None:
    """Append each change as one JSON line (dataclasses.asdict)."""
    if not changes:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        for c in changes:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
        f.flush()


# ---------------------------------------------------------------------------
# Prism
# ---------------------------------------------------------------------------

def find_prism() -> str | None:
    """Path of the Prism executable, or None.

    On Windows, npm installs both `prism` (an extensionless sh script) and `prism.cmd`. On Python 3.12+
    shutil.which("prism") can return the sh script, which Popen rejects with WinError 193 — so look for
    `prism.cmd` first and only then fall back to `prism`."""
    if _IS_WINDOWS:
        return shutil.which("prism.cmd") or shutil.which("prism")
    return shutil.which("prism")


class PrismManager:
    """Runs `prism mock <spec> --port <port> --host 0.0.0.0 -d` as a subprocess.

    -d (dynamic): every call returns freshly generated data that follows the schema (formats, enums and
    integer ranges inferred from the logs make it realistic). dynamic=False serves the recorded examples."""

    def __init__(self, spec_path: str | Path = DEFAULT_SPEC_PATH, port: int = DEFAULT_PRISM_PORT,
                 dynamic: bool = True) -> None:
        self.spec_path = Path(spec_path)
        self.port = port
        self.dynamic = dynamic
        self.proc: subprocess.Popen | None = None
        self._log_file: Any = None
        self._warned_missing = False

    def start(self) -> None:
        """Start Prism if not running. On Windows the executable is `prism.cmd` (see find_prism)."""
        if self.is_running():
            return
        exe = find_prism()
        if exe is None:
            if not self._warned_missing:
                _say("WARNING: prism not found on PATH (npm install -g @stoplight/prism-cli); "
                     "continuing without a mock server")
                self._warned_missing = True
            return
        if not self.spec_path.exists():
            _say(f"WARNING: {self.spec_path} does not exist yet; not starting prism")
            return

        log_path = self.spec_path.parent / "prism.log"
        self._log_file = open(log_path, "ab")
        kwargs: dict[str, Any] = {}
        if _IS_WINDOWS:
            # own process group so we can send CTRL_BREAK to prism.cmd *and* its node child
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        cmd = [exe, "mock", str(self.spec_path), "--port", str(self.port), "--host", "0.0.0.0"]
        if self.dynamic:
            cmd.append("-d")
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self._log_file,
                                         stderr=subprocess.STDOUT, **kwargs)
        except OSError as e:
            _say(f"WARNING: could not start prism: {e}")
            self._close_log()
            self.proc = None
            return
        mode = "dynamic data" if self.dynamic else "static examples"
        _say(f"prism: mock server on http://localhost:{self.port}, {mode} (pid {self.proc.pid}, log {log_path})")

    def stop(self) -> None:
        """Terminate Prism (wait up to 5s, then kill). No-op if not running."""
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                self._terminate(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._kill(proc)
                    proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as e:
            _say(f"WARNING: problem stopping prism: {e}")
        finally:
            self.proc = None
            self._close_log()

    def restart(self) -> None:
        """stop() then start(). Called after every spec rewrite."""
        self.stop()
        self.start()

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # --- helpers ---------------------------------------------------------
    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        if _IS_WINDOWS:
            # Kill the whole tree (prism.cmd -> cmd.exe -> node) while it is still intact. A softer
            # CTRL_BREAK can end cmd.exe first; proc.wait() then returns and the orphaned node keeps
            # port 4010, so the next Prism start fails. Prism holds no state, so /F loses nothing.
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGTERM)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        if _IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _close_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None


# ---------------------------------------------------------------------------
# Debounce + incremental rebuild
# ---------------------------------------------------------------------------

class RebuildThrottle:
    """Allows at most one rebuild per `interval` seconds, however many notifications arrive.

    notify() marks work pending (safe to call from the watchdog thread); due(now) says whether a rebuild
    should run now; begin(now) clears the pending flag and starts the interval.
    """

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._pending = threading.Event()
        self._last: float | None = None

    def notify(self) -> None:
        self._pending.set()

    @property
    def pending(self) -> bool:
        return self._pending.is_set()

    def due(self, now: float) -> bool:
        return self._pending.is_set() and (self._last is None or now - self._last >= self.interval)

    def begin(self, now: float) -> None:
        self._pending.clear()  # cleared *before* the rebuild so lines arriving during it trigger another
        self._last = now


LLM_MIN_INTERVAL = 5.0  # seconds between background LLM-refinement passes, at most (see ContractWatcher)


def _merge_llm_descriptions(spec: dict[str, Any], enriched: dict[str, Any]) -> dict[str, Any]:
    """A copy of `spec` with `summary`/`description`/`x-llm-model` carried over from `enriched` wherever
    the same operation (method + path) and field (by name) still exist. Used by ContractWatcher.process
    so the dashboard keeps showing the last successful AIDH pass's text in between background refinement
    runs, instead of every plain rebuild wiping it out until the next (much slower) pass finishes. Never
    touches types/required/schema shape — only description-ish text — so a stale carried-over value is
    at worst slightly out of date, never structurally wrong."""
    merged = copy.deepcopy(spec)
    if (enriched.get("info") or {}).get("x-llm-model"):
        merged.setdefault("info", {})["x-llm-model"] = enriched["info"]["x-llm-model"]

    old_ops: dict[tuple[str, str], dict[str, Any]] = {}
    for path, item in (enriched.get("paths") or {}).items():
        if isinstance(item, dict):
            for method, op in item.items():
                if isinstance(op, dict):
                    old_ops[(path, method)] = op

    for path, item in (merged.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            old_op = old_ops.get((path, method))
            if not old_op:
                continue
            if old_op.get("description"):
                op["summary"] = old_op.get("summary", op.get("summary"))
                op["description"] = old_op["description"]
            _merge_body_descriptions(op.get("requestBody"), old_op.get("requestBody"))
            for status, resp in (op.get("responses") or {}).items():
                old_resp = (old_op.get("responses") or {}).get(status)
                if isinstance(old_resp, dict):
                    _merge_body_descriptions(resp, old_resp)
    return merged


def _merge_body_descriptions(body: Any, old_body: Any) -> None:
    """Carry field descriptions from old_body's JSON schema onto body's, in place (request/response
    body -> content.application/json.schema)."""
    if not isinstance(body, dict) or not isinstance(old_body, dict):
        return
    schema = (body.get("content") or {}).get("application/json", {}).get("schema")
    old_schema = (old_body.get("content") or {}).get("application/json", {}).get("schema")
    _merge_schema_descriptions(schema, old_schema)


def _merge_schema_descriptions(schema: Any, old_schema: Any) -> None:
    if not isinstance(schema, dict) or not isinstance(old_schema, dict):
        return
    for branch_key in ("anyOf", "oneOf"):
        branches, old_branches = schema.get(branch_key), old_schema.get(branch_key)
        if isinstance(branches, list) and isinstance(old_branches, list):
            for b in branches:
                for ob in old_branches:
                    if isinstance(b, dict) and isinstance(ob, dict) and b.get("type") == ob.get("type"):
                        _merge_schema_descriptions(b, ob)
    props, old_props = schema.get("properties"), old_schema.get("properties")
    if isinstance(props, dict) and isinstance(old_props, dict):
        for name, prop in props.items():
            old_prop = old_props.get(name)
            if not isinstance(prop, dict) or not isinstance(old_prop, dict):
                continue
            if old_prop.get("description") and not prop.get("description"):
                prop["description"] = old_prop["description"]
            _merge_schema_descriptions(prop, old_prop)
    items, old_items = schema.get("items"), old_schema.get("items")
    if isinstance(items, dict) and isinstance(old_items, dict):
        _merge_schema_descriptions(items, old_items)


class ContractWatcher:
    """State for continuous mode: the log offset, all entries seen so far, and the last good spec.

    `llm`, if given, never runs inline in the rebuild loop: diffing/breaking-change detection is timing-
    sensitive (it relies on samples arriving close to real time, e.g. "missing from the last 10
    responses in a row"), and a real LLM call can take seconds — running it synchronously here would
    stretch each rebuild far past `debounce_seconds`, letting dozens of log lines (including the very
    traffic that proves a breaking change) pile up into a single rebuild and collapse past that
    evidence. Instead, `process()` always rebuilds/diffs from the plain rule-based spec, and a separate
    background thread periodically (at most every LLM_MIN_INTERVAL seconds, never overlapping itself)
    rebuilds WITH the LLM from the same accumulated entries and overwrites spec_path with that
    AI-annotated version — the dashboard just re-reads whatever is on disk. `self.spec` (used for
    diffing) is only ever the rule-based version, so LLM latency can never affect change detection.

    Without more care this would make the AI descriptions flicker: a fast-moving log makes `process()`
    rebuild far more often than one LLM pass takes, so its plain write would almost immediately stomp
    the enriched file the background thread just wrote. So `process()` caches the most recent
    successful enrichment (`self._last_enriched`) and, when writing its own rebuild, carries forward
    any summary/description/x-llm-model that still matches by operation and field name (see
    _merge_llm_descriptions) — `self.spec` itself stays the untouched rule-based version throughout."""

    def __init__(
        self,
        log_path: str | Path,
        spec_path: str | Path = DEFAULT_SPEC_PATH,
        changes_path: str | Path = DEFAULT_CHANGES_PATH,
        prism: PrismManager | None = None,
        on_update: Callable[[dict[str, Any], list[SpecChange]], None] | None = None,
        llm: AidhClient | None = None,
        infer_errors: bool = False,
        reporter: Any = None,
    ) -> None:
        self.log_path = Path(log_path)
        self.spec_path = Path(spec_path)
        self.changes_path = Path(changes_path)
        self.prism = prism
        self.on_update = on_update
        self.llm = llm
        self.reporter = reporter  # report.ChangeReporter: a Word report per breaking burst (None = off)
        self.infer_errors = infer_errors  # errors.py's B2 guesses; a pure/fast computation (unlike llm),
                                          # so unlike the LLM pass this runs inline, no async needed
        self.entries: list[LogEntry] = []
        self.offset = 0
        self.skipped: dict[str, int] = {}  # skip-reason tally across the whole session (A6 quality report)
        self.spec: dict[str, Any] | None = None
        self.stats = TrafficStats()
        self.rebuilds = 0
        self._reset_reported = False
        self._warming_reported: set[tuple[str, str, str]] | None = None  # last printed warming-up set
        self._llm_lock = threading.Lock()
        self._llm_thread: threading.Thread | None = None
        self._llm_last_run = 0.0
        self._llm_pending = False
        self._last_enriched: dict[str, Any] | None = None  # most recent successful LLM pass; merged
                                                            # into each plain rebuild's write (see above)

    def initialize(self) -> list[SpecChange]:
        """Write the initial spec from whatever is in the log already (empty spec if missing/empty).
        That first build is the baseline: it is never diffed against a spec file left on disk by an
        earlier run (which may come from different logs), so startup reports no changes. Always []."""
        self.spec = None
        return self.process(initial=True)

    def process(self, initial: bool = False) -> list[SpecChange]:
        """Read new log lines, rebuild, diff, write. Returns the changes found (possibly [])."""
        new_entries, new_offset = read_new_logs(self.log_path, self.offset, self.skipped)
        if new_offset < self.offset or (new_offset == 0 and self.offset > 0):
            if not self._reset_reported:
                what = "missing" if not self.log_path.exists() else "truncated"
                _say(f"log file {what}; will read {self.log_path} from the start "
                     f"(keeping {len(self.entries)} entries already seen)")
                self._reset_reported = True
        else:
            self._reset_reported = False
        self.offset = new_offset

        if not new_entries and not initial:
            return []
        self.entries.extend(new_entries)
        self.stats.add(new_entries)

        try:
            spec = build_from_entries(self.entries, infer_errors=self.infer_errors)  # rule-based (+ opt-in
                                       # error guesses); never the LLM here — see class docstring
        except Exception as e:  # noqa: BLE001 — never let one rebuild kill continuous mode
            _say(f"ERROR: rebuild failed ({type(e).__name__}: {e}); keeping last good spec")
            return []
        errors = validate_spec(spec)
        if errors:
            _say(f"ERROR: rebuilt spec is invalid ({len(errors)} errors, first: {errors[0]}); keeping last good spec")
            return []

        changes = [] if initial else diff_specs(self.spec, spec, self.stats)  # first build = baseline
        if initial or spec != self.spec:
            to_write = _merge_llm_descriptions(spec, self._last_enriched) if self._last_enriched else spec
            write_spec(to_write, self.spec_path)
        self._write_quality_report(spec)
        self.spec = spec
        self.stats.mark(spec)
        if not initial:
            self.rebuilds += 1

        n_breaking = sum(c.breaking for c in changes)
        n_endpoints = len(_operations(spec))
        label = "baseline" if initial else f"rebuild #{self.rebuilds}"
        summary = "later changes are diffed against this" if initial else (f"{len(changes)} change{'s' if len(changes) != 1 else ''} ({n_breaking} breaking)"
                   if changes else "no contract changes")
        _say(f"{label}: {len(self.entries)} entries (+{len(new_entries)}), {n_endpoints} endpoints, {summary}")
        for c in changes:
            tag = "BREAKING" if c.breaking else "ok      "
            where = f" {c.location}" if c.location else ""
            _say(f"  {tag} {c.kind:<16} {c.method} {c.path}{where}: {c.detail}")
        self._report_warming()

        if changes:
            append_changes(changes, self.changes_path)
            if self.reporter is not None:
                try:
                    self.reporter.notify(spec, changes, time.monotonic())
                except Exception as e:  # noqa: BLE001
                    _say(f"WARNING: change reporter failed: {e}")
            if self.prism is not None and not initial:
                self.prism.restart()
            if self.on_update is not None:
                try:
                    self.on_update(spec, changes)
                except Exception as e:  # noqa: BLE001
                    _say(f"WARNING: on_update callback failed: {e}")
        if self.llm is not None:
            self._maybe_refine_async()
        return changes

    def _maybe_refine_async(self) -> None:
        """Kick off a background LLM-refinement pass if one isn't already running and at least
        LLM_MIN_INTERVAL seconds have passed since the last one started. Never blocks `process()`."""
        with self._llm_lock:
            if self._llm_thread is not None and self._llm_thread.is_alive():
                self._llm_pending = True  # more entries arrived mid-pass; the worker will redo on exit
                return
            if time.monotonic() - self._llm_last_run < LLM_MIN_INTERVAL:
                return
            self._llm_last_run = time.monotonic()
            snapshot = list(self.entries)
            self._llm_thread = threading.Thread(target=self._refine_worker, args=(snapshot,), daemon=True)
            self._llm_thread.start()

    def _refine_worker(self, entries: list[LogEntry]) -> None:
        """Rebuild WITH the LLM from a snapshot of entries and overwrite spec_path with the result.
        Runs off the main thread; never touches self.spec/self.stats (those stay rule-based-only, see
        class docstring), so a slow or failed AIDH call can never affect diffing or breaking-change
        detection — at worst the dashboard briefly shows undescribed fields until the next pass."""
        try:
            enriched = build_from_entries(entries, self.llm)
            errors = validate_spec(enriched)
            if errors:
                _say(f"WARNING: LLM-refined spec failed validation ({errors[0]}); keeping the plain spec on disk")
            else:
                write_spec(enriched, self.spec_path)
                self._last_enriched = enriched
        except Exception as e:  # noqa: BLE001 — a refinement pass must never take down the watcher
            _say(f"WARNING: LLM refinement pass failed: {type(e).__name__}: {e}")
        finally:
            with self._llm_lock:
                redo, self._llm_pending, self._llm_thread = self._llm_pending, False, None
            if redo:
                self._maybe_refine_async()

    def _report_warming(self) -> None:
        """Print the pairs still warming up only when the *set* of warming-up endpoint+status pairs
        changed (a pair appeared or reached MIN_SAMPLES_FOR_REQUIRED), not when counts merely grew."""
        warming = self.stats.warming()
        keys = set(warming)
        if self._warming_reported is not None and keys == self._warming_reported:
            return
        previous = self._warming_reported or set()
        self._warming_reported = keys
        n = self.stats.min_samples
        warmed = sorted(k for k in previous if k not in keys)
        if warmed:
            _say("  warmed up (changes now tracked): "
                 + ", ".join(f"{m} {p} {s}" for m, p, s in warmed))
        if warming:
            items = sorted(warming.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2]))
            _say(f"  warming up (<{n} samples, changes not tracked yet): "
                 + ", ".join(f"{m} {p} {s} ({c}/{n})" for (m, p, s), c in items))

    def _write_quality_report(self, spec: dict[str, Any]) -> None:
        """output/quality.json next to the spec (A6) — never lets a report-writing hiccup break the
        rebuild it's reporting on."""
        try:
            report = build_quality_report(spec, len(self.entries), self.skipped)
            path = self.spec_path.parent / "quality.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — the spec/changes files are the important output
            _say(f"WARNING: could not write quality report: {type(e).__name__}: {e}")


def watch(
    log_path: str | Path,
    spec_path: str | Path = DEFAULT_SPEC_PATH,
    changes_path: str | Path = DEFAULT_CHANGES_PATH,
    prism: PrismManager | None = None,
    debounce_seconds: float = 1.0,
    on_update: Callable[[dict[str, Any], list[SpecChange]], None] | None = None,
    stop_event: threading.Event | None = None,
    poll_interval: float = 1.0,
    llm: AidhClient | None = None,
    infer_errors: bool = False,
    reporter: Any = None,
) -> None:
    """Block until Ctrl+C (or until `stop_event` is set — used by tests).

    1. Write an initial spec from whatever is already in log_path (empty spec if file missing/empty), start Prism.
    2. watchdog Observer on log_path's directory; on create/modify/move of log_path, debounce, then
       parser.read_new_logs -> add to accumulated entries -> build_from_entries -> diff_specs vs previous
       -> if changed: write_spec, append_changes, prism.restart(), on_update(spec, changes).
    A cheap poll every `poll_interval` seconds backs up watchdog (some filesystems drop events).
    Survives bad log lines and invalid intermediate specs (logs the error, keeps the last good spec).
    `reporter` (report.ChangeReporter) gets every change batch and, once a burst containing a BREAKING
    change has been quiet for its quiet window, writes the Word change report; it is flushed on stop.
    """
    from watchdog.observers import Observer  # imported lazily so `main.py build` doesn't need watchdog

    log_path = Path(log_path)
    target = os.path.normcase(os.path.abspath(log_path))
    watch_dir = log_path.resolve().parent
    watch_dir.mkdir(parents=True, exist_ok=True)
    stop = stop_event or threading.Event()

    state = ContractWatcher(log_path, spec_path, changes_path, prism, on_update, llm, infer_errors, reporter)
    throttle = RebuildThrottle(debounce_seconds)

    class _Handler:
        def dispatch(self, event: Any) -> None:
            for attr in ("src_path", "dest_path"):
                p = getattr(event, attr, None)
                if not p:
                    continue
                if isinstance(p, bytes):
                    p = os.fsdecode(p)
                if os.path.normcase(os.path.abspath(p)) == target:
                    throttle.notify()
                    return

    observer = Observer()
    try:
        state.initialize()
        if prism is not None:
            prism.start()
        observer.schedule(_Handler(), str(watch_dir), recursive=False)
        observer.start()
        _say(f"watching {log_path} (rebuild at most every {debounce_seconds:g}s; Ctrl+C to stop)")

        last_poll = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            if now - last_poll >= poll_interval:
                last_poll = now
                throttle.notify()  # read_new_logs is a cheap stat when nothing changed
            if throttle.due(now):
                throttle.begin(now)
                try:
                    state.process()
                except Exception as e:  # noqa: BLE001
                    _say(f"ERROR: {type(e).__name__}: {e}")
            if reporter is not None:
                try:
                    reporter.flush_if_quiet(now)
                except Exception as e:  # noqa: BLE001
                    _say(f"WARNING: change reporter failed: {e}")
            stop.wait(0.05)
    except KeyboardInterrupt:
        _say("stopping (Ctrl+C)")
    finally:
        if reporter is not None:
            try:
                reporter.flush()  # a burst still settling at shutdown still gets its report
            except Exception as e:  # noqa: BLE001
                _say(f"WARNING: change reporter failed: {e}")
        if observer.is_alive():
            observer.stop()
            observer.join(timeout=5)
        if prism is not None:
            prism.stop()
