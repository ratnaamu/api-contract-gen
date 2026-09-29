"""watcher.py — continuous mode: watch the log file, re-infer, diff the spec, restart Prism.   Owner: [Name 3]

Contract:
    log file changes  ->  output/openapi.yaml (rewritten)  +  output/changes.jsonl (appended SpecChange rows)
The dashboard only reads those two files, so dashboard and watcher can be built/run independently.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from inferrer import infer_all
from models import LogEntry, SpecChange
from normalizer import group_by_endpoint, normalize_path
from parser import read_new_logs
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
# Request bodies get the same traffic-based evidence (successful 2xx requests only):
#  - a formerly required request field missing from REMOVAL_STREAK successful requests in a row is
#    reported as field_removed (BREAKING) — e.g. the old name of a renamed field;
#  - a field that first appears *after* warm-up and is then present in every successful request,
#    NEW_REQUIRED_STREAK times or more, is reported as a new required field (BREAKING): old clients
#    that don't send it are now being rejected.
NEW_REQUIRED_STREAK = 10
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

def build_from_entries(entries: list[LogEntry]) -> dict[str, Any]:
    """Full pipeline on in-memory entries: normalizer.group_by_endpoint -> inferrer.infer_all -> spec_builder.build_spec."""
    return build_spec(infer_all(group_by_endpoint(entries)))


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


class TrafficStats:
    """Per (endpoint, status) sample counts and per-field "missing in a row" streaks.

    `mark(spec)` snapshots the state that belongs to `spec`; diff_specs(old, new, stats) then uses the
    marked snapshot for the old side (was the old required/optional status trustworthy?) and the live
    streaks for removal detection. ContractWatcher calls mark() after every accepted rebuild.
    """

    def __init__(self, min_samples: int = MIN_SAMPLES_FOR_REQUIRED, removal_streak: int = REMOVAL_STREAK,
                 new_required_streak: int = NEW_REQUIRED_STREAK) -> None:
        self.min_samples = min_samples
        self.removal_streak = removal_streak
        self.new_required_streak = new_required_streak
        self.counts: dict[_StatsKey, int] = {}           # responses per (METHOD, path, status)
        self._marked_counts: dict[_StatsKey, int] = {}
        self._req_counts: dict[tuple[str, str], int] = {}  # successful requests with a body, per endpoint
        self._known: dict[_StatsKey, set[FieldPath]] = {}
        self._streak: dict[_StatsKey, dict[FieldPath, int]] = {}
        # request fields first seen after warm-up -> times present since (None once it was missing)
        self._since_new: dict[_StatsKey, dict[FieldPath, int | None]] = {}
        self.ever_required: set[tuple[str, str, str, FieldPath]] = set()
        self.reported_removed: set[tuple[str, str, str, FieldPath]] = set()
        self.reported_new_required: set[tuple[str, str, str, FieldPath]] = set()

    # --- feeding ---------------------------------------------------------
    def add(self, entries: list[LogEntry]) -> None:
        for e in entries:
            template, _ = normalize_path(e.get("path", ""))
            method, status = str(e["method"]).upper(), str(int(e["status"]))
            key: _StatsKey = (method, template, status)
            self.counts[key] = self.counts.get(key, 0) + 1
            if e.get("response_body") is not None:
                self._track(key, e["response_body"])
            if status.startswith("2") and e.get("request_body") is not None:
                before = self._req_counts.get((method, template), 0)
                self._track((method, template, _REQUEST), e["request_body"],
                            track_new=before >= self.min_samples)
                self._req_counts[(method, template)] = before + 1

    def _track(self, key: _StatsKey, body: Any, track_new: bool = False) -> None:
        present = _present_paths(body) | {()}
        known = self._known.setdefault(key, set())
        first_seen = present - known
        known |= present
        streak = self._streak.setdefault(key, {})
        for f in known:
            if f and f[:-1] in present:  # only count samples where the parent is there
                streak[f] = 0 if f in present else streak.get(f, 0) + 1
        if track_new:
            since = self._since_new.setdefault(key, {})
            for f in first_seen:
                if f:
                    since[f] = 0
        since = self._since_new.get(key)
        if since:
            for f, n in since.items():
                if n is not None and f[:-1] in present:
                    since[f] = n + 1 if f in present else None

    def mark(self, spec: dict[str, Any]) -> None:
        self._marked_counts = dict(self.counts)
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
        self.reported_new_required = self.new_required_now(spec)

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

    def removed_now(self, spec: dict[str, Any]) -> set[tuple[str, str, str, FieldPath]]:
        ops = _operations(spec)
        return {r for r in self.ever_required
                if (r[0], r[1]) in ops and self.missing_streak(*r) >= self.removal_streak}

    def new_required_now(self, spec: dict[str, Any]) -> set[tuple[str, str, str, FieldPath]]:
        """Request fields that appeared after warm-up and were in every successful request since
        (at least NEW_REQUIRED_STREAK of them)."""
        ops = _operations(spec)
        return {(m, p, scope, f)
                for (m, p, scope), since in self._since_new.items() if (m, p) in ops
                for f, n in since.items() if n is not None and n >= self.new_required_streak}

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

    def emit(self, kind: str, location: str, detail: str, breaking: bool) -> None:
        self.changes.append(SpecChange(kind=kind, method=self.method, path=self.path, location=location,
                                       detail=detail, breaking=breaking, detected_at=self.detected_at))

    def trusted(self, scope: str) -> bool:
        """Is required/optional information trustworthy for this endpoint + scope? (No stats -> yes.)"""
        return self.stats is None or self.stats.was_warm(self.method, self.path, scope)

    # --- schemas ---------------------------------------------------------
    def schema(self, old: Any, new: Any, loc: str, side: str, track_required: bool) -> None:
        ob, nb = _branches(old), _branches(new)
        if ob and nb and set(ob) != set(nb):
            self.emit("type_changed", loc, f"type {_fmt_types(ob)} -> {_fmt_types(nb)}", True)
            return  # children of a re-typed field are not meaningful to compare
        if "object" in ob and "object" in nb:
            self.object(ob["object"], nb["object"], loc, side, track_required)
        if "array" in ob and "array" in nb:
            self.schema(ob["array"].get("items"), nb["array"].get("items"), loc + "[]", side, track_required)

    def object(self, old: dict[str, Any], new: dict[str, Any], loc: str, side: str, track_required: bool) -> None:
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
                required = name in nreq
                # A new *required* request field breaks existing clients; anything else is additive.
                self.emit("field_added", f"{loc}.{name}",
                          "new required field" if required else "new optional field",
                          side == "request" and required)
        for name in op:
            if name not in np_:
                continue
            if track_required:
                was, now = name in oreq, name in nreq
                if not was and now:
                    self.emit("became_required", f"{loc}.{name}", "optional -> required", True)
                elif was and not now:
                    detail = ("required -> optional (absent from some responses)" if side == "response"
                              else "required -> optional")
                    self.emit("became_optional", f"{loc}.{name}", detail, False)
            self.schema(op[name], np_[name], f"{loc}.{name}", side, track_required)

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
            self.schema(o, n, "request.body", "request", trusted)

    def responses(self, old_op: dict[str, Any], new_op: dict[str, Any]) -> None:
        old = {str(k): v for k, v in (old_op.get("responses") or {}).items()}
        new = {str(k): v for k, v in (new_op.get("responses") or {}).items()}
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
                self.schema(o, n, loc, "response", trusted)

    # --- changes detected from traffic (removals, new required request fields) ------------
    def traffic_changes(self, new: dict[str, Any]) -> None:
        """From TrafficStats, each reported once and only for the top-most field:
        - field_removed (BREAKING): a formerly required response/request field missing REMOVAL_STREAK
          times in a row. Replaces a same-rebuild became_optional for that field or anything under it.
        - new required request field (BREAKING): appeared after warm-up, then in every successful request
          NEW_REQUIRED_STREAK+ times. Upgrades a same-rebuild field_added, else reported as became_required."""
        stats = self.stats
        if stats is None:
            return
        removed = _top_most(stats.removed_now(new) - stats.reported_removed)
        required = _top_most(stats.new_required_now(new) - stats.reported_new_required)

        removed_locs: set[tuple[str, str, str]] = set()
        for m, p, scope, field in sorted(removed, key=_stats_order):
            self.method, self.path = m, p
            loc = _stats_location(scope, field)
            removed_locs.add((m, p, loc))
            if scope == _REQUEST:
                detail = f"not sent in the last {stats.removal_streak}+ successful requests (renamed or removed?)"
            else:
                detail = f"missing from the last {stats.removal_streak}+ responses"
            self.emit("field_removed", loc, detail, True)

        def covered(c: SpecChange) -> bool:
            return c.kind == "became_optional" and any(
                c.method == m and c.path == p and (c.location == loc or c.location.startswith((loc + ".", loc + "[]")))
                for m, p, loc in removed_locs)

        self.changes = [c for c in self.changes if not covered(c)]

        for m, p, scope, field in sorted(required, key=_stats_order):
            loc = _stats_location(scope, field)
            detail = (f"new required field: sent in every successful request since it appeared "
                      f"({stats.new_required_streak}+); clients without it are rejected")
            same = next((c for c in self.changes
                         if c.kind == "field_added" and c.method == m and c.path == p and c.location == loc), None)
            if same is not None:  # appeared and proved required within one rebuild
                same.detail, same.breaking = detail, True
            else:
                self.method, self.path = m, p
                self.emit("became_required", loc, detail, True)


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
    """Runs `prism mock <spec> --port <port> --host 0.0.0.0 --dynamic` as a subprocess."""

    def __init__(self, spec_path: str | Path = DEFAULT_SPEC_PATH, port: int = DEFAULT_PRISM_PORT) -> None:
        self.spec_path = Path(spec_path)
        self.port = port
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
        cmd = [exe, "mock", str(self.spec_path), "--port", str(self.port), "--host", "0.0.0.0", "--dynamic"]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self._log_file,
                                         stderr=subprocess.STDOUT, **kwargs)
        except OSError as e:
            _say(f"WARNING: could not start prism: {e}")
            self._close_log()
            self.proc = None
            return
        _say(f"prism: mock server on http://localhost:{self.port} (pid {self.proc.pid}, log {log_path})")

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


class ContractWatcher:
    """State for continuous mode: the log offset, all entries seen so far, and the last good spec."""

    def __init__(
        self,
        log_path: str | Path,
        spec_path: str | Path = DEFAULT_SPEC_PATH,
        changes_path: str | Path = DEFAULT_CHANGES_PATH,
        prism: PrismManager | None = None,
        on_update: Callable[[dict[str, Any], list[SpecChange]], None] | None = None,
    ) -> None:
        self.log_path = Path(log_path)
        self.spec_path = Path(spec_path)
        self.changes_path = Path(changes_path)
        self.prism = prism
        self.on_update = on_update
        self.entries: list[LogEntry] = []
        self.offset = 0
        self.spec: dict[str, Any] | None = None
        self.stats = TrafficStats()
        self.rebuilds = 0
        self._reset_reported = False
        self._warming_reported: set[tuple[str, str, str]] | None = None  # last printed warming-up set

    def initialize(self) -> list[SpecChange]:
        """Write the initial spec from whatever is in the log already (empty spec if missing/empty).
        That first build is the baseline: it is never diffed against a spec file left on disk by an
        earlier run (which may come from different logs), so startup reports no changes. Always []."""
        self.spec = None
        return self.process(initial=True)

    def process(self, initial: bool = False) -> list[SpecChange]:
        """Read new log lines, rebuild, diff, write. Returns the changes found (possibly [])."""
        new_entries, new_offset = read_new_logs(self.log_path, self.offset)
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
            spec = build_from_entries(self.entries)
        except Exception as e:  # noqa: BLE001 — never let one rebuild kill continuous mode
            _say(f"ERROR: rebuild failed ({type(e).__name__}: {e}); keeping last good spec")
            return []
        errors = validate_spec(spec)
        if errors:
            _say(f"ERROR: rebuilt spec is invalid ({len(errors)} errors, first: {errors[0]}); keeping last good spec")
            return []

        changes = [] if initial else diff_specs(self.spec, spec, self.stats)  # first build = baseline
        if initial or spec != self.spec:
            write_spec(spec, self.spec_path)
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
            if self.prism is not None and not initial:
                self.prism.restart()
            if self.on_update is not None:
                try:
                    self.on_update(spec, changes)
                except Exception as e:  # noqa: BLE001
                    _say(f"WARNING: on_update callback failed: {e}")
        return changes

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


def watch(
    log_path: str | Path,
    spec_path: str | Path = DEFAULT_SPEC_PATH,
    changes_path: str | Path = DEFAULT_CHANGES_PATH,
    prism: PrismManager | None = None,
    debounce_seconds: float = 1.0,
    on_update: Callable[[dict[str, Any], list[SpecChange]], None] | None = None,
    stop_event: threading.Event | None = None,
    poll_interval: float = 1.0,
) -> None:
    """Block until Ctrl+C (or until `stop_event` is set — used by tests).

    1. Write an initial spec from whatever is already in log_path (empty spec if file missing/empty), start Prism.
    2. watchdog Observer on log_path's directory; on create/modify/move of log_path, debounce, then
       parser.read_new_logs -> add to accumulated entries -> build_from_entries -> diff_specs vs previous
       -> if changed: write_spec, append_changes, prism.restart(), on_update(spec, changes).
    A cheap poll every `poll_interval` seconds backs up watchdog (some filesystems drop events).
    Survives bad log lines and invalid intermediate specs (logs the error, keeps the last good spec).
    """
    from watchdog.observers import Observer  # imported lazily so `main.py build` doesn't need watchdog

    log_path = Path(log_path)
    target = os.path.normcase(os.path.abspath(log_path))
    watch_dir = log_path.resolve().parent
    watch_dir.mkdir(parents=True, exist_ok=True)
    stop = stop_event or threading.Event()

    state = ContractWatcher(log_path, spec_path, changes_path, prism, on_update)
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
            stop.wait(0.05)
    except KeyboardInterrupt:
        _say("stopping (Ctrl+C)")
    finally:
        if observer.is_alive():
            observer.stop()
            observer.join(timeout=5)
        if prism is not None:
            prism.stop()
