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
        calls.append({"log_exists": Path(log_path).exists(), "spec_exists": Path(spec_path).exists(),
                      "changes_exists": Path(changes_path).exists(), "prism": prism})

    monkeypatch.setattr(watcher, "watch", fake_watch)
    return calls


def _leftovers(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A previous demo run: a filled live log, a spec and some changes."""
    out = tmp_path / "output"
    out.mkdir()
    log, spec, changes = tmp_path / "live_logs.jsonl", out / "openapi.yaml", out / "changes.jsonl"
    log.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    spec.write_text("openapi: 3.0.3\n", encoding="utf-8")
    changes.write_text('{"kind": "endpoint_removed"}\n', encoding="utf-8")
    return log, spec, changes


def test_watch_fresh_clears_log_spec_and_changes(tmp_path, monkeypatch):
    calls = _stub_watch(monkeypatch)
    log, spec, changes = _leftovers(tmp_path)
    from main import cmd_watch
    assert cmd_watch(str(log), str(spec), 4010, True, fresh=True) == 0
    assert calls == [{"log_exists": False, "spec_exists": False, "changes_exists": False, "prism": None}]


def test_watch_without_fresh_keeps_files(tmp_path, monkeypatch):
    calls = _stub_watch(monkeypatch)
    log, spec, changes = _leftovers(tmp_path)
    from main import cmd_watch
    assert cmd_watch(str(log), str(spec), 4010, True) == 0
    assert calls[0]["log_exists"] and calls[0]["spec_exists"] and calls[0]["changes_exists"]
    assert log.stat().st_size > 0


def test_watch_fresh_baseline_is_zero_entries(tmp_path, monkeypatch, capsys):
    """Real watcher, stopped right after start-up: the baseline must be built from 0 entries."""
    import watcher
    from main import cmd_watch
    log, spec, changes = _leftovers(tmp_path)
    seen = {}
    real_init = watcher.ContractWatcher.initialize

    def init_and_stop(self):
        real_init(self)
        seen["entries"] = len(self.entries)
        raise KeyboardInterrupt  # watch() handles Ctrl+C and returns

    monkeypatch.setattr(watcher.ContractWatcher, "initialize", init_and_stop)
    assert cmd_watch(str(log), str(spec), 4010, True, fresh=True) == 0
    assert seen["entries"] == 0
    assert "baseline: 0 entries" in capsys.readouterr().out
    assert not changes.exists()


def test_watch_fresh_empties_log_that_cannot_be_deleted(tmp_path, monkeypatch, capsys):
    """Windows refuses to delete a file another process has open (e.g. replay_logs.py): empty it instead."""
    calls = _stub_watch(monkeypatch)
    log, spec, changes = _leftovers(tmp_path)
    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if self == log:
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    from main import cmd_watch
    assert cmd_watch(str(log), str(spec), 4010, True, fresh=True) == 0
    assert log.exists() and log.stat().st_size == 0
    assert calls[0]["spec_exists"] is False
    assert "emptied" in capsys.readouterr().out


def test_watch_fresh_refuses_to_delete_input_data(monkeypatch, capsys):
    calls = _stub_watch(monkeypatch)
    from main import cmd_watch
    before = SAMPLE.stat().st_size
    assert cmd_watch(str(SAMPLE), "unused/openapi.yaml", 4010, True, fresh=True) == 2
    assert SAMPLE.exists() and SAMPLE.stat().st_size == before
    assert calls == []
    assert "input data" in capsys.readouterr().out


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
