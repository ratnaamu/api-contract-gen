"""Tests for noise.py (A7). Run from repo root: pytest -q"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from noise import MUTATION_KINDS, Mutator, mutate_file  # noqa: E402
from parser import parse_line  # noqa: E402

ENTRY = {"timestamp": "t", "method": "GET", "path": "/x", "query": {}, "status": 200,
        "request_body": None, "response_body": {"id": 1, "name": "a", "email": "a@b.co"}, "headers": {}}


def all_rate(kind: str, rate: float = 1.0) -> Mutator:
    """A Mutator with only `kind` enabled (rate 1.0), everything else off."""
    overrides = {k: (rate if k == kind else 0.0) for k in MUTATION_KINDS}
    return Mutator(seed=1, rate=0.0, **overrides)


def test_drop_field_removes_a_key():
    m = all_rate("drop_field")
    out = m.mutate_entry(ENTRY)
    assert set(out["response_body"]) < set(ENTRY["response_body"])
    assert m.applied["drop_field"] == 1


def test_null_field_sets_none_not_remove():
    m = all_rate("null_field")
    out = m.mutate_entry(ENTRY)
    assert None in out["response_body"].values()
    assert set(out["response_body"]) == set(ENTRY["response_body"])  # key stays, only the value changes


def test_casing_changes_a_key_spelling():
    m = all_rate("casing")
    out = m.mutate_entry(ENTRY)
    assert set(out["response_body"].keys()) != set(ENTRY["response_body"].keys())
    assert {k.lower() for k in out["response_body"]} == {k.lower() for k in ENTRY["response_body"]}


def test_version_mix_renames_field_with_v2_suffix():
    m = all_rate("version_mix")
    out = m.mutate_entry(ENTRY)
    assert any(k.endswith("_v2") for k in out["response_body"])


def test_truncate_replaces_body_with_a_string():
    m = all_rate("truncate")
    out = m.mutate_entry(ENTRY)
    assert isinstance(out["response_body"], str)
    assert len(out["response_body"]) < len(json.dumps(ENTRY["response_body"]))


def test_status_text_appends_a_phrase():
    m = all_rate("status_text")
    out = m.mutate_entry(ENTRY)
    assert out["status"] != 200
    assert str(200) in str(out["status"]) and "OK" in str(out["status"])


def test_corrupt_json_shortens_the_line():
    m = all_rate("corrupt_json")
    line = json.dumps(ENTRY)
    corrupted = m.maybe_corrupt_line(line)
    assert corrupted is not None and len(corrupted) < len(line)
    assert parse_line(corrupted) is None  # actually broken, not accidentally still-valid JSON


def test_zero_rate_never_mutates():
    m = Mutator(seed=1, rate=0.0)
    out = m.mutate_entry(ENTRY)
    assert out == ENTRY
    assert all(n == 0 for n in m.applied.values())


def test_same_seed_is_deterministic():
    a = Mutator(seed=7, rate=0.3).mutate_entry(ENTRY)
    b = Mutator(seed=7, rate=0.3).mutate_entry(ENTRY)
    assert a == b


def test_different_seeds_can_differ():
    results = {json.dumps(Mutator(seed=s, rate=0.5).mutate_entry(ENTRY), sort_keys=True) for s in range(10)}
    assert len(results) > 1


# ---------- mutate_file ----------

def test_mutate_file_preserves_line_count(tmp_path):
    src = tmp_path / "clean.jsonl"
    src.write_text("\n".join(json.dumps(ENTRY) for _ in range(20)) + "\n", encoding="utf-8")
    dst = tmp_path / "noisy.jsonl"
    mutate_file(src, dst, seed=1, rate=0.3)
    assert len(dst.read_text(encoding="utf-8").strip().splitlines()) == 20


def test_mutate_file_leaves_blank_lines_and_bad_input_alone(tmp_path):
    src = tmp_path / "clean.jsonl"
    src.write_text(json.dumps(ENTRY) + "\n\nalready garbage\n", encoding="utf-8")
    dst = tmp_path / "noisy.jsonl"
    mutate_file(src, dst, seed=1, rate=0.0)  # rate 0 -> only pass-through behavior is exercised
    lines = dst.read_text(encoding="utf-8").splitlines()
    assert lines == [json.dumps(ENTRY), "", "already garbage"]


def test_mutate_file_reports_applied_counts(tmp_path):
    src = tmp_path / "clean.jsonl"
    src.write_text("\n".join(json.dumps(ENTRY) for _ in range(50)) + "\n", encoding="utf-8")
    dst = tmp_path / "noisy.jsonl"
    applied = mutate_file(src, dst, seed=1, rate=0.0, drop_field=1.0)
    assert applied["drop_field"] == 50
    assert all(n == 0 for k, n in applied.items() if k != "drop_field")
