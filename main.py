"""main.py — CLI entry point that wires the modules together.

    python main.py build  sample_logs.jsonl -o output/openapi.yaml   # one-shot
    python main.py watch  live_logs.jsonl                            # continuous mode + Prism
    python main.py dashboard --port 8000                             # FastAPI dashboard
    python main.py demo [--auto]                                     # the whole live demo
    python main.py llm-check                                         # test the AIDH connection
    # add --llm-refine to build/watch/demo to turn on LLM-written descriptions (see llm_refine.py)
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from inferrer import infer_all
from llm_refine import AidhClient
from normalizer import group_by_endpoint
from parser import parse_line
from spec_builder import build_spec, spec_paths, validate_spec, write_spec


def _load_dotenv() -> None:
    """Load AIDH_* (and any other) vars from a .env file at the repo root, if one exists. Never
    overrides a variable already set in the shell. A no-op (not an error) if there's no .env or
    python-dotenv isn't installed — --llm-refine falls back to shell-set env vars either way."""
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass


def _llm_client(enabled: bool) -> "AidhClient | None":
    """AidhClient.from_env() if --llm-refine was passed, else None. Warns (doesn't fail) if the flag
    was given but AIDH_BASE_URL/AIDH_DOMAIN_ID/AIDH_MODEL aren't all set (in the shell or in .env).

    Also turns on INFO-level logging for llm_refine specifically (a no-op if logging is already
    configured, e.g. inside `demo`'s dashboard thread), so every successful/failed AIDH call prints
    a line — that's the easiest way to see whether the LLM is actually being used."""
    if not enabled:
        return None
    _load_dotenv()
    logging.getLogger("llm_refine").setLevel(logging.INFO)
    logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(message)s")
    client = AidhClient.from_env()
    if client is None:
        print("warning: --llm-refine given but AIDH_BASE_URL/AIDH_DOMAIN_ID/AIDH_MODEL are not all set "
              "(checked the shell environment and .env); continuing without LLM-refined descriptions.")
    return client


def cmd_llm_check() -> int:
    """Send one test prompt straight to AIDH and print the raw exchange. Use this to confirm
    AIDH_BASE_URL/AIDH_DOMAIN_ID/AIDH_MODEL and the gateway's response shape are correct *before*
    trusting --llm-refine on real traffic. Returns 0 if a reply came back, 1 otherwise."""
    import os

    _load_dotenv()
    client = AidhClient.from_env()
    if client is None:
        missing = [k for k in ("AIDH_BASE_URL", "AIDH_DOMAIN_ID", "AIDH_MODEL") if not os.environ.get(k, "").strip()]
        print(f"error: missing env var(s): {', '.join(missing)} (checked the shell environment and .env)")
        return 1

    logging.getLogger("llm_refine").setLevel(logging.INFO)
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")

    print(f"AIDH_BASE_URL  = {client.base_url}")
    print(f"AIDH_DOMAIN_ID = {client.domain_id}")
    print(f"AIDH_MODEL     = {client.model}")
    print(f"POST {client.base_url}/api/generate ...")
    reply = client.chat("You are a terse test assistant.", "Reply with exactly the text: AIDH_OK")
    if reply is None:
        print("FAILED: no usable reply (see the [llm_refine] warning above for the reason)")
        return 1
    print(f"reply: {reply!r}")
    print("looks reachable and correctly configured." if "AIDH_OK" in reply
          else "reachable, but the model didn't echo the expected text — check AIDH_MODEL / prompt handling.")
    return 0


def cmd_build(log_path: str, out_path: str, llm_refine: bool = False) -> int:
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

    llm = _llm_client(llm_refine)
    grouped = group_by_endpoint(entries)
    endpoints = infer_all(grouped, llm)
    spec = build_spec(endpoints, llm=llm)
    errors = validate_spec(spec)
    yaml_path, json_path = write_spec(spec, out_path)

    print(f"log entries read : {len(entries)}")
    print(f"lines skipped    : {skipped}")
    print(f"endpoints found  : {len(endpoints)}")
    if errors:
        print(f"validation       : FAILED ({len(errors)} error{'s' if len(errors) != 1 else ''})")
        for err in errors:
            print(f"  - {err}")
    else:
        print("validation       : OK")
    print(f"openapi.json     : {json_path}")
    print(f"openapi.yaml     : {yaml_path}")
    return 1 if errors else 0


def cmd_watch(log_path: str, spec_path: str, prism_port: int, no_prism: bool, fresh: bool = False,
              static: bool = False, llm_refine: bool = False) -> int:
    """Run watcher.watch (with PrismManager unless --no-prism).
    changes.jsonl is written next to the spec, where the dashboard expects it.
    fresh=True first deletes the watched log file, the spec and changes.jsonl (clean slate for the demo),
    so the baseline is always 0 entries. A log file another process still has open (Windows) is emptied
    instead. Refuses (returns 2) if the log is one of the input datasets, so they can't be deleted.
    Prism runs in dynamic mode (-d: fresh data per call); static=True serves the recorded examples."""
    from watcher import PrismManager, watch

    log = Path(log_path)
    spec = Path(spec_path)
    changes = spec.parent / "changes.jsonl"
    if fresh:
        if log.resolve() in _PROTECTED_LOGS:
            print(f"error: --fresh would delete {log.name}, which is input data. "
                  f"Watch a separate file (e.g. live_logs.jsonl) and replay into it.")
            return 2
        for p in (log, *spec_paths(spec), changes):
            if not _remove_for_fresh(p):
                return 1
    prism = None if no_prism else PrismManager(spec, prism_port, dynamic=not static)
    print(f"log     : {Path(log_path)}")
    print(f"spec    : {spec}")
    print(f"changes : {changes}")
    print(f"prism   : {'disabled' if no_prism else f'port {prism_port}, ' + ('static' if static else 'dynamic')}")
    try:
        watch(log_path, spec, changes, prism, llm=_llm_client(llm_refine))
    except KeyboardInterrupt:  # backstop; watch() normally handles Ctrl+C itself
        if prism is not None:
            prism.stop()
    return 0


def cmd_dashboard(host: str, port: int, spec_path: str = "output/openapi.yaml",
                   log_path: str = "live_logs.jsonl") -> int:
    """Run the FastAPI dashboard with uvicorn. It reads `spec_path` and changes.jsonl next to it
    (the same layout `watch --spec` writes), plus `log_path` for raw request/response samples."""
    import uvicorn

    from dashboard import app as dashboard_app

    spec = Path(spec_path)
    dashboard_app.SPEC_PATH = spec
    dashboard_app.CHANGES_PATH = spec.parent / "changes.jsonl"
    dashboard_app.LOG_PATH = Path(log_path)
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
    static: bool = False,
    llm_refine: bool = False,
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
        prism = watcher.PrismManager(spec, prism_port, dynamic=not static)
    prism_available = prism is not None and (not isinstance(prism, watcher.PrismManager)
                                             or watcher.find_prism() is not None)
    busy = [(port, "dashboard")] + ([(prism_port, "Prism")] if prism_available else [])
    for p, what in busy:
        if _port_in_use(p):
            print(f"error: port {p} ({what}) is already in use. Is another demo, dashboard or Prism "
                  f"still running? Stop it, or pick another port.")
            return 1

    # ---- 1. clean start ----
    for p in (log, *spec_paths(spec), changes):
        if not _remove_for_fresh(p):
            return 1

    dashboard_app.SPEC_PATH = spec
    dashboard_app.CHANGES_PATH = changes
    dashboard_app.LOG_PATH = log
    dashboard_app.PRISM_PORT = prism_port
    watch_stop = threading.Event()
    server = uvicorn.Server(uvicorn.Config(dashboard_app.app, host=host, port=port, log_level="warning"))
    watch_thread = threading.Thread(
        target=watcher.watch, name="watcher", daemon=True,
        kwargs=dict(log_path=log, spec_path=spec, changes_path=changes, prism=prism,
                    debounce_seconds=debounce, stop_event=watch_stop, llm=_llm_client(llm_refine)))
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
        prism_line = (f"Prism mock: http://127.0.0.1:{prism_port} ({'static examples' if static else 'dynamic data'})"
                      if prism_available
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
             sample_delay: float = 0.05, changed_delay: float = 0.3, static: bool = False,
             llm_refine: bool = False) -> int:
    return run_demo(log_path, spec_path, port=port, prism_port=prism_port, use_prism=not no_prism,
                    auto=auto, sample_delay=sample_delay, changed_delay=changed_delay, static=static,
                    llm_refine=llm_refine)


def main() -> None:
    ap = argparse.ArgumentParser(description="API contract & mock generator from raw logs")
    sub = ap.add_subparsers(dest="cmd", required=True)

    llm_help = ("also ask the AIDH LLM (AIDH_BASE_URL/AIDH_DOMAIN_ID/AIDH_MODEL) for field and endpoint "
                "descriptions the rule-based inferrer can't produce; off by default, and any LLM failure "
                "just falls back to the rule-based spec")

    sub.add_parser("llm-check", help="send one test prompt to AIDH and print the raw exchange (config/connectivity check)")

    b = sub.add_parser("build", help="infer spec once from a log file")
    b.add_argument("log_path")
    b.add_argument("-o", "--out", default="output/openapi.yaml")
    b.add_argument("--llm-refine", action="store_true", help=llm_help)

    w = sub.add_parser("watch", help="continuous mode")
    w.add_argument("log_path")
    w.add_argument("--spec", default="output/openapi.yaml")
    w.add_argument("--prism-port", type=int, default=4010)
    w.add_argument("--no-prism", action="store_true")
    w.add_argument("--static", action="store_true",
                   help="Prism serves the recorded examples instead of fresh generated data (-d)")
    w.add_argument("--fresh", action="store_true",
                   help="delete the log file, the spec and changes.jsonl before starting (clean demo)")
    w.add_argument("--llm-refine", action="store_true", help=llm_help)

    d = sub.add_parser("dashboard", help="run the dashboard")
    d.add_argument("--host", default="0.0.0.0")
    d.add_argument("--port", type=int, default=8000)
    d.add_argument("--spec", default="output/openapi.yaml",
                   help="spec to show; changes.jsonl is read from the same folder")
    d.add_argument("--log", default="live_logs.jsonl",
                   help="traffic log to pull raw request/response samples from")

    m = sub.add_parser("demo", help="run the whole live demo (clean start, watcher + Prism, dashboard, replays)")
    m.add_argument("--auto", action="store_true", help="don't wait for Enter between steps (for recording)")
    m.add_argument("--port", type=int, default=8000, help="dashboard port")
    m.add_argument("--prism-port", type=int, default=4010)
    m.add_argument("--no-prism", action="store_true")
    m.add_argument("--static", action="store_true",
                   help="Prism serves the recorded examples instead of fresh generated data (-d)")
    m.add_argument("--log", default="live_logs.jsonl", help="live log file (deleted at start)")
    m.add_argument("--spec", default="output/openapi.yaml", help="spec (and changes.jsonl next to it; deleted at start)")
    m.add_argument("--sample-delay", type=float, default=0.05)
    m.add_argument("--changed-delay", type=float, default=0.3)
    m.add_argument("--llm-refine", action="store_true", help=llm_help)

    a = ap.parse_args()
    if a.cmd == "llm-check":
        raise SystemExit(cmd_llm_check())
    if a.cmd == "demo":
        raise SystemExit(cmd_demo(a.auto, a.port, a.prism_port, a.no_prism, a.log, a.spec,
                                  a.sample_delay, a.changed_delay, a.static, a.llm_refine))
    if a.cmd == "build":
        raise SystemExit(cmd_build(a.log_path, a.out, a.llm_refine))
    if a.cmd == "watch":
        raise SystemExit(cmd_watch(a.log_path, a.spec, a.prism_port, a.no_prism, a.fresh, static=a.static,
                                   llm_refine=a.llm_refine))
    raise SystemExit(cmd_dashboard(a.host, a.port, a.spec, a.log))


if __name__ == "__main__":
    main()
