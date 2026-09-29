"""Request-side change detection from traffic (TrafficStats):
new required request fields and removed request fields. Run from repo root: pytest -q"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import watcher  # noqa: E402
from watcher import TrafficStats, build_from_entries, diff_specs  # noqa: E402


def entry(request: dict | None, status: int = 201, response: dict | None = None) -> dict:
    return {"timestamp": "2026-09-29T09:00:00Z", "method": "POST", "path": "/applications", "query": {},
            "request_body": request, "status": status, "response_body": response or {"id": 1}, "headers": {}}


def posts(n: int, status: int = 201, **fields) -> list[dict]:
    return [entry({"name": "a", "dob": "1990-01-01", **fields}, status) for _ in range(n)]


def run_batches(*batches: list[dict]) -> list[list]:
    """Feed batches like ContractWatcher does; the changes of each batch after the first."""
    stats, entries, spec, out = TrafficStats(), [], None, []
    for batch in batches:
        entries += batch
        stats.add(batch)
        new = build_from_entries(entries)
        if spec is not None:
            out.append(diff_specs(spec, new, stats))
        spec = new
        stats.mark(new)
    return out


def flat(per_batch: list[list]) -> list:
    return [c for batch in per_batch for c in batch]


def kinds(changes) -> set[tuple[str, str, bool]]:
    return {(c.kind, c.location, c.breaking) for c in changes}


def test_constant():
    assert watcher.NEW_REQUIRED_STREAK == 10


# ---------- new required request field ----------

def test_new_field_in_every_request_becomes_required_breaking():
    per_batch = run_batches(posts(12), *[posts(1, contact={"name": "x", "phone": "1"}) for _ in range(15)])
    assert kinds(per_batch[0]) == {("field_added", "request.body.contact", False)}   # first sighting: additive
    required = [(i, c) for i, batch in enumerate(per_batch) for c in batch if c.kind == "became_required"]
    assert len(required) == 1                                                        # reported once, top-most only
    i, c = required[0]
    assert i == 9 and c.location == "request.body.contact" and c.breaking            # the 10th request with it
    assert "new required field" in c.detail


def test_new_required_field_within_one_batch_upgrades_field_added():
    (changes,) = run_batches(posts(12), posts(10, contact="x"))
    assert kinds(changes) == {("field_added", "request.body.contact", True)}
    assert "new required field" in changes[0].detail


def test_new_optional_field_is_never_required():
    mixed = [e for i in range(30) for e in (posts(1, note="n") if i % 3 else posts(1))]
    per_batch = run_batches(posts(12), *[[e] for e in mixed])
    assert not [c for c in flat(per_batch) if c.breaking]
    assert ("field_added", "request.body.note", False) in kinds(flat(per_batch))


def test_fields_seen_during_warm_up_are_baseline_not_new():
    per_batch = run_batches(posts(3, contact="x"), *[posts(1, contact="x") for _ in range(20)])
    assert flat(per_batch) == []


def test_failed_requests_do_not_count():
    """400s without the field don't break the streak; 400s with it don't build one."""
    per_batch = run_batches(posts(12), posts(3, contact="x"), posts(20, status=400),
                            *[posts(1, contact="x") for _ in range(7)])
    assert ("became_required", "request.body.contact", True) in kinds(flat(per_batch))
    per_batch = run_batches(posts(12), posts(30, status=400, contact="x"))
    assert not [c for c in flat(per_batch) if c.breaking]


# ---------- removed request field ----------

def test_required_request_field_missing_streak_is_removed_breaking():
    renamed = [entry({"name": "a", "birth": "1990-01-01"}) for _ in range(1)]
    per_batch = run_batches(posts(12), *[renamed for _ in range(12)])
    removed = [(i, c) for i, batch in enumerate(per_batch) for c in batch if c.kind == "field_removed"]
    assert len(removed) == 1
    i, c = removed[0]
    assert i == 9 and c.location == "request.body.dob" and c.breaking
    assert "successful requests" in c.detail
    assert ("became_optional", "request.body.dob", False) in kinds(per_batch[0])     # the soft signal first


def test_rename_detected_on_both_names():
    (changes,) = run_batches(posts(12), [entry({"name": "a", "birth": "1990"}) for _ in range(10)])
    assert kinds(changes) == {("field_removed", "request.body.dob", True),
                              ("field_added", "request.body.birth", True)}


def test_optional_request_field_going_missing_is_not_removed():
    mixed = [e for i in range(12) for e in (posts(1, note="n") if i % 2 else posts(1))]
    per_batch = run_batches(mixed, posts(15))
    assert not [c for c in flat(per_batch) if c.kind == "field_removed"]


def test_request_scope_does_not_show_in_warming_list():
    stats = TrafficStats()
    stats.add(posts(3))
    assert set(stats.warming()) == {("POST", "/applications", "201")}
