"""main.py — CLI entry point that wires the modules together.

    python main.py build  sample_logs.jsonl -o output/openapi.yaml   # one-shot
    python main.py watch  live_logs.jsonl                            # continuous mode + Prism
    python main.py dashboard --port 8000                             # FastAPI dashboard
    python main.py demo [--auto]                                     # the whole live demo
"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from typing import Any, Callable

from inferrer import infer_all
from normalizer import group_by_endpoint
from parser import parse_line
from spec_builder import build_spec, validate_spec, write_spec


def cmd_build(log_path: str, out_path: str) -> int:
    """parser.parse_line -> normalizer.group_by_endpoint -> inferrer.infer_all
    -> spec_builder.build_spec -> validate_spec -> write_spec.
    Print a short summary. Return 0 if valid, 1 otherwise."""
    src = Path(log_path)
    if not src.is_file():
        print(f"error: log file not found: {src}")
        return 1

    # Read line by line (rather than parser.read_logs) so skipped lines can be counted.
    entries = []
    skipped = 0
    with src.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue  # blank lines are not counted as skipped
            entry = parse_line(line)
            if entry is None:
                skipped += 1
            else:
                entries.append(entry)

    grouped = group_by_endpoint(entries)
    endpoints = infer_all(grouped)
    spec = build_spec(endpoints)
    errors = validate_spec(spec)
    write_spec(spec, out_path)

    print(f"log entries read : {len(entries)}")
    print(f"lines skipped    : {skipped}")
    print(f"endpoints found  : {len(endpoints)}")
    if errors:
        print(f"validation       : FAILED ({len(errors)} error{'s' if len(errors) != 1 else ''})")
        for err in errors:
            print(f"  - {err}")
    else:
        print("validation       : OK")
    print(f"output           : {Path(out_path)}")
    return 1 if errors else 0


def cmd_watch(log_path: str, spec_path: str, prism_port: int, no_prism: bool, fresh: bool = False) -> int:
    """Run watcher.watch (with PrismManager unless --no-prism).
    changes.jsonl is written next to the spec, where the dashboard expects it.
    fresh=True deletes the spec and changes.jsonl first (clean slate for the demo)."""
    from watcher import PrismManager, watch

    spec = Path(spec_path)
    changes = spec.parent / "changes.jsonl"
    if fresh:
        for p in (spec, changes):
            if p.exists():
                p.unlink()
                print(f"fresh   : removed {p}")
    prism = None if no_prism else PrismManager(spec, prism_port)
    print(f"log     : {Path(log_path)}")
    print(f"spec    : {spec}")
    print(f"changes : {changes}")
    print(f"prism   : {'disabled' if no_prism else f'port {prism_port}'}")
    try:
        watch(log_path, spec, changes, prism)
    except KeyboardInterrupt:  # backstop; watch() normally handles Ctrl+C itself
        if prism is not None:
            prism.stop()
    return 0


def cmd_dashboard(host: str, port: int, spec_path: str = "output/openapi.yaml") -> int:
    """Run the FastAPI dashboard with uvicorn. It reads `spec_path` and changes.jsonl next to it
    (the same layout `watch --spec` writes)."""
    import uvicorn

    from dashboard import app as dashboard_app

    spec = Path(spec_path)
    dashboard_app.SPEC_PATH = spec
    dashboard_app.CHANGES_PATH = spec.parent / "changes.jsonl"
    shown = "localhost" if host in ("0.0.0.0", "::", "") else host
    print(f"dashboard: http://{shown}:{port}  (reading {spec} and {dashboard_app.CHANGES_PATH})")
    uvicorn.run(dashboard_app.app, host=host, port=port, log_level="warning")
    return 0


# ---------------------------------------------------------------------------
# demo: the whole live demo in one command
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent
DEMO_SAMPLE = _ROOT / "sample_logs.jsonl"
DEMO_CHANGED = _ROOT / "changed_logs.jsonl"
# Input datasets the demo's clean start must never delete.
_PROTECTED_LOGS = {DEMO_SAMPLE.resolve(), DEMO_CHANGED.resolve()}
START_PROMPT = "Press Enter to start the traffic replay"
CHANGE_PROMPT = "Press Enter to introduce the API change"


def _remove_for_fresh(p: Path) -> bool:
    """Delete `p` if it exists. If Windows refuses because another process has it open
    (e.g. replay_logs.py still writing), empty it instead. Returns False if neither worked."""
    if not p.exists():
        return True
    try:
        p.unlink()
        print(f"fresh   : removed {p}")
        return True
    except PermissionError:
        pass
    except OSError as e:
        print(f"error: could not remove {p}: {e}")
        return False
    try:
        with p.open("w", encoding="utf-8"):
            pass
        print(f"fresh   : emptied {p} (in use by another process, could not delete)")
        return True
    except OSError as e:
        print(f"error: could not remove or empty {p}: {e}. Stop whatever is writing to it and retry.")
        return False


def _banner(*lines: str) -> None:
    bar = "=" * max(60, *(len(s) + 4 for s in lines))
    print("\n" + bar, *("  " + s for s in lines), bar, sep="\n", flush=True)


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _enter_pause(prompt: str) -> None:
    """Block until Enter. Ctrl+C propagates (stops the demo); a closed stdin just continues."""
    _banner(prompt)
    try:
        input()
    except EOFError:
        pass


def run_demo(
    log_path: str | Path = "live_logs.jsonl",
    spec_path: str | Path = "output/openapi.yaml",
    host: str = "127.0.0.1",
    port: int = 8000,
    prism_port: int = 4010,
    use_prism: bool = True,
    auto: bool = False,
    sample_delay: float = 0.05,
    changed_delay: float = 0.3,
    *,
    sample_path: str | Path = DEMO_SAMPLE,
    changed_path: str | Path = DEMO_CHANGED,
    auto_pause: float = 3.0,
    debounce: float = 1.0,
    prism: Any = None,
    pause: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Clean start -> watcher (+ Prism) and dashboard in background threads -> wait -> replay sample logs
    -> wait -> replay changed logs -> run until Ctrl+C (or `stop_event`), then stop everything.

    The watcher and dashboard run in this process, so Prism is the only child process and a single
    Ctrl+C reaches one place that shuts things down in order. `prism`, `pause` and `stop_event` are
    injection points for tests. Returns 0, or 1/2 if the demo could not start.
    """
    import uvicorn

    import watcher
    from dashboard import app as dashboard_app
    from replay_logs import replay

    log, spec = Path(log_path), Path(spec_path)
    changes = spec.parent / "changes.jsonl"
    stop = stop_event or threading.Event()

    # ---- preflight: nothing is deleted or started unless the demo can actually run ----
    for src in (sample_path, changed_path):
        if not Path(src).is_file():
            print(f"error: {src} not found. Generate it with: python generate_logs.py --changed")
            return 1
    if log.resolve() in _PROTECTED_LOGS:
        print(f"error: the demo would delete {log.name}, which is input data. Use e.g. live_logs.jsonl.")
        return 2
    if prism is None and use_prism:
        prism = watcher.PrismManager(spec, prism_port)
    prism_available = prism is not None and (not isinstance(prism, watcher.PrismManager)
                                             or watcher.find_prism() is not None)
    busy = [(port, "dashboard")] + ([(prism_port, "Prism")] if prism_available else [])
    for p, what in busy:
        if _port_in_use(p):
            print(f"error: port {p} ({what}) is already in use. Is another demo, dashboard or Prism "
                  f"still running? Stop it, or pick another port.")
            return 1

    # ---- 1. clean start ----
    for p in (log, spec, changes):
        if not _remove_for_fresh(p):
            return 1

    dashboard_app.SPEC_PATH = spec
    dashboard_app.CHANGES_PATH = changes
    dashboard_app.PRISM_PORT = prism_port
    watch_stop = threading.Event()
    server = uvicorn.Server(uvicorn.Config(dashboard_app.app, host=host, port=port, log_level="warning"))
    watch_thread = threading.Thread(
        target=watcher.watch, name="watcher", daemon=True,
        kwargs=dict(log_path=log, spec_path=spec, changes_path=changes, prism=prism,
                    debounce_seconds=debounce, stop_event=watch_stop))
    dash_thread = threading.Thread(target=server.run, name="dashboard", daemon=True)

    def wait(seconds: float) -> None:
        if stop.wait(seconds):
            raise KeyboardInterrupt  # stop_event set -> same path as Ctrl+C

    def pause_step(prompt: str) -> None:
        if stop.is_set():
            raise KeyboardInterrupt
        if auto:
            print(f"(--auto) {prompt.replace('Press Enter to', 'about to')} in {auto_pause:g}s", flush=True)
            wait(auto_pause)
        else:
            (pause or _enter_pause)(prompt)
        if stop.is_set():  # stopped while we were waiting
            raise KeyboardInterrupt

    def replay_step(src: Path, delay: float) -> None:
        if stop.is_set():
            raise KeyboardInterrupt
        n = sum(1 for line in open(src, encoding="utf-8") if line.strip())
        print(f"replaying {src.name}: {n} lines, {delay:g}s apart -> {log}", flush=True)
        written = replay(src, log, delay, stop=stop)
        if stop.is_set():
            raise KeyboardInterrupt
        print(f"replayed {written} lines from {src.name}", flush=True)
        wait(debounce + 1.0)  # let the watcher's last rebuild land before the next prompt

    url = f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1', '::') else host}:{port}"
    try:
        # ---- 2. background services ----
        watch_thread.start()
        deadline = time.monotonic() + 15
        while not spec.exists():
            if not watch_thread.is_alive() or time.monotonic() > deadline:
                print("error: the watcher did not start (no baseline spec written)")
                return 1
            wait(0.05)
        dash_thread.start()
        while not server.started:
            if not dash_thread.is_alive() or time.monotonic() > deadline:
                print(f"error: the dashboard did not start on {host}:{port}")
                return 1
            wait(0.05)

        # ---- 3. URL + wait ----
        prism_line = (f"Prism mock: http://127.0.0.1:{prism_port}" if prism_available
                      else "Prism: not running (the dashboard's 'Try it' buttons will fail)")
        _banner(f"Dashboard:  {url}", prism_line, "Ctrl+C stops everything")
        pause_step(START_PROMPT)
        # ---- 4. normal traffic ----
        replay_step(Path(sample_path), sample_delay)
        # ---- 5. wait ----
        pause_step(CHANGE_PROMPT)
        # ---- 6. the breaking change ----
        replay_step(Path(changed_path), changed_delay)
        # ---- 7. keep running ----
        _banner(f"Demo running. Dashboard: {url}", "Press Ctrl+C to stop.")
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_demo(stop, server, dash_thread, watch_stop, watch_thread, prism)
    return 0


def _shutdown_demo(stop, server, dash_thread, watch_stop, watch_thread, prism) -> None:
    """Stop replay, dashboard, watcher; the watcher's own cleanup stops Prism. A second Ctrl+C
    during shutdown skips the waiting but still kills Prism."""
    print("\nstopping demo...", flush=True)
    try:
        stop.set()
        server.should_exit = True
        if dash_thread.is_alive():
            dash_thread.join(timeout=5)
        watch_stop.set()
        if watch_thread.is_alive():
            watch_thread.join(timeout=15)
    except KeyboardInterrupt:
        pass
    finally:
        running = getattr(prism, "is_running", None)
        if prism is not None and callable(running) and running():
            prism.stop()  # watcher thread didn't get to it
    print("demo stopped.", flush=True)


def cmd_demo(auto: bool = False, port: int = 8000, prism_port: int = 4010, no_prism: bool = False,
             log_path: str = "live_logs.jsonl", spec_path: str = "output/openapi.yaml",
             sample_delay: float = 0.05, changed_delay: float = 0.3) -> int:
    return run_demo(log_path, spec_path, port=port, prism_port=prism_port, use_prism=not no_prism,
                    auto=auto, sample_delay=sample_delay, changed_delay=changed_delay)


def main() -> None:
    ap = argparse.ArgumentParser(description="API contract & mock generator from raw logs")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="infer spec once from a log file")
    b.add_argument("log_path")
    b.add_argument("-o", "--out", default="output/openapi.yaml")

    w = sub.add_parser("watch", help="continuous mode")
    w.add_argument("log_path")
    w.add_argument("--spec", default="output/openapi.yaml")
    w.add_argument("--prism-port", type=int, default=4010)
    w.add_argument("--no-prism", action="store_true")
    w.add_argument("--fresh", action="store_true",
                   help="delete the spec and changes.jsonl before starting (clean demo)")

    d = sub.add_parser("dashboard", help="run the dashboard")
    d.add_argument("--host", default="0.0.0.0")
    d.add_argument("--port", type=int, default=8000)
    d.add_argument("--spec", default="output/openapi.yaml",
                   help="spec to show; changes.jsonl is read from the same folder")

    m = sub.add_parser("demo", help="run the whole live demo (clean start, watcher + Prism, dashboard, replays)")
    m.add_argument("--auto", action="store_true", help="don't wait for Enter between steps (for recording)")
    m.add_argument("--port", type=int, default=8000, help="dashboard port")
    m.add_argument("--prism-port", type=int, default=4010)
    m.add_argument("--no-prism", action="store_true")
    m.add_argument("--log", default="live_logs.jsonl", help="live log file (deleted at start)")
    m.add_argument("--spec", default="output/openapi.yaml", help="spec (and changes.jsonl next to it; deleted at start)")
    m.add_argument("--sample-delay", type=float, default=0.05)
    m.add_argument("--changed-delay", type=float, default=0.3)

    a = ap.parse_args()
    if a.cmd == "demo":
        raise SystemExit(cmd_demo(a.auto, a.port, a.prism_port, a.no_prism, a.log, a.spec,
                                  a.sample_delay, a.changed_delay))
    if a.cmd == "build":
        raise SystemExit(cmd_build(a.log_path, a.out))
    if a.cmd == "watch":
        raise SystemExit(cmd_watch(a.log_path, a.spec, a.prism_port, a.no_prism, a.fresh))
    raise SystemExit(cmd_dashboard(a.host, a.port, a.spec))


if __name__ == "__main__":
    main()
