"""Tests for `main.py demo` (run_demo) and replay_logs.replay. Prism is never needed."""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import watcher  # noqa: E402
from replay_logs import replay  # noqa: E402

SAMPLE = ROOT / "sample_logs.jsonl"
CHANGED = ROOT / "changed_logs.jsonl"

pytestmark = pytest.mark.slow  # spawns real threads/servers; run everything else with -m "not slow"


# ---------- helpers ----------

class FakePrism:
    def __init__(self) -> None:
        self.starts = self.stops = self.restarts = 0
        self.running = False

    def start(self) -> None:
        self.starts += 1
        self.running = True

    def stop(self) -> None:
        self.stops += 1
        self.running = False

    def restart(self) -> None:
        self.restarts += 1
        self.running = True

    def is_running(self) -> bool:
        return self.running


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(cond, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def get_json(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as r:
        return json.loads(r.read())


def rows(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def removed_alerts(p: Path) -> int:
    """Breaking field_removed / field_renamed rows: only the changed-logs replay produces these
    (never the leftovers, whose only row is an endpoint_removed)."""
    return sum(1 for r in rows(p) if r.get("breaking") and r.get("kind") in ("field_removed", "field_renamed"))


def leftovers(tmp_path: Path) -> tuple[Path, Path, Path]:
    """What a previous demo run leaves behind."""
    out = tmp_path / "output"
    out.mkdir()
    log, spec, changes = tmp_path / "live_logs.jsonl", out / "openapi.yaml", out / "changes.jsonl"
    log.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    spec.write_text("openapi: 3.0.3\ninfo: {title: old, version: '1'}\npaths: {}\n", encoding="utf-8")
    changes.write_text('{"kind": "endpoint_removed", "breaking": true}\n', encoding="utf-8")
    return log, spec, changes


def demo_kwargs(tmp_path: Path, **kw) -> dict:
    log, spec, _ = leftovers(tmp_path)
    base = dict(log_path=log, spec_path=spec, port=free_port(), prism_port=free_port(),
                sample_delay=0, changed_delay=0, auto_pause=0.1, debounce=0.2)
    base.update(kw)
    return base


def start_demo(**kw) -> tuple[threading.Thread, dict]:
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(rc=main.run_demo(**kw)), daemon=True)
    t.start()
    return t, result


def demo_threads_alive() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name in ("watcher", "dashboard") and t.is_alive()]


# ---------- the full run ----------

def test_auto_demo_end_to_end(tmp_path, capsys):
    stop, prism = threading.Event(), FakePrism()
    kw = demo_kwargs(tmp_path, auto=True, prism=prism, stop_event=stop)
    changes = Path(kw["spec_path"]).parent / "changes.jsonl"
    printed: list[str] = []

    def console() -> str:
        printed.append(capsys.readouterr().out)
        return "".join(printed)

    t, result = start_demo(**kw)
    try:
        # step 7 reached: the changed logs produced breaking alerts, and the dashboard serves them live
        assert wait_for(lambda: "Demo running" in console())
        assert removed_alerts(changes) >= 3
        summary = get_json(kw["port"], "/api/summary")
        assert summary["endpoint_count"] == 8          # 7 + DELETE /users/{id}
        assert summary["breaking_changes"] >= 3
        spec = get_json(kw["port"], "/api/spec")
        assert "delete" in spec["paths"]["/users/{id}"]
        assert t.is_alive(), "step 7: keeps running until Ctrl+C"
    finally:
        stop.set()
        t.join(timeout=30)
    assert not t.is_alive()
    assert result["rc"] == 0
    assert demo_threads_alive() == []
    assert prism.starts == 1 and prism.restarts > 0 and not prism.is_running()
    assert not main._port_in_use(kw["port"]), "dashboard port released"
    out = console()
    assert out.index("Demo running") < out.index("stopping demo") < out.index("demo stopped.")
    # the leftover breaking row from the "previous run" is gone: clean start
    assert rows(changes)[0]["kind"] == "endpoint_added"


def test_clean_start_baseline_is_zero_entries(tmp_path, capsys):
    stop = threading.Event()
    seen = {}

    def pause(prompt: str) -> None:
        seen["out"] = capsys.readouterr().out
        stop.set()

    kw = demo_kwargs(tmp_path, prism=FakePrism(), pause=pause, stop_event=stop)
    assert main.run_demo(**kw) == 0
    out = seen["out"]
    assert "removed" in out and "live_logs.jsonl" in out
    assert "baseline: 0 entries" in out
    assert f"http://localhost:{kw['port']}" in out
    assert not Path(kw["log_path"]).exists()                      # deleted, nothing replayed yet


def test_interactive_steps_happen_in_order(tmp_path):
    stop = threading.Event()
    prompts: list[tuple[str, dict]] = []
    kw = demo_kwargs(tmp_path, prism=FakePrism(), stop_event=stop)
    spec_path, log = Path(kw["spec_path"]), Path(kw["log_path"])
    changes = spec_path.parent / "changes.jsonl"

    def pause(prompt: str) -> None:
        import yaml
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        prompts.append((prompt, {
            "endpoints": sum(len(item) for item in spec["paths"].values()),  # operations, not paths
            "log_lines": len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0,
            "breaking": sum(r["breaking"] for r in rows(changes)),
        }))

    kw["pause"] = pause
    t, result = start_demo(**kw)
    try:
        assert wait_for(lambda: removed_alerts(changes) >= 3)
    finally:
        stop.set()
        t.join(timeout=30)
    assert result["rc"] == 0
    assert [p for p, _ in prompts] == [main.START_PROMPT, main.CHANGE_PROMPT]
    assert main.CHANGE_PROMPT == "Press Enter to introduce the API change"
    # before step 4: empty; before step 6: all 200 sample lines in, 7 endpoints, no breaking change yet
    assert prompts[0][1] == {"endpoints": 0, "log_lines": 0, "breaking": 0}
    assert prompts[1][1] == {"endpoints": 7, "log_lines": 200, "breaking": 0}
    assert len(log.read_text(encoding="utf-8").splitlines()) == 220


def test_auto_mode_never_waits_for_enter(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("--auto must not read stdin"))
    stop = threading.Event()
    kw = demo_kwargs(tmp_path, auto=True, prism=FakePrism(), stop_event=stop)
    changes = Path(kw["spec_path"]).parent / "changes.jsonl"
    t, result = start_demo(**kw)
    try:
        assert wait_for(lambda: removed_alerts(changes) >= 3)
    finally:
        stop.set()
        t.join(timeout=30)
    assert result["rc"] == 0


def test_enter_pause_uses_input_and_tolerates_closed_stdin(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("builtins.input", lambda *a: calls.append(1) or "")
    main._enter_pause(main.CHANGE_PROMPT)
    assert calls == [1] and main.CHANGE_PROMPT in capsys.readouterr().out

    def eof(*a):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    main._enter_pause(main.START_PROMPT)  # no exception: continue as if Enter was pressed


# ---------- Ctrl+C ----------

@pytest.mark.parametrize("at", [main.START_PROMPT, main.CHANGE_PROMPT])
def test_ctrl_c_at_a_prompt_stops_everything(tmp_path, at, capsys):
    prism = FakePrism()

    def pause(prompt: str) -> None:
        if prompt == at:
            raise KeyboardInterrupt

    kw = demo_kwargs(tmp_path, prism=prism, pause=pause)
    assert main.run_demo(**kw) == 0
    assert demo_threads_alive() == []
    assert prism.stops >= 1 and not prism.is_running()
    assert not main._port_in_use(kw["port"])
    out = capsys.readouterr().out
    assert "stopping demo" in out and "demo stopped." in out
    log = Path(kw["log_path"])
    if at == main.START_PROMPT:
        assert not log.exists()
    else:
        assert len(log.read_text(encoding="utf-8").splitlines()) == 200  # changed logs never replayed


def test_ctrl_c_during_replay_stops_replay(tmp_path):
    stop = threading.Event()
    replaying = threading.Event()
    kw = demo_kwargs(tmp_path, prism=FakePrism(), pause=lambda p: replaying.set(), stop_event=stop,
                     sample_delay=0.05)
    log = Path(kw["log_path"])
    t, result = start_demo(**kw)
    assert wait_for(replaying.is_set)                      # leftover log already deleted by now
    assert wait_for(lambda: log.exists() and len(log.read_text(encoding="utf-8").splitlines()) >= 5)
    stop.set()                               # like Ctrl+C in the middle of step 4
    t.join(timeout=30)
    assert result["rc"] == 0
    n = len(log.read_text(encoding="utf-8").splitlines())
    time.sleep(0.3)
    assert n < 200 and len(log.read_text(encoding="utf-8").splitlines()) == n  # replay really stopped
    assert demo_threads_alive() == []


# ---------- without Prism installed ----------

def test_demo_without_prism_installed_keeps_going(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(watcher.shutil, "which", lambda name: None)
    stop = threading.Event()
    kw = demo_kwargs(tmp_path, auto=True, stop_event=stop)   # real PrismManager, but no prism on PATH
    changes = Path(kw["spec_path"]).parent / "changes.jsonl"
    t, result = start_demo(**kw)
    try:
        assert wait_for(lambda: removed_alerts(changes) >= 3)
    finally:
        stop.set()
        t.join(timeout=30)
    assert result["rc"] == 0
    out = capsys.readouterr().out
    assert "prism not found" in out
    assert "Prism: not running" in out


def test_no_prism_flag(tmp_path, capsys):
    stop = threading.Event()
    kw = demo_kwargs(tmp_path, use_prism=False, pause=lambda p: stop.set(), stop_event=stop)
    assert main.run_demo(**kw) == 0
    assert "Prism: not running" in capsys.readouterr().out


# ---------- preflight: refuse to start (and delete nothing) ----------

def _untouched(log: Path, spec: Path, changes: Path) -> bool:
    return log.stat().st_size > 0 and spec.exists() and changes.exists()


def test_missing_source_logs(tmp_path, capsys):
    kw = demo_kwargs(tmp_path, prism=FakePrism(), sample_path=tmp_path / "nope.jsonl")
    assert main.run_demo(**kw) == 1
    assert "generate_logs.py --changed" in capsys.readouterr().out
    spec = Path(kw["spec_path"])
    assert _untouched(Path(kw["log_path"]), spec, spec.parent / "changes.jsonl")


def test_dashboard_port_in_use(tmp_path, capsys):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        kw = demo_kwargs(tmp_path, prism=FakePrism(), port=busy.getsockname()[1])
        assert main.run_demo(**kw) == 1
    assert "already in use" in capsys.readouterr().out
    spec = Path(kw["spec_path"])
    assert _untouched(Path(kw["log_path"]), spec, spec.parent / "changes.jsonl")
    assert demo_threads_alive() == []


def test_prism_port_in_use(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(watcher, "find_prism", lambda: "/fake/prism")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        kw = demo_kwargs(tmp_path, prism_port=busy.getsockname()[1])  # real PrismManager, "installed"
        assert main.run_demo(**kw) == 1
    assert "(Prism) is already in use" in capsys.readouterr().out


def test_refuses_to_use_input_data_as_live_log(tmp_path, capsys):
    before = SAMPLE.stat().st_size
    kw = demo_kwargs(tmp_path, prism=FakePrism(), log_path=SAMPLE)
    assert main.run_demo(**kw) == 2
    assert SAMPLE.stat().st_size == before
    assert "input data" in capsys.readouterr().out


# ---------- CLI ----------

def test_demo_cli_wiring(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "run_demo", lambda *a, **kw: seen.append((a, kw)) or 0)
    for argv, auto, no_prism in ((["demo"], False, False), (["demo", "--auto", "--no-prism", "--port", "8123"], True, True)):
        monkeypatch.setattr(sys, "argv", ["main.py", *argv])
        with pytest.raises(SystemExit) as e:
            main.main()
        assert e.value.code == 0
        a, kw = seen[-1]
        assert kw["auto"] is auto and kw["use_prism"] is (not no_prism)
        assert kw["sample_delay"] == 0.05 and kw["changed_delay"] == 0.3
        assert kw["live"] is True and kw["drift"] is True  # the default demo: passport service + drift
    assert seen[-1][1]["port"] == 8123
    assert seen[0][0] == ("live_logs.jsonl", "output/openapi.yaml")
    for argv, live, drift in ((["demo", "--replay"], False, False), (["demo", "--live"], True, False),
                              (["demo", "--drift-interval", "20", "--llm-refine"], True, True)):
        monkeypatch.setattr(sys, "argv", ["main.py", *argv])
        with pytest.raises(SystemExit):
            main.main()
        kw = seen[-1][1]
        assert kw["live"] is live and kw["drift"] is drift
    assert kw["drift_interval"] == 20 and kw["llm_refine"] is True
    monkeypatch.setattr(sys, "argv", ["main.py", "demo", "--replay", "--live"])
    with pytest.raises(SystemExit) as e:
        main.main()
    assert e.value.code == 2  # mutually exclusive


# ---------- replay_logs.replay ----------

def test_replay_appends_lines_with_newlines(tmp_path):
    src, dst = tmp_path / "src.jsonl", tmp_path / "dst.jsonl"
    src.write_text('{"a": 1}\n\n{"a": 2}\n{"a": 3}', encoding="utf-8")  # blank line + no final newline
    dst.write_text('{"old": 0}\n', encoding="utf-8")
    seen = []
    assert replay(src, dst, 0, on_line=seen.append) == 3
    assert dst.read_text(encoding="utf-8") == '{"old": 0}\n{"a": 1}\n{"a": 2}\n{"a": 3}\n'
    assert seen == [1, 2, 3]


def test_replay_stops_on_event(tmp_path):
    src, dst = tmp_path / "src.jsonl", tmp_path / "dst.jsonl"
    src.write_text("".join(f'{{"i": {i}}}\n' for i in range(100)), encoding="utf-8")
    stop = threading.Event()
    threading.Timer(0.25, stop.set).start()
    t0 = time.monotonic()
    n = replay(src, dst, 0.05, stop=stop)
    assert 1 <= n < 20 and time.monotonic() - t0 < 2


# ---------- Windows: Prism process tree is killed, not just prism.cmd ----------

def test_windows_prism_stop_kills_whole_tree(monkeypatch):
    monkeypatch.setattr(watcher, "_IS_WINDOWS", True)
    ran = []
    monkeypatch.setattr(watcher.subprocess, "run", lambda cmd, **kw: ran.append(cmd))

    class Proc:
        pid = 4321
        waited = False

        def poll(self):
            return None if not self.waited else 1

        def wait(self, timeout=None):
            self.waited = True
            return 1

    pm = watcher.PrismManager("x.yaml", 4010)
    pm.proc = Proc()
    pm.stop()
    assert ran == [["taskkill", "/PID", "4321", "/T", "/F"]]
    assert pm.proc is None


# ---------- --live: passport service + continuous traffic ----------

def test_live_demo_end_to_end(tmp_path, capsys):
    stop, prism = threading.Event(), FakePrism()
    # auto_pause: the v2 rollout is instant now (no restart), so give v1 a few rounds of traffic first —
    # the rename can only be detected once date_of_birth was an established field
    kw = demo_kwargs(tmp_path, auto=True, prism=prism, stop_event=stop, live=True, service_port=free_port(),
                     auto_pause=2.0,
                     traffic_kwargs=dict(min_count=40, max_count=60, min_gap=0.2, max_gap=0.4, delay=0))
    log, changes = Path(kw["log_path"]), Path(kw["spec_path"]).parent / "changes.jsonl"
    printed: list[str] = []

    def console() -> str:
        printed.append(capsys.readouterr().out)
        return "".join(printed)

    def renamed() -> int:
        return sum(1 for r in rows(changes) if r.get("kind") == "field_renamed" and r.get("breaking"))

    t, result = start_demo(**kw)
    try:
        assert wait_for(lambda: "passport service v1" in console())
        assert wait_for(lambda: "traffic: round 1" in console(), timeout=30)
        assert wait_for(lambda: "passport service v2" in console(), timeout=30)
        # the generator switched to v2 payloads on its own, and the watcher saw the rename
        assert wait_for(lambda: "v2 payloads" in console(), timeout=30)
        assert wait_for(lambda: renamed() >= 1, timeout=60)
        assert get_json(kw["port"], "/api/summary")["endpoint_count"] == 5
        assert get_json(kw["service_port"], "/meta")["version"] == 2
        assert log.exists() and len(rows(log)) >= 80
    finally:
        stop.set()
        t.join(timeout=40)
    assert not t.is_alive() and result.get("rc") == 0
    out = console()
    assert "traffic summary:" in out and "demo stopped." in out
    assert not [th.name for th in threading.enumerate() if th.name in ("traffic", "passport-service") and th.is_alive()]
    with pytest.raises(Exception):
        get_json(kw["service_port"], "/meta")  # the service is gone with the demo


def test_live_demo_refuses_busy_service_port(tmp_path, capsys):
    port = free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen()
        kw = demo_kwargs(tmp_path, auto=True, prism=FakePrism(), live=True, service_port=port)
        assert main.run_demo(**kw) == 1
    assert "passport service" in capsys.readouterr().out


def test_drift_demo_rolls_out_sprints_and_traffic_follows(tmp_path, capsys):
    from demo_app import drift as drift_module
    stop, prism = threading.Event(), FakePrism()
    kw = demo_kwargs(tmp_path, auto=True, prism=prism, stop_event=stop, live=True, drift=True, drift_interval=0.6,
                     service_port=free_port(), auto_pause=1.5,
                     traffic_kwargs=dict(min_count=25, max_count=35, min_gap=0.1, max_gap=0.2, delay=0))
    log, changes = Path(kw["log_path"]), Path(kw["spec_path"]).parent / "changes.jsonl"
    printed: list[str] = []

    def console() -> str:
        printed.append(capsys.readouterr().out)
        return "".join(printed)

    t, result = start_demo(**kw)
    try:
        assert wait_for(lambda: "All sprints rolled out" in console(), timeout=60)
        out = console()
        for n in range(2, drift_module.MAX_STAGE + 1):
            assert f"SPRINT {n}/{drift_module.MAX_STAGE} rolled out" in out
        assert get_json(kw["service_port"], "/meta")["stage"] == drift_module.MAX_STAGE
        # the generator followed: final-contract submissions exist (citizenship + consent), and so do
        # old-client 400s; the watcher saw more than one breaking kind over the run
        assert wait_for(lambda: any("citizenship" in r.get("request_body", {}) and r.get("request_body", {}).get("consent") is True
                                    and r.get("status") == 201 for r in rows(log) if isinstance(r.get("request_body"), dict)), timeout=30)
        assert wait_for(lambda: "following it" in console(), timeout=30)
        assert wait_for(lambda: len({r["kind"] for r in rows(changes) if r.get("breaking")}) >= 2, timeout=90)
    finally:
        stop.set()
        t.join(timeout=40)
    assert not t.is_alive() and result.get("rc") == 0
    assert "demo stopped." in console()
