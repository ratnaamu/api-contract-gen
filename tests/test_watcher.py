"""Tests for watcher.py. Run from repo root: pytest -q   (Prism is never needed.)"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import watcher  # noqa: E402
from spec_builder import load_spec, validate_spec  # noqa: E402
from watcher import (  # noqa: E402
    ContractWatcher, PrismManager, RebuildThrottle, TrafficStats, append_changes, build_from_entries, diff_specs,
    watch,
)

SAMPLE = ROOT / "sample_logs.jsonl"
CHANGED = ROOT / "changed_logs.jsonl"


# ---------- helpers ----------

def entry(method: str = "GET", path: str = "/users/1", status: int = 200, response: Any = None,
          request: Any = None, query: dict[str, str] | None = None, ts: str = "2026-09-01T09:00:00Z") -> dict:
    return {"timestamp": ts, "method": method, "path": path, "query": query or {},
            "request_body": request, "status": status, "response_body": response, "headers": {}}


def spec_of(*entries: dict) -> dict:
    return build_from_entries(list(entries))


def kinds(changes) -> set[tuple[str, str, bool]]:
    return {(c.kind, c.location, c.breaking) for c in changes}


class FakePrism:
    def __init__(self) -> None:
        self.starts = self.stops = self.restarts = 0

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1

    def restart(self) -> None:
        self.restarts += 1


def wait_for(cond, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


def read_changes(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------- diff: non-breaking ----------

def test_old_none_means_every_endpoint_added():
    new = spec_of(entry(response={"id": 1}), entry("POST", "/orders", 201, {"ok": True}, {"a": 1}))
    changes = diff_specs(None, new)
    assert {(c.kind, c.method, c.path, c.breaking) for c in changes} == {
        ("endpoint_added", "GET", "/users/{id}", False),
        ("endpoint_added", "POST", "/orders", False),
    }
    assert all(c.detected_at for c in changes)


def test_identical_specs_have_no_changes():
    s = spec_of(entry(response={"id": 1, "name": "a"}))
    assert diff_specs(s, s) == []


def test_new_endpoint_is_non_breaking():
    old = spec_of(entry(response={"id": 1}))
    new = spec_of(entry(response={"id": 1}), entry("DELETE", "/users/1", 204))
    changes = diff_specs(old, new)
    assert len(changes) == 1
    c = changes[0]
    assert (c.kind, c.method, c.path, c.location, c.breaking) == ("endpoint_added", "DELETE", "/users/{id}", "", False)


def test_new_optional_response_field_is_non_breaking():
    old = spec_of(entry(response={"id": 1}))
    new = spec_of(entry(response={"id": 1}), entry(response={"id": 2, "phone": "x"}))
    assert kinds(diff_specs(old, new)) == {("field_added", "response.200.body.phone", False)}


def test_new_optional_request_field_is_non_breaking():
    old = spec_of(entry("POST", "/orders", 201, {"ok": True}, {"a": 1}))
    new = spec_of(entry("POST", "/orders", 201, {"ok": True}, {"a": 1}),
                  entry("POST", "/orders", 201, {"ok": True}, {"a": 1, "note": "x"}))
    assert kinds(diff_specs(old, new)) == {("field_added", "request.body.note", False)}


def test_new_status_code_is_non_breaking():
    old = spec_of(entry(response={"id": 1}))
    new = spec_of(entry(response={"id": 1}), entry(status=404, response={"error": "nope"}))
    assert kinds(diff_specs(old, new)) == {("status_added", "response.404", False)}


# ---------- diff: breaking ----------

def test_endpoint_removed_is_breaking():
    old = spec_of(entry(response={"id": 1}), entry("DELETE", "/users/1", 204))
    new = spec_of(entry(response={"id": 1}))
    changes = diff_specs(old, new)
    assert [(c.kind, c.method, c.breaking) for c in changes] == [("endpoint_removed", "DELETE", True)]


def test_field_removed_is_breaking():
    old = spec_of(entry(response={"id": 1, "email": "a@b"}))
    new = spec_of(entry(response={"id": 1}))
    assert kinds(diff_specs(old, new)) == {("field_removed", "response.200.body.email", True)}


def test_field_type_changed_is_breaking():
    old = spec_of(entry(response={"id": 1}))
    new = spec_of(entry(response={"id": "usr_1"}))
    changes = diff_specs(old, new)
    assert kinds(changes) == {("type_changed", "response.200.body.id", True)}
    assert "integer -> string" in changes[0].detail


def test_widened_type_counts_as_type_changed():
    """Accumulated logs merge int + string into anyOf; that is still a type change."""
    old = spec_of(entry(response={"id": 1}))
    new = spec_of(entry(response={"id": 1}), entry(response={"id": "usr_1"}))
    assert kinds(diff_specs(old, new)) == {("type_changed", "response.200.body.id", True)}


def test_nested_type_change_has_dotted_location():
    old = spec_of(entry(response={"address": {"city": "Austin"}, "tags": [{"n": 1}]}))
    new = spec_of(entry(response={"address": {"city": 7}, "tags": [{"n": "x"}]}))
    assert kinds(diff_specs(old, new)) == {
        ("type_changed", "response.200.body.address.city", True),
        ("type_changed", "response.200.body.tags[].n", True),
    }


def test_optional_field_became_required_is_breaking():
    old = spec_of(entry("POST", "/orders", 201, {"ok": True}, {"a": 1}),
                  entry("POST", "/orders", 201, {"ok": True}, {"a": 1, "note": "x"}))
    new = spec_of(entry("POST", "/orders", 201, {"ok": True}, {"a": 1, "note": "x"}))
    assert kinds(diff_specs(old, new)) == {("became_required", "request.body.note", True)}


def test_new_required_request_field_is_breaking():
    old = spec_of(entry("POST", "/orders", 201, {"ok": True}, {"a": 1}))
    new = spec_of(entry("POST", "/orders", 201, {"ok": True}, {"a": 1, "b": 2}))
    assert kinds(diff_specs(old, new)) == {("field_added", "request.body.b", True)}


def test_response_field_became_optional_is_non_breaking():
    """Spec-only: an occasional absence is just required -> optional. Removal needs traffic stats
    (see the TrafficStats tests below)."""
    old = spec_of(entry(response={"id": 1, "email": "a@b"}))
    new = spec_of(entry(response={"id": 1, "email": "a@b"}), entry(response={"id": 2}))
    assert kinds(diff_specs(old, new)) == {("became_optional", "response.200.body.email", False)}


def test_request_field_became_optional_is_non_breaking():
    old = spec_of(entry("POST", "/orders", 201, {"ok": True}, {"a": 1, "b": 2}))
    new = spec_of(entry("POST", "/orders", 201, {"ok": True}, {"a": 1, "b": 2}),
                  entry("POST", "/orders", 201, {"ok": True}, {"a": 1}))
    assert kinds(diff_specs(old, new)) == {("became_optional", "request.body.b", False)}


def test_param_type_changed_is_breaking():
    old = spec_of(entry("GET", "/products", response=[], query={"limit": "10"}))
    new = spec_of(entry("GET", "/products", response=[], query={"limit": "ten"}))
    changes = diff_specs(old, new)
    assert kinds(changes) == {("type_changed", "request.query.limit", True)}
    assert "integer -> string" in changes[0].detail


def test_path_param_type_changed_is_breaking():
    old = spec_of(entry(path="/users/12", response={"ok": 1}))
    new = spec_of(entry(path="/users/usr_000012", response={"ok": 1}))
    assert kinds(diff_specs(old, new)) == {("type_changed", "request.path.id", True)}


def test_2xx_status_removed_is_breaking_but_4xx_is_not():
    old = spec_of(entry(response={"id": 1}), entry(status=404, response={"e": 1}))
    new = spec_of(entry(status=404, response={"e": 1}))
    assert kinds(diff_specs(old, new)) == {("status_removed", "response.200", True)}
    assert kinds(diff_specs(new, old)) == {("status_added", "response.200", False)}
    old2 = spec_of(entry(response={"id": 1}), entry(status=404, response={"e": 1}))
    new2 = spec_of(entry(response={"id": 1}))
    assert kinds(diff_specs(old2, new2)) == {("status_removed", "response.404", False)}


def test_changed_logs_produce_expected_alerts(tmp_path, capsys):
    log = tmp_path / "live.jsonl"
    log.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    cw = ContractWatcher(log, tmp_path / "openapi.yaml", tmp_path / "changes.jsonl")
    cw.initialize()
    changes = []
    for line in CHANGED.read_text(encoding="utf-8").splitlines(keepends=True):
        with log.open("a", encoding="utf-8") as f:
            f.write(line)
        changes += cw.process()
    got = kinds(changes)
    for field in ("email", "name", "id"):  # dropped / renamed in every changed GET /users/{id}
        assert ("field_removed", f"response.200.body.{field}", True) in got
    assert ("field_added", "response.200.body.full_name", False) in got
    assert ("field_added", "response.200.body.user_id", False) in got
    assert any(c.kind == "endpoint_added" and c.method == "DELETE" for c in changes)
    # each removal is reported exactly once
    removed = [c.location for c in changes if c.kind == "field_removed"]
    assert len(removed) == len(set(removed)) == 3


# ---------- warm-up + removal detection (TrafficStats) ----------

def run_batches(*batches: list[dict]) -> list[list]:
    """Feed batches the way ContractWatcher does; return the changes of each batch after the first."""
    stats = TrafficStats()
    entries: list[dict] = []
    spec = None
    out = []
    for batch in batches:
        entries += batch
        stats.add(batch)
        new = build_from_entries(entries)
        if spec is not None:
            out.append(diff_specs(spec, new, stats))
        spec = new
        stats.mark(new)
    return out


def users(n: int, **fields) -> list[dict]:
    return [entry("POST", "/users", 201, {"id": i, **fields}, {"name": "a"}) for i in range(n)]


def test_thresholds_are_constants():
    assert watcher.MIN_SAMPLES_FOR_REQUIRED == 10
    assert watcher.REMOVAL_STREAK == 10


def test_warming_up_suppresses_required_optional_changes():
    # 5 samples all with phone -> phone "required", but only 5 samples: not trusted yet
    (changes,) = run_batches(users(5, phone="x"), users(1))
    assert changes == []


def test_after_warm_up_optional_change_is_reported_non_breaking():
    (changes,) = run_batches(users(12, phone="x"), users(1))
    assert kinds(changes) == {("became_optional", "response.201.body.phone", False)}


def test_warm_up_is_per_status_code():
    ok = users(12, phone="x")
    bad = [entry("POST", "/users", 400, {"error": "e", "field": "f"}, {"x": 1}) for _ in range(3)]
    (changes,) = run_batches(ok + bad, [entry("POST", "/users", 400, {"error": "e"}, {"x": 1})] + users(1))
    # 201 is warm (12 samples) -> reported; 400 has 3 samples -> suppressed
    assert kinds(changes) == {("became_optional", "response.201.body.phone", False)}


def test_no_field_added_while_warming_up_request_body():
    stats = TrafficStats()
    stats.add([entry("POST", "/orders", 201, {"ok": True}, {"a": 1})] * 3)
    old = spec_of(entry("POST", "/orders", 201, {"ok": True}, {"a": 1}))
    stats.mark(old)
    new = spec_of(entry("POST", "/orders", 201, {"ok": True}, {"a": 1, "b": 2}))
    assert diff_specs(old, new, stats) == []


def test_no_field_added_while_warming_up_response():
    (changes,) = run_batches(users(5), users(1, phone="x"))
    assert changes == []


def test_no_field_added_while_warming_up_nested_and_array():
    rows = lambda n, **f: [entry("GET", "/users", 200, [{"id": i, **f}]) for i in range(n)]  # noqa: E731
    (changes,) = run_batches(rows(4), rows(1, address={"city": "x"}))
    assert changes == []


def test_no_field_added_while_warming_up_query_param():
    q = lambda n, **p: [entry("GET", "/products", 200, [], query=p) for _ in range(n)]  # noqa: E731
    (changes,) = run_batches(q(3), q(1, limit="10"))
    assert changes == []
    (changes,) = run_batches(q(12), q(1, limit="10"))
    assert kinds(changes) == {("field_added", "request.query.limit", False)}


def test_no_response_body_added_while_warming_up():
    (changes,) = run_batches([entry(status=200, response=None) for _ in range(3)], [entry(response={"id": 1})])
    assert changes == []


def test_field_added_reported_after_warm_up():
    (changes,) = run_batches(users(12), users(1, phone="x"))
    assert kinds(changes) == {("field_added", "response.201.body.phone", False)}


def test_warm_up_batch_itself_is_baseline():
    """Old side had 9 samples; the batch that crosses 10 still counts as warm-up."""
    per_batch = run_batches(users(9), users(3, phone="x"), users(1, phone="x", tag="t"))
    assert per_batch[0] == []                                                       # 9 -> 12: baseline
    assert kinds(per_batch[1]) == {("field_added", "response.201.body.tag", False)}  # now tracked


def test_field_added_warm_up_is_per_status_code():
    ok = users(12)
    bad = [entry("POST", "/users", 400, {"error": "e"}, {"x": 1}) for _ in range(3)]
    (changes,) = run_batches(ok + bad, [entry("POST", "/users", 400, {"error": "e", "hint": "h"}, {"x": 1})]
                             + users(1, phone="x"))
    assert kinds(changes) == {("field_added", "response.201.body.phone", False)}  # 400.hint suppressed


def test_type_change_still_reported_while_warming_up():
    (changes,) = run_batches(users(3), [entry("POST", "/users", 201, {"id": "usr_1"}, {"name": "a"})])
    assert kinds(changes) == {("type_changed", "response.201.body.id", True)}


def test_became_required_only_reported_after_warm_up():
    old = spec_of(entry("POST", "/orders", 201, {"ok": True}, {"a": 1}),
                  entry("POST", "/orders", 201, {"ok": True}, {"a": 1, "note": "x"}))
    new = spec_of(entry("POST", "/orders", 201, {"ok": True}, {"a": 1, "note": "x"}))
    for n, expected in ((3, set()), (10, {("became_required", "request.body.note", True)})):
        stats = TrafficStats()
        stats.add([entry("POST", "/orders", 201, {"ok": True}, {"a": 1})] * n)
        stats.mark(old)
        assert kinds(diff_specs(old, new, stats)) == expected, n


def test_occasionally_missing_field_is_never_breaking():
    """The real false alarm: phone in the first 13 samples, then sometimes missing."""
    mixed = [e for i in range(20) for e in users(1, **({"phone": "x"} if i % 3 else {}))]
    per_batch = run_batches(users(13, phone="x"), *[[e] for e in mixed])
    flat = [c for batch in per_batch for c in batch]
    assert not [c for c in flat if c.breaking]
    assert ("became_optional", "response.201.body.phone", False) in kinds(flat)


def test_field_missing_streak_is_reported_as_removed_once():
    per_batch = run_batches(users(12, email="a@b"), *[users(1) for _ in range(15)])
    removed = [(i, c) for i, batch in enumerate(per_batch) for c in batch if c.kind == "field_removed"]
    assert len(removed) == 1
    i, c = removed[0]
    assert i == 9  # the 10th response in a row without the field
    assert (c.method, c.path, c.location, c.breaking) == ("POST", "/users", "response.201.body.email", True)
    assert ("became_optional", "response.201.body.email", False) in kinds(per_batch[0])


def test_removal_in_one_big_batch_replaces_became_optional():
    (changes,) = run_batches(users(12, email="a@b"), users(10))
    assert kinds(changes) == {("field_removed", "response.201.body.email", True)}


def test_nested_field_only_counts_responses_with_parent():
    # address is optional-ish: present every other response; city always present inside address
    alternating = [e for i in range(30) for e in (users(1, address={"city": "x"}) if i % 2 else users(1))]
    per_batch = run_batches(users(12, address={"city": "x"}), *[[e] for e in alternating])
    flat = [c for batch in per_batch for c in batch]
    assert not [c for c in flat if c.kind == "field_removed"]


def test_removed_parent_reports_only_top_most_field():
    (changes,) = run_batches(users(12, address={"city": "x", "zip": "1"}), users(10))
    assert kinds(changes) == {("field_removed", "response.201.body.address", True)}


def test_removal_inside_array_items():
    rows = lambda n, **f: [entry("GET", "/users", 200, [{"id": i, **f}, {"id": i + 1, **f}]) for i in range(n)]  # noqa: E731
    (changes,) = run_batches(rows(12, email="a"), rows(10))
    assert kinds(changes) == {("field_removed", "response.200.body[].email", True)}


def _replay_sample(tmp_path: Path, chunk: int) -> list:
    log = tmp_path / f"live_{chunk}.jsonl"
    log.touch()
    cw = ContractWatcher(log, tmp_path / f"openapi_{chunk}.yaml", tmp_path / f"changes_{chunk}.jsonl")
    cw.initialize()
    lines = SAMPLE.read_text(encoding="utf-8").splitlines(keepends=True)
    changes = []
    for i in range(0, len(lines), chunk):
        with log.open("a", encoding="utf-8", newline="\n") as f:
            f.write("".join(lines[i:i + chunk]))
        changes += cw.process()
    return changes


@pytest.mark.parametrize("chunk", [1, 7, 50])  # 1 = a rebuild per line, the strictest case
def test_replaying_sample_logs_alone_has_zero_breaking_changes(tmp_path, chunk):
    changes = _replay_sample(tmp_path, chunk)
    assert any(c.kind == "endpoint_added" for c in changes), "expected the endpoint_added changes"
    breaking = [(c.kind, c.method, c.path, c.location, c.detail) for c in changes if c.breaking]
    assert breaking == []
    # every field in sample_logs.jsonl first shows up during its endpoint+status warm-up -> baseline
    added = [(c.method, c.path, c.location) for c in changes if c.kind == "field_added"]
    assert added == []


def test_sample_logs_have_no_fields_first_seen_after_warm_up():
    """Guard for the test above: if the fixture changes so a field first appears after warm-up,
    a field_added would be correct and the replay test's expectation must change too."""
    from normalizer import normalize_path
    from parser import read_logs
    count: dict = {}
    seen: dict = {}
    late = []
    for e in read_logs(SAMPLE):
        key = (e["method"], normalize_path(e["path"])[0], e["status"])
        body = e["response_body"]
        fields = watcher._present_paths(body) if body is not None else set()
        if count.get(key, 0) >= watcher.MIN_SAMPLES_FOR_REQUIRED:
            late += [(key, watcher._fmt_path(f)) for f in fields - seen.get(key, set())]
        seen.setdefault(key, set()).update(fields)
        count[key] = count.get(key, 0) + 1
    assert late == []
    assert any(n >= watcher.MIN_SAMPLES_FOR_REQUIRED for n in count.values())  # the check was exercised


def _warming_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if "warming up (" in line]


def test_console_marks_warming_up_endpoints(tmp_path, capsys):
    _replay_sample(tmp_path, 10)
    out = capsys.readouterr().out
    assert "warming up (<10 samples" in out
    assert "POST /users 400 (" in out             # only 4 samples in the whole file: never warms up
    assert "warmed up" in out and "POST /users 201" in out


def test_warming_line_only_printed_when_set_changes(tmp_path, capsys):
    log = tmp_path / "live.jsonl"
    cw = ContractWatcher(log, tmp_path / "openapi.yaml", tmp_path / "changes.jsonl")
    cw.initialize()
    capsys.readouterr()

    def add(*entries: dict) -> str:
        with log.open("a", encoding="utf-8", newline="\n") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        cw.process()
        return capsys.readouterr().out

    out = add(*users(3))                                   # POST /users 201 starts warming up
    assert len(_warming_lines(out)) == 1 and "POST /users 201 (3/10)" in out
    for _ in range(4):                                     # counts grow, set unchanged -> silent
        assert _warming_lines(add(*users(1))) == []
    out = add(entry("POST", "/users", 400, {"error": "e"}, {"x": 1}))   # a new pair joins
    assert len(_warming_lines(out)) == 1 and "POST /users 400 (1/10)" in out and "POST /users 201 (7/10)" in out
    assert _warming_lines(add(*users(2))) == []           # 201 at 9/10: still the same set
    out = add(*users(1))                                   # 201 reaches 10 -> leaves the set
    assert "warmed up (changes now tracked): POST /users 201" in out
    assert len(_warming_lines(out)) == 1 and "POST /users 201" not in _warming_lines(out)[0]
    assert _warming_lines(add(*users(5))) == []           # warm endpoint traffic: silent


def test_warming_lines_during_sample_replay_track_set_changes(tmp_path, capsys):
    """One line per rebuild before the fix; now only when the set of warming-up pairs changes."""
    log = tmp_path / "live.jsonl"
    log.touch()
    cw = ContractWatcher(log, tmp_path / "openapi.yaml", tmp_path / "changes.jsonl")
    cw.initialize()
    lines = SAMPLE.read_text(encoding="utf-8").splitlines(keepends=True)
    set_changes, rebuilds, prev = 0, 0, set()
    for line in lines:
        with log.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line)
        cw.process()
        rebuilds += 1
        now = set(cw.stats.warming())
        if now != prev:
            set_changes += 1
        prev = now
    printed = _warming_lines(capsys.readouterr().out)
    assert rebuilds == 200
    assert len(printed) == set_changes      # exactly one line per change of the set ...
    assert set_changes < 40                 # ... which is far fewer than the 200 rebuilds


# ---------- append_changes ----------

def test_append_changes_writes_one_json_line_each(tmp_path):
    out = tmp_path / "sub" / "changes.jsonl"
    changes = diff_specs(None, spec_of(entry(response={"id": 1}), entry("DELETE", "/users/1", 204)))
    append_changes(changes, out)
    append_changes(changes[:1], out)
    rows = read_changes(out)
    assert len(rows) == 3
    assert set(rows[0]) == {"kind", "method", "path", "location", "detail", "breaking", "detected_at"}
    assert rows[0]["detected_at"].endswith("Z")


# ---------- debounce ----------

def test_throttle_allows_at_most_one_rebuild_per_interval():
    t = RebuildThrottle(1.0)
    assert not t.due(0.0)                 # nothing pending
    for _ in range(50):
        t.notify()                        # burst of lines
    assert t.due(0.0)
    t.begin(0.0)
    for i in range(1, 10):
        t.notify()
        assert not t.due(i / 10)          # 0.1 .. 0.9s: still throttled
    assert t.due(1.0)                     # one rebuild once the second is up
    t.begin(1.0)
    assert not t.due(5.0)                 # nothing new since -> no rebuild


def test_throttle_notify_during_rebuild_triggers_another():
    t = RebuildThrottle(1.0)
    t.notify()
    t.begin(0.0)                          # clears pending first ...
    t.notify()                            # ... so a line arriving mid-rebuild is not lost
    assert t.due(1.0)


def test_watch_loop_rebuilds_at_most_once_per_interval(tmp_path, monkeypatch):
    log = tmp_path / "live.jsonl"
    lines = SAMPLE.read_text(encoding="utf-8").splitlines(keepends=True)
    calls: list[float] = []
    real_process = ContractWatcher.process

    def counting(self, initial=False):
        if not initial:
            calls.append(time.monotonic())
        return real_process(self, initial)

    monkeypatch.setattr(ContractWatcher, "process", counting)
    stop = threading.Event()
    th = threading.Thread(target=watch, kwargs=dict(
        log_path=log, spec_path=tmp_path / "o" / "openapi.yaml", changes_path=tmp_path / "o" / "changes.jsonl",
        debounce_seconds=0.5, stop_event=stop, poll_interval=0.05), daemon=True)
    th.start()
    try:
        assert wait_for(lambda: (tmp_path / "o" / "openapi.yaml").exists())
        with log.open("a", encoding="utf-8", newline="\n") as f:
            for line in lines[:60]:       # ~1.2s of steady traffic
                f.write(line)
                f.flush()
                time.sleep(0.02)
        time.sleep(0.7)
    finally:
        stop.set()
        th.join(timeout=10)
    assert calls, "expected at least one rebuild"
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert all(g >= 0.5 - 0.02 for g in gaps), gaps
    assert len(calls) <= 5                # 60 lines, but only a handful of rebuilds


# ---------- ContractWatcher (incremental state) ----------

def test_missing_log_gives_empty_valid_spec_then_picks_up_file(tmp_path):
    log, spec_path, changes_path = tmp_path / "live.jsonl", tmp_path / "openapi.yaml", tmp_path / "changes.jsonl"
    cw = ContractWatcher(log, spec_path, changes_path, prism=FakePrism())
    assert cw.initialize() == []
    spec = load_spec(spec_path)
    assert spec["paths"] == {} and validate_spec(spec) == []

    log.write_text("".join(SAMPLE.read_text(encoding="utf-8").splitlines(keepends=True)[:20]), encoding="utf-8")
    changes = cw.process()
    assert changes and all(c.kind == "endpoint_added" for c in changes)
    assert cw.prism.restarts == 1


def test_bad_lines_and_partial_lines_are_survived(tmp_path):
    log = tmp_path / "live.jsonl"
    good = SAMPLE.read_text(encoding="utf-8").splitlines(keepends=True)
    log.write_text("not json\n" + good[0] + '{"half": ', encoding="utf-8")
    cw = ContractWatcher(log, tmp_path / "openapi.yaml", tmp_path / "changes.jsonl")
    cw.initialize()
    assert len(cw.entries) == 1
    with log.open("a", encoding="utf-8") as f:
        f.write('"line"}\n' + good[1])  # completes the partial (still missing method) + one good line
    cw.process()
    assert len(cw.entries) == 2


def test_truncation_rereads_from_start_and_keeps_entries(tmp_path, capsys):
    log = tmp_path / "live.jsonl"
    lines = SAMPLE.read_text(encoding="utf-8").splitlines(keepends=True)
    log.write_text("".join(lines[:30]), encoding="utf-8")
    cw = ContractWatcher(log, tmp_path / "openapi.yaml", tmp_path / "changes.jsonl")
    cw.initialize()
    assert len(cw.entries) == 30

    log.write_text("".join(lines[30:35]), encoding="utf-8")  # truncated + rewritten, now smaller
    cw.process()
    assert len(cw.entries) == 35
    assert cw.offset == log.stat().st_size
    assert "truncated" in capsys.readouterr().out


def test_no_spec_rewrite_or_prism_restart_without_new_lines(tmp_path):
    log = tmp_path / "live.jsonl"
    log.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    prism = FakePrism()
    cw = ContractWatcher(log, tmp_path / "openapi.yaml", tmp_path / "changes.jsonl", prism=prism)
    cw.initialize()
    mtime = (tmp_path / "openapi.yaml").stat().st_mtime_ns
    assert cw.process() == []
    assert (tmp_path / "openapi.yaml").stat().st_mtime_ns == mtime
    assert prism.restarts == 0


def test_invalid_spec_keeps_last_good(tmp_path, monkeypatch, capsys):
    log = tmp_path / "live.jsonl"
    lines = SAMPLE.read_text(encoding="utf-8").splitlines(keepends=True)
    log.write_text("".join(lines[:10]), encoding="utf-8")
    cw = ContractWatcher(log, tmp_path / "openapi.yaml", tmp_path / "changes.jsonl")
    cw.initialize()
    good = load_spec(tmp_path / "openapi.yaml")

    monkeypatch.setattr(watcher, "validate_spec", lambda spec: ["boom"])
    with log.open("a", encoding="utf-8") as f:
        f.write("".join(lines[10:50]))
    assert cw.process() == []
    assert load_spec(tmp_path / "openapi.yaml") == good
    assert "keeping last good spec" in capsys.readouterr().out


# ---------- startup baseline (bug: leftover spec produced false endpoint_removed) ----------

def _leftover_spec(path: Path) -> None:
    """Simulate `main.py build sample_logs.jsonl` having been run earlier."""
    from parser import read_logs
    from spec_builder import write_spec
    write_spec(build_from_entries(read_logs(SAMPLE)), path)
    assert len(load_spec(path)["paths"]) >= 5


def test_startup_empty_log_ignores_leftover_spec(tmp_path):
    spec_path, changes_path = tmp_path / "openapi.yaml", tmp_path / "changes.jsonl"
    _leftover_spec(spec_path)
    prism = FakePrism()
    cw = ContractWatcher(tmp_path / "live.jsonl", spec_path, changes_path, prism=prism)

    assert cw.initialize() == []                       # no false endpoint_removed
    assert not changes_path.exists()
    assert load_spec(spec_path)["paths"] == {}         # baseline replaced the leftover
    assert prism.restarts == 0

    # later traffic is diffed against the (empty) baseline, not the leftover file
    (tmp_path / "live.jsonl").write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    changes = cw.process()
    assert changes and {c.kind for c in changes} == {"endpoint_added"}
    assert not any(r["kind"] == "endpoint_removed" for r in read_changes(changes_path))


def test_startup_with_existing_log_is_baseline_not_diffed(tmp_path):
    from parser import read_logs
    from spec_builder import write_spec
    spec_path, changes_path = tmp_path / "openapi.yaml", tmp_path / "changes.jsonl"
    write_spec(build_from_entries(read_logs(CHANGED)), spec_path)  # leftover from *different* logs
    log = tmp_path / "live.jsonl"
    log.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    cw = ContractWatcher(log, spec_path, changes_path)
    assert cw.initialize() == []
    assert not changes_path.exists()
    assert load_spec(spec_path) == build_from_entries(read_logs(SAMPLE))
    assert cw.process() == []                          # nothing new -> still nothing


def test_watch_startup_does_not_report_leftover_endpoints(tmp_path):
    out = tmp_path / "output"
    spec_path, changes_path = out / "openapi.yaml", out / "changes.jsonl"
    _leftover_spec(spec_path)
    stop = threading.Event()
    th = threading.Thread(target=watch, kwargs=dict(
        log_path=tmp_path / "live.jsonl", spec_path=spec_path, changes_path=changes_path,
        prism=FakePrism(), debounce_seconds=0.1, stop_event=stop, poll_interval=0.1), daemon=True)
    th.start()
    try:
        assert wait_for(lambda: (load_spec(spec_path) or {}).get("paths") == {})
        time.sleep(0.5)                                # a few idle poll cycles
    finally:
        stop.set()
        th.join(timeout=10)
    assert read_changes(changes_path) == []


# ---------- finding Prism (bug: WinError 193 from extensionless `prism` on Windows) ----------

def _fake_which(available: dict[str, str]):
    return lambda name: available.get(name)


def test_find_prism_windows_prefers_cmd(monkeypatch):
    monkeypatch.setattr(watcher, "_IS_WINDOWS", True)
    monkeypatch.setattr(watcher.shutil, "which", _fake_which({
        "prism": r"C:\npm\prism",            # the sh script Python 3.12 returns
        "prism.cmd": r"C:\npm\prism.cmd",
    }))
    assert watcher.find_prism() == r"C:\npm\prism.cmd"


def test_find_prism_windows_falls_back_to_prism(monkeypatch):
    monkeypatch.setattr(watcher, "_IS_WINDOWS", True)
    monkeypatch.setattr(watcher.shutil, "which", _fake_which({"prism": r"C:\tools\prism.exe"}))
    assert watcher.find_prism() == r"C:\tools\prism.exe"


def test_find_prism_windows_none(monkeypatch):
    monkeypatch.setattr(watcher, "_IS_WINDOWS", True)
    monkeypatch.setattr(watcher.shutil, "which", _fake_which({}))
    assert watcher.find_prism() is None


def test_find_prism_posix_uses_prism(monkeypatch):
    monkeypatch.setattr(watcher, "_IS_WINDOWS", False)
    monkeypatch.setattr(watcher.shutil, "which", _fake_which({
        "prism": "/usr/local/bin/prism", "prism.cmd": "/nope/prism.cmd"}))
    assert watcher.find_prism() == "/usr/local/bin/prism"


def test_prism_start_on_windows_launches_prism_cmd(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "_IS_WINDOWS", True)
    monkeypatch.setattr(watcher.shutil, "which", _fake_which({
        "prism": r"C:\npm\prism", "prism.cmd": r"C:\npm\prism.cmd"}))
    monkeypatch.setattr(watcher.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    launched: list[list[str]] = []

    class FakePopen:
        pid = 1234

        def __init__(self, cmd, **kwargs):
            launched.append(cmd)
            assert kwargs.get("creationflags") == 0x200

        def poll(self):
            return None

    monkeypatch.setattr(watcher.subprocess, "Popen", FakePopen)
    spec = tmp_path / "openapi.yaml"
    spec.write_text("openapi: 3.0.3\n", encoding="utf-8")
    pm = PrismManager(spec, 4011)
    pm.start()
    try:
        assert launched == [[r"C:\npm\prism.cmd", "mock", str(spec), "--port", "4011",
                             "--host", "0.0.0.0", "--dynamic"]]
        assert pm.is_running()
    finally:
        pm.proc = None       # don't send CTRL_BREAK to a fake process
        pm._close_log()


# ---------- Prism without Prism ----------

def test_prism_missing_warns_and_does_not_crash(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(watcher.shutil, "which", lambda name: None)
    spec = tmp_path / "openapi.yaml"
    spec.write_text("openapi: 3.0.3\n", encoding="utf-8")
    pm = PrismManager(spec, 4999)
    pm.start()
    pm.restart()
    pm.stop()
    assert not pm.is_running()
    out = capsys.readouterr().out
    assert out.count("prism not found") == 1  # warned once, not on every restart


# ---------- end to end ----------

def test_end_to_end_append_changed_logs(tmp_path):
    log = tmp_path / "live.jsonl"
    out_dir = tmp_path / "output"
    spec_path, changes_path = out_dir / "openapi.yaml", out_dir / "changes.jsonl"
    prism = FakePrism()
    updates: list[int] = []
    stop = threading.Event()

    th = threading.Thread(target=watch, kwargs=dict(
        log_path=log, spec_path=spec_path, changes_path=changes_path, prism=prism,
        debounce_seconds=0.2, stop_event=stop, poll_interval=0.2,
        on_update=lambda spec, changes: updates.append(len(changes))), daemon=True)
    th.start()
    try:
        # 1. log file doesn't exist yet -> empty but valid spec, Prism started
        assert wait_for(lambda: spec_path.exists())
        assert load_spec(spec_path)["paths"] == {}
        assert wait_for(lambda: prism.starts == 1)

        # 2. baseline traffic appears
        with log.open("a", encoding="utf-8", newline="\n") as f:
            f.write(SAMPLE.read_text(encoding="utf-8"))
        assert wait_for(lambda: len((load_spec(spec_path) or {}).get("paths", {})) >= 5)
        assert wait_for(lambda: any(r["kind"] == "endpoint_added" for r in read_changes(changes_path)))
        time.sleep(0.5)  # let any trailing rebuild of the sample batch finish
        assert not [r for r in read_changes(changes_path) if r["breaking"]]  # sample replay: no alerts
        baseline_rows = len(read_changes(changes_path))
        restarts_before = prism.restarts

        # 3. changed traffic arrives line by line
        with log.open("a", encoding="utf-8", newline="\n") as f:
            for line in CHANGED.read_text(encoding="utf-8").splitlines(keepends=True):
                f.write(line)
                f.flush()
                time.sleep(0.01)

        def has_alerts() -> bool:
            rows = read_changes(changes_path)[baseline_rows:]
            return (any(r["kind"] == "endpoint_added" and r["method"] == "DELETE" for r in rows)
                    and any(r["breaking"] and r["location"] == "response.200.body.email" for r in rows))

        assert wait_for(has_alerts)
        spec = load_spec(spec_path)
        assert validate_spec(spec) == []
        assert "delete" in spec["paths"]["/users/{id}"]
        assert "full_name" in spec["paths"]["/users/{id}"]["get"]["responses"]["200"]["content"][
            "application/json"]["schema"]["properties"]
        assert prism.restarts > restarts_before
        assert updates
    finally:
        stop.set()
        th.join(timeout=10)
    assert not th.is_alive()
    assert prism.stops == 1  # stopped cleanly on exit
