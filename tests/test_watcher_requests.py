"""Traffic-based change detection (TrafficStats): new request fields (required = BREAKING, optional = ok),
removed fields and rename detection, in requests and responses. Run from repo root: pytest -q"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import models  # noqa: E402
import watcher  # noqa: E402
from watcher import TrafficStats, build_from_entries, diff_specs  # noqa: E402


def entry(request: dict | None, status: int = 201, response: dict | None = None,
          method: str = "POST", path: str = "/applications") -> dict:
    return {"timestamp": "2026-09-29T09:00:00Z", "method": method, "path": path, "query": {},
            "request_body": request, "status": status, "response_body": response or {"id": 1}, "headers": {}}


def posts(n: int, status: int = 201, **fields) -> list[dict]:
    return [entry({"name": "a", "dob": "1990-01-01", **fields}, status) for _ in range(n)]


def gets(n: int, **fields) -> list[dict]:
    return [entry(None, 200, {"id": 1, **fields}, "GET", "/applications/PA-000001") for _ in range(n)]


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


def one_by_one(first: list[dict], rest: list[dict]) -> list[list]:
    return run_batches(first, *[[e] for e in rest])


def flat(per_batch: list[list]) -> list:
    return [c for batch in per_batch for c in batch]


def kinds(changes) -> set[tuple[str, str, bool]]:
    return {(c.kind, c.location, c.breaking) for c in changes}


def test_constants_and_kind():
    assert watcher.NEW_REQUIRED_STREAK == 10 and watcher.RENAME_WINDOW == 2
    assert "field_renamed" in models.ChangeKind.__args__


# ---------- 1. new request fields: required -> BREAKING, optional -> ok ----------

def test_new_required_request_field_is_one_breaking_change():
    per_batch = one_by_one(posts(12), posts(15, contact={"name": "x", "phone": "1"}))
    changes = flat(per_batch)
    assert kinds(changes) == {("field_added", "request.body.contact", True)}      # one change, top-most only
    i = next(i for i, b in enumerate(per_batch) if b)
    assert i == 9                                                                  # after 10 requests with it
    assert "new required field" in changes[0].detail
    assert all(not b for b in per_batch[:9])                                       # nothing misleading before


def test_new_required_request_field_within_one_batch():
    (changes,) = run_batches(posts(12), posts(10, contact="x"))
    assert kinds(changes) == {("field_added", "request.body.contact", True)}


def test_new_optional_request_field_is_ok():
    mixed = [e for i in range(20) for e in (posts(1, note="n") if i % 3 else posts(1))]
    changes = flat(one_by_one(posts(12), mixed))
    assert kinds(changes) == {("field_added", "request.body.note", False)}
    assert changes[0].detail == "new optional field"


def test_optional_request_field_reported_as_soon_as_a_request_lacks_it():
    per_batch = one_by_one(posts(12), posts(3, note="n") + posts(1))
    assert [kinds(b) for b in per_batch] == [set(), set(), set(), {("field_added", "request.body.note", False)}]


def test_new_response_fields_stay_ok_and_are_immediate():
    (changes,) = run_batches(gets(12), gets(1, photo_url="https://x"))
    assert kinds(changes) == {("field_added", "response.200.body.photo_url", False)}


def test_fields_seen_during_warm_up_are_baseline_not_new():
    assert flat(one_by_one(posts(3, contact="x"), posts(20, contact="x"))) == []


def test_failed_requests_do_not_count():
    """400s without the field don't break the streak; 400s with it don't build one."""
    changes = flat(run_batches(posts(12), posts(3, contact="x"), posts(20, status=400),
                               *[posts(1, contact="x") for _ in range(7)]))
    assert ("field_added", "request.body.contact", True) in kinds(changes)
    assert not [c for c in flat(run_batches(posts(12), posts(30, status=400, contact="x"))) if c.breaking]


# ---------- 2. rename detection ----------

def renamed_posts(n: int) -> list[dict]:
    return [entry({"name": "a", "birth_date": "1990-01-01"}) for _ in range(n)]


def test_request_rename_is_one_breaking_change():
    per_batch = one_by_one(posts(12), renamed_posts(15))
    changes = flat(per_batch)
    assert kinds(changes) == {("field_renamed", "request.body.dob", True)}
    assert changes[0].detail == "dob appears to be renamed to birth_date"
    i = next(i for i, b in enumerate(per_batch) if b)
    assert i == 9                                   # once the old name has been missing 10 times
    assert all(not b for b in per_batch[:9])        # no became_optional / field_added noise before


def test_response_rename_is_one_breaking_change():
    old = [entry(None, 200, {"id": 1, "date_of_birth": "1990-01-01"}, "GET", "/applications/PA-000001")] * 12
    new = [entry(None, 200, {"id": 1, "birth_date": "1990-01-01"}, "GET", "/applications/PA-000001")] * 15
    changes = flat(one_by_one(old, new))
    assert kinds(changes) == {("field_renamed", "response.200.body.date_of_birth", True)}
    assert changes[0].detail == "date_of_birth appears to be renamed to birth_date"


def test_rename_in_one_big_batch():
    (changes,) = run_batches(posts(12), renamed_posts(10))
    assert kinds(changes) == {("field_renamed", "request.body.dob", True)}


def test_different_type_is_not_a_rename():
    """dob (string) disappears while age (integer) appears: a removal and a new field."""
    changes = flat(one_by_one(posts(12), [entry({"name": "a", "age": 36}) for _ in range(12)]))
    assert kinds(changes) == {("became_optional", "request.body.dob", False),     # first absence (ok) ...
                              ("field_removed", "request.body.dob", True),        # ... then removed
                              ("field_added", "request.body.age", True)}


def test_not_at_the_same_time_is_not_a_rename():
    """The new field appears long after the old one started missing."""
    later = [entry({"name": "a"}) for _ in range(6)] + renamed_posts(10)
    changes = flat(one_by_one(posts(12), later))
    assert ("field_removed", "request.body.dob", True) in kinds(changes)
    assert not [c for c in changes if c.kind == "field_renamed"]


def test_different_parent_is_not_a_rename():
    moved = [entry({"name": "a", "person": {"birth": "1990"}}) for _ in range(12)]
    changes = flat(one_by_one(posts(12), moved))
    assert ("field_removed", "request.body.dob", True) in kinds(changes)
    assert not [c for c in changes if c.kind == "field_renamed"]


def test_old_field_returning_cancels_the_rename():
    """dob goes missing while birth_date appears, then dob comes back: no rename; the held changes
    come out as plain additive/optional ones."""
    back = renamed_posts(4) + posts(3, birth_date="1990-01-01") + posts(10, birth_date="1990-01-01")
    changes = flat(one_by_one(posts(12), back))
    assert not [c for c in changes if c.kind in ("field_renamed", "field_removed")]
    assert ("became_optional", "request.body.dob", False) in kinds(changes)
    assert not [c for c in changes if c.breaking and c.kind != "field_added"]


def test_several_changes_at_once_pair_by_shared_words():
    """name -> full_name and email vanishes while user_ref appears (all strings): only the pair that shares
    a word is a rename; the other two are a removal and an addition."""
    before = [entry(None, 200, {"name": "a", "email": "e"}, "GET", "/u/1") for _ in range(12)]
    after = [entry(None, 200, {"full_name": "a", "user_ref": "r"}, "GET", "/u/1") for _ in range(12)]
    got = kinds(flat(one_by_one(before, after)))
    assert ("field_renamed", "response.200.body.name", True) in got
    assert ("field_removed", "response.200.body.email", True) in got
    assert ("field_added", "response.200.body.user_ref", False) in got
    assert not [k for k in got if k[0] == "field_renamed" and k[1].endswith("email")]


def test_single_candidate_needs_no_shared_word():
    """Exactly one field gone and one of the same type new at the same time: that is the rename."""
    changes = flat(one_by_one(posts(12), [entry({"name": "a", "born": "1990"}) for _ in range(12)]))
    assert kinds(changes) == {("field_renamed", "request.body.dob", True)}


def test_nested_rename():
    before = [entry(None, 200, {"id": 1, "address": {"zip": "1", "city": "x"}}, "GET", "/a/PA-000001")] * 12
    after = [entry(None, 200, {"id": 1, "address": {"postal_code": "1", "city": "x"}}, "GET", "/a/PA-000001")] * 12
    changes = flat(one_by_one(before, after))
    assert kinds(changes) == {("field_renamed", "response.200.body.address.zip", True)}
    assert changes[0].detail == "zip appears to be renamed to postal_code (in address)"


def test_renamed_object_reports_only_the_object():
    before = [entry(None, 200, {"id": 1, "home": {"city": "x"}}, "GET", "/a/PA-000001")] * 12
    after = [entry(None, 200, {"id": 1, "home_address": {"city": "x"}}, "GET", "/a/PA-000001")] * 12
    assert kinds(flat(one_by_one(before, after))) == {("field_renamed", "response.200.body.home", True)}


def test_plain_removal_still_reported():
    changes = flat(one_by_one(posts(12), [entry({"name": "a"}) for _ in range(12)]))
    assert kinds(changes) == {("became_optional", "request.body.dob", False), ("field_removed", "request.body.dob", True)}
    assert "successful requests" in next(c for c in changes if c.kind == "field_removed").detail


def test_rename_reported_once():
    per_batch = one_by_one(posts(12), renamed_posts(40))
    assert len([c for c in flat(per_batch) if c.kind == "field_renamed"]) == 1


def test_request_scope_does_not_show_in_warming_list():
    stats = TrafficStats()
    stats.add(posts(3))
    assert set(stats.warming()) == {("POST", "/applications", "201")}
