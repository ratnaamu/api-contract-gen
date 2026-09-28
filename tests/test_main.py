"""Tests for main.py (cmd_build). Run from repo root: pytest -q"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main import cmd_build  # noqa: E402
from spec_builder import load_spec, validate_spec  # noqa: E402

SAMPLE = ROOT / "sample_logs.jsonl"


def test_build_writes_valid_spec(tmp_path, capsys):
    out = tmp_path / "out" / "openapi.yaml"

    rc = cmd_build(str(SAMPLE), str(out))

    assert rc == 0
    assert out.is_file()

    spec = load_spec(out)
    assert spec is not None
    assert spec["openapi"] == "3.0.3"
    assert spec["paths"], "expected at least one endpoint in the spec"
    assert validate_spec(spec) == []

    summary = capsys.readouterr().out
    assert "log entries read" in summary
    assert "lines skipped" in summary
    assert "endpoints found" in summary
    assert "validation       : OK" in summary
    assert str(out) in summary


def test_build_missing_log_file_returns_1(tmp_path):
    out = tmp_path / "openapi.yaml"
    assert cmd_build(str(tmp_path / "nope.jsonl"), str(out)) == 1
    assert not out.exists()
