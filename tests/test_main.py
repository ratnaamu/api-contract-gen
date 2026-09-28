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


# ---------- cmd_watch --fresh ----------

def _stub_watch(monkeypatch) -> list[dict]:
    """Replace watcher.watch so cmd_watch returns immediately; record what it saw."""
    import watcher
    calls: list[dict] = []

    def fake_watch(log_path, spec_path, changes_path, prism):
        calls.append({"spec_exists": Path(spec_path).exists(),
                      "changes_exists": Path(changes_path).exists(), "prism": prism})

    monkeypatch.setattr(watcher, "watch", fake_watch)
    return calls


def _leftovers(tmp_path: Path) -> tuple[Path, Path]:
    out = tmp_path / "output"
    out.mkdir()
    spec, changes = out / "openapi.yaml", out / "changes.jsonl"
    spec.write_text("openapi: 3.0.3\n", encoding="utf-8")
    changes.write_text('{"kind": "endpoint_removed"}\n', encoding="utf-8")
    return spec, changes


def test_watch_fresh_clears_spec_and_changes(tmp_path, monkeypatch):
    calls = _stub_watch(monkeypatch)
    spec, changes = _leftovers(tmp_path)
    from main import cmd_watch
    assert cmd_watch(str(tmp_path / "live.jsonl"), str(spec), 4010, True, fresh=True) == 0
    assert calls == [{"spec_exists": False, "changes_exists": False, "prism": None}]


def test_watch_without_fresh_keeps_files(tmp_path, monkeypatch):
    calls = _stub_watch(monkeypatch)
    spec, changes = _leftovers(tmp_path)
    from main import cmd_watch
    assert cmd_watch(str(tmp_path / "live.jsonl"), str(spec), 4010, True) == 0
    assert calls[0]["spec_exists"] and calls[0]["changes_exists"]


def test_watch_fresh_with_nothing_to_clear(tmp_path, monkeypatch):
    calls = _stub_watch(monkeypatch)
    from main import cmd_watch
    spec = tmp_path / "output" / "openapi.yaml"
    assert cmd_watch(str(tmp_path / "live.jsonl"), str(spec), 4010, True, fresh=True) == 0
    assert len(calls) == 1


def test_fresh_flag_is_wired_through_cli(monkeypatch):
    import main
    seen: list[tuple] = []
    monkeypatch.setattr(main, "cmd_watch", lambda *a: seen.append(a) or 0)
    for argv, expected in ((["watch", "live.jsonl", "--fresh", "--no-prism"], True),
                           (["watch", "live.jsonl"], False)):
        monkeypatch.setattr(sys, "argv", ["main.py", *argv])
        try:
            main.main()
        except SystemExit as e:
            assert e.code == 0
        assert seen[-1][-1] is expected
