"""Tests for generate_logs.py's B6 additions (--error-rate/--holdout-errors). Run from repo root: pytest -q

Not a full test of the generator itself (untested before this change too) — scoped to what's new.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generate_logs import downsample_errors, generate, main  # noqa: E402


def entry(status: int) -> dict:
    return {"timestamp": "t", "method": "GET", "path": "/x", "query": {}, "request_body": None,
            "status": status, "response_body": {}, "headers": {}}


def test_downsample_errors_keeps_all_2xx():
    entries = [entry(200) for _ in range(20)]
    out = downsample_errors(entries, rate=0.0, seed=1)
    assert len(out) == 20


def test_downsample_errors_drops_most_errors_at_low_rate():
    entries = [entry(200) for _ in range(50)] + [entry(404) for _ in range(50)]
    out = downsample_errors(entries, rate=0.1, seed=1)
    kept_errors = sum(1 for e in out if e["status"] >= 400)
    assert 0 < kept_errors < 50  # some survive (rate > 0), most don't (rate is low)
    assert sum(1 for e in out if e["status"] == 200) == 50  # 2xx entirely unaffected


def test_downsample_errors_rate_1_keeps_everything():
    entries = [entry(200) for _ in range(10)] + [entry(500) for _ in range(10)]
    assert downsample_errors(entries, rate=1.0, seed=1) == entries


def test_downsample_errors_is_deterministic():
    entries = [entry(404) for _ in range(30)]
    a = downsample_errors(entries, rate=0.4, seed=7)
    b = downsample_errors(entries, rate=0.4, seed=7)
    assert a == b


def test_holdout_errors_writes_full_traffic_before_downsampling(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "training.jsonl"
    holdout = tmp_path / "holdout.jsonl"
    monkeypatch.setattr(sys, "argv", ["generate_logs.py", "-n", "100", "-o", str(out),
                                      "--error-rate", "0.0", "--holdout-errors", str(holdout)])
    main()
    holdout_lines = holdout.read_text(encoding="utf-8").splitlines()
    training_lines = out.read_text(encoding="utf-8").splitlines()
    assert len(holdout_lines) == 100
    holdout_errors = sum(1 for line in holdout_lines if json.loads(line)["status"] >= 400)
    training_errors = sum(1 for line in training_lines if json.loads(line)["status"] >= 400)
    assert holdout_errors > 0  # the generator's own traffic mix includes some errors
    assert training_errors == 0  # --error-rate 0.0 dropped every one of them from the training file
    assert len(training_lines) == len(holdout_lines) - holdout_errors


def test_without_error_rate_output_is_unaffected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "logs.jsonl"
    monkeypatch.setattr(sys, "argv", ["generate_logs.py", "-n", "50", "-o", str(out)])
    main()
    assert len(out.read_text(encoding="utf-8").splitlines()) == 50
