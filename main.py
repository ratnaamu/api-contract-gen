"""main.py — CLI entry point that wires the modules together.

    python main.py build  sample_logs.jsonl -o output/openapi.yaml   # one-shot
    python main.py watch  live_logs.jsonl                            # continuous mode + Prism
    python main.py dashboard --port 8000                             # FastAPI dashboard
    python main.py demo [--auto]                                     # the whole live demo
    python main.py llm-check                                         # test the AIDH connection
    # add --llm-refine to build/watch/demo to turn on LLM-written descriptions (see llm_refine.py)
    # add --infer-errors to build/watch/demo to guess plausible error statuses (see errors.py)
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from llm_refine import AidhClient
from parser import parse_line_with_reason
from quality import build_quality_report
from spec_builder import spec_paths, validate_spec, write_spec


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


def cmd_build(log_path: str, out_path: str, llm_refine: bool = False, infer_errors: bool = False) -> int:
    """parser.parse_line -> watcher.build_from_entries (normalizer.group_by_endpoint -> inferrer.infer_all
    -> spec_builder.build_spec, optionally also errors.py's B2 guesses) -> validate_spec -> write_spec.
    Print a short summary. Return 0 if valid, 1 otherwise."""
    from watcher import build_from_entries

    src = Path(log_path)
    if not src.is_file():
        print(f"error: log file not found: {src}")
        return 1

    # Read line by line (rather than parser.read_logs) so skipped lines can be counted by reason.
    entries = []
    skipped: dict[str, int] = {}
    with src.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue  # blank lines are not counted as skipped
            entry, reason = parse_line_with_reason(line)
            if entry is None:
                skipped[reason] = skipped.get(reason, 0) + 1
            else:
                entries.append(entry)

    llm = _llm_client(llm_refine)
    spec = build_from_entries(entries, llm=llm, infer_errors=infer_errors)
    endpoint_count = sum(len(item) for item in spec["paths"].values())
    errors = validate_spec(spec)
    yaml_path, json_path = write_spec(spec, out_path)
    quality_path = yaml_path.parent / "quality.json"
    quality_path.write_text(
        json.dumps(build_quality_report(spec, len(entries), skipped), indent=2) + "\n", encoding="utf-8")

    print(f"log entries read : {len(entries)}")
    print(f"lines skipped    : {sum(skipped.values())}" + (f" ({skipped})" if skipped else ""))
    print(f"endpoints found  : {endpoint_count}")
    if errors:
        print(f"validation       : FAILED ({len(errors)} error{'s' if len(errors) != 1 else ''})")
        for err in errors:
            print(f"  - {err}")
    else:
        print("validation       : OK")
    print(f"quality.json     : {quality_path}")
    print(f"openapi.json     : {json_path}")
    print(f"openapi.yaml     : {yaml_path}")
    return 1 if errors else 0


def _reporter(spec: Path, log: Path, llm: Any):
    """report.ChangeReporter for this spec (Word report per breaking burst), or None if python-docx is
    missing — the pipeline must keep working without it."""
    try:
        import docx  # noqa: F401  (python-docx)
        from report import ChangeReporter
    except ImportError:
        print("note: python-docx not installed; no Word change reports (pip install python-docx)")
        return None
    return ChangeReporter(spec, log_path=log, llm=llm, say=lambda m: print(m, flush=True))


def cmd_watch(log_path: str, spec_path: str, prism_port: int, no_prism: bool, fresh: bool = False,
              static: bool = False, llm_refine: bool = False, infer_errors: bool = False,
              use_mock_proxy: bool = False) -> int:
    """Run watcher.watch (with PrismManager unless --no-prism).
    changes.jsonl is written next to the spec, where the dashboard expects it.
    fresh=True first deletes the watched log file, the spec and changes.jsonl (clean slate for the demo),
    so the baseline is always 0 entries. A log file another process still has open (Windows) is emptied
    instead. Refuses (returns 2) if the log is one of the input datasets, so they can't be deleted.
    Prism runs in dynamic mode (-d: fresh data per call); static=True serves the recorded examples.

    use_mock_proxy=True moves Prism to an internal port (prism_port + 1) and runs mock_proxy.py (B5) on
    prism_port instead, in a background thread for the duration of this (otherwise blocking) command."""
    import uvicorn

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
    upstream_port = prism_port + 1 if use_mock_proxy else prism_port
    prism = None if no_prism else PrismManager(spec, upstream_port, dynamic=not static)
    print(f"log     : {Path(log_path)}")
    print(f"spec    : {spec}")
    print(f"changes : {changes}")
    print(f"prism   : {'disabled' if no_prism else f'port {upstream_port}, ' + ('static' if static else 'dynamic')}")

    proxy_server = None
    if use_mock_proxy:
        import mock_proxy as proxy_module
        proxy_module.SPEC_PATH = spec
        proxy_module.LOG_PATH = log
        proxy_module.UPSTREAM_URL = f"http://127.0.0.1:{upstream_port}"
        proxy_server = uvicorn.Server(uvicorn.Config(proxy_module.app, host="0.0.0.0", port=prism_port, log_level="warning"))
        threading.Thread(target=proxy_server.run, name="mock-proxy", daemon=True).start()
        print(f"proxy   : port {prism_port} (stateful 404s, validation, auth, chaos; see mock_proxy.py)")

    llm = _llm_client(llm_refine)
    print(f"reports : {spec.parent / 'reports'} (a Word report on every breaking change)")
    try:
        watch(log_path, spec, changes, prism, llm=llm, infer_errors=infer_errors,
              reporter=_reporter(spec, log, llm))
    except KeyboardInterrupt:  # backstop; watch() normally handles Ctrl+C itself
        if prism is not None:
            prism.stop()
    finally:
        if proxy_server is not None:
            proxy_server.should_exit = True
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
    dashboard_app.REPORTS_DIR = spec.parent / "reports"
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
LIVE_START_PROMPT = "Press Enter to start live traffic against the passport service"
LIVE_CHANGE_PROMPT = "Press Enter to roll out passport API v2 (the breaking change)"
DEFAULT_SERVICE_PORT = 8001
DEFAULT_DRIFT_INTERVAL = 60.0  # seconds between sprints in --drift mode


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
    infer_errors: bool = False,
    mock_proxy: bool = False,
    live: bool = False,
    service_port: int = DEFAULT_SERVICE_PORT,
    drift: bool = False,
    drift_interval: float = DEFAULT_DRIFT_INTERVAL,
    *,
    sample_path: str | Path = DEMO_SAMPLE,
    changed_path: str | Path = DEMO_CHANGED,
    auto_pause: float = 3.0,
    debounce: float = 1.0,
    prism: Any = None,
    pause: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
    traffic_kwargs: dict[str, Any] | None = None,
) -> int:
    """Clean start -> watcher (+ Prism) and dashboard in background threads -> wait -> replay sample logs
    -> wait -> replay changed logs -> run until Ctrl+C (or `stop_event`), then stop everything.

    The watcher and dashboard run in this process, so Prism is the only child process and a single
    Ctrl+C reaches one place that shuts things down in order. `prism`, `pause` and `stop_event` are
    injection points for tests. Returns 0, or 1/2 if the demo could not start.

    mock_proxy=True (B5, see mock_proxy.py) moves Prism to an internal port (prism_port + 1) and puts
    the proxy on prism_port instead — the port the dashboard's "Try it" already targets — so stateful
    404s/validation/auth/chaos apply completely transparently, with no frontend change needed.

    live=True swaps the replays for the realistic source: the passport service (demo_app) runs in this
    process on `service_port`, logging to `log`; the first Enter starts traffic.run_continuous in a
    background thread (rounds of 50-150 requests every 5-7s, for as long as the demo runs); the second
    Enter moves the running service to v2 (sprint 1 of demo_app/drift.py — handlers read the stage per
    request, so there is no restart) — the traffic generator notices on its next round and switches
    payload shape, so the breaking-change alerts appear with no further action. `traffic_kwargs`
    overrides run_continuous's pacing (tests use small, fast rounds).

    drift=True (with live): only the first Enter is needed. `drift_interval` seconds after traffic starts
    the service rolls to v2, and then keeps moving — one more sprint every `drift_interval` seconds (pages as string, updated_at dropped, nationality -> citizenship, an additive
    tracking_number, a required consent, submitted_at -> created_at), announced on the console as it
    happens, until drift.MAX_STAGE. The traffic follows each sprint; ~10% of submissions keep sending
    the previous shape (old clients). So the dashboard keeps finding breaking changes for as long as the
    demo runs, with no keypress after the first Enter.
    """
    import uvicorn

    import watcher
    from dashboard import app as dashboard_app
    from replay_logs import replay

    log, spec = Path(log_path), Path(spec_path)
    changes = spec.parent / "changes.jsonl"
    stop = stop_event or threading.Event()
    upstream_prism_port = prism_port + 1 if mock_proxy else prism_port

    # ---- preflight: nothing is deleted or started unless the demo can actually run ----
    if not live:
        for src in (sample_path, changed_path):
            if not Path(src).is_file():
                print(f"error: {src} not found. Generate it with: python generate_logs.py --changed")
                return 1
    if log.resolve() in _PROTECTED_LOGS:
        print(f"error: the demo would delete {log.name}, which is input data. Use e.g. live_logs.jsonl.")
        return 2
    if prism is None and use_prism:
        prism = watcher.PrismManager(spec, upstream_prism_port, dynamic=not static)
    prism_available = prism is not None and (not isinstance(prism, watcher.PrismManager)
                                             or watcher.find_prism() is not None)
    busy = [(port, "dashboard")] + ([(prism_port, "Prism" if not mock_proxy else "mock proxy")] if prism_available or mock_proxy else [])
    if live:
        busy.append((service_port, "passport service"))
    for p, what in busy:
        if _port_in_use(p):
            print(f"error: port {p} ({what}) is already in use. Is another demo, dashboard or Prism "
                  f"still running? Stop it, or pick another port.")
            return 1

    # ---- 1. clean start ----
    for p in (log, *spec_paths(spec), changes):
        if not _remove_for_fresh(p):
            return 1

    reports_dir = spec.parent / "reports"
    dashboard_app.SPEC_PATH = spec
    dashboard_app.CHANGES_PATH = changes
    dashboard_app.REPORTS_DIR = reports_dir
    dashboard_app.LOG_PATH = log
    dashboard_app.PRISM_PORT = prism_port
    watch_stop = threading.Event()
    server = uvicorn.Server(uvicorn.Config(dashboard_app.app, host=host, port=port, log_level="warning"))
    llm = _llm_client(llm_refine)
    for p in (list(reports_dir.iterdir()) if reports_dir.is_dir() else []):
        if p.is_file():
            _remove_for_fresh(p)  # a clean start means a clean release history too
    _remove_for_fresh(spec.parent / "contract-report-latest.docx")
    watch_thread = threading.Thread(
        target=watcher.watch, name="watcher", daemon=True,
        kwargs=dict(log_path=log, spec_path=spec, changes_path=changes, prism=prism,
                    debounce_seconds=debounce, stop_event=watch_stop, llm=llm,
                    infer_errors=infer_errors, reporter=_reporter(spec, log, llm)))
    dash_thread = threading.Thread(target=server.run, name="dashboard", daemon=True)

    proxy_server = None
    proxy_thread = None
    if mock_proxy:
        import mock_proxy as proxy_module
        proxy_module.SPEC_PATH = spec
        proxy_module.LOG_PATH = log
        proxy_module.UPSTREAM_URL = f"http://127.0.0.1:{upstream_prism_port}"
        proxy_server = uvicorn.Server(uvicorn.Config(proxy_module.app, host=host, port=prism_port, log_level="warning"))
        proxy_thread = threading.Thread(target=proxy_server.run, name="mock-proxy", daemon=True)

    def wait(seconds: float) -> None:
        if stop.wait(seconds):
            raise KeyboardInterrupt  # stop_event set -> same path as Ctrl+C

    # ---- live mode: the passport service as an in-process uvicorn server; sprints switch at runtime ----
    service: dict[str, Any] = {"server": None, "thread": None, "app": None}

    def start_service(stage: int = 0) -> bool:
        from demo_app import create_app
        app_ = create_app(stage=stage, log_path=log)
        srv = uvicorn.Server(uvicorn.Config(app_, host=host, port=service_port, log_level="warning"))
        th = threading.Thread(target=srv.run, name="passport-service", daemon=True)
        th.start()
        deadline_ = time.monotonic() + 15
        while not srv.started:
            if not th.is_alive() or time.monotonic() > deadline_:
                print(f"error: the passport service did not start on {host}:{service_port}")
                return False
            wait(0.05)
        service["server"], service["thread"], service["app"] = srv, th, app_
        print(f"passport service v{1 if stage == 0 else 2}: http://127.0.0.1:{service_port} -> {log}", flush=True)
        return True

    def roll_out(stage: int) -> None:
        """Move the running service to sprint `stage` and say what changed (no restart involved)."""
        from demo_app import drift as drift_module
        app_ = service["app"]
        if app_ is None:
            return
        new = app_.state.set_stage(stage)
        sp = drift_module.sprint(new)
        if sp is not None:
            _banner(f"SPRINT {new}/{drift_module.MAX_STAGE} rolled out: {sp.name}"
                    + ("  [BREAKING]" if sp.breaking else "  [additive]"),
                    sp.summary, "Traffic follows from its next round; ~10% of clients keep the old shape.")
        print(f"passport service v{1 if new == 0 else 2}: now at sprint {new} ({drift_module.stage_name(new)})", flush=True)

    def stop_service() -> None:
        srv, th = service["server"], service["thread"]
        if srv is not None:
            srv.should_exit = True
            if th is not None and th.is_alive():
                th.join(timeout=10)
        service["server"] = service["thread"] = service["app"] = None

    traffic_state: dict[str, Any] = {"thread": None, "result": None}

    def start_traffic() -> None:
        import httpx

        import traffic as traffic_module
        client = httpx.Client(base_url=f"http://127.0.0.1:{service_port}", timeout=10)
        kwargs = dict(delay=0.02, stop=stop, say=lambda m: print(m, flush=True))
        kwargs.update(traffic_kwargs or {})

        def body() -> None:
            try:
                traffic_state["result"] = traffic_module.run_continuous(client, **kwargs)
            except Exception as e:  # noqa: BLE001 — never take the demo down with the generator
                print(f"traffic: stopped: {type(e).__name__}: {e}", flush=True)
            finally:
                client.close()
        th = threading.Thread(target=body, name="traffic", daemon=True)
        th.start()
        traffic_state["thread"] = th

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
        if proxy_thread is not None:
            proxy_thread.start()
            while not proxy_server.started:
                if not proxy_thread.is_alive() or time.monotonic() > deadline:
                    print(f"error: the mock proxy did not start on {host}:{prism_port}")
                    return 1
                wait(0.05)

        # ---- 3. URL + wait ----
        if mock_proxy:
            prism_line = (f"Mock proxy: http://127.0.0.1:{prism_port} (stateful 404s, validation, auth, chaos)"
                          if prism_available else
                          "Mock proxy: not running (Prism itself did not start; the proxy still would have)")
        else:
            prism_line = (f"Prism mock: http://127.0.0.1:{prism_port} ({'static examples' if static else 'dynamic data'})"
                          if prism_available
                          else "Prism: not running (the dashboard's 'Try it' buttons will fail)")
        if live:
            if not start_service(stage=0):
                return 1
            _banner(f"Dashboard:  {url}", prism_line,
                    f"Passport form: http://127.0.0.1:{service_port}  (Backend switch -> mock)",
                    f"Reports: {reports_dir}  (a Word report on every breaking change; also on the dashboard)",
                    "Ctrl+C stops everything")
            pause_step(LIVE_START_PROMPT)
            # ---- 4. live traffic, in rounds, for the rest of the demo ----
            start_traffic()
            if drift:
                # ---- 5/6. hands-free: every sprint, v2 included, rolls out on the timer ----
                from demo_app import drift as drift_module
                _banner(f"Demo running. Dashboard: {url}",
                        f"Contract drift: the first sprint (v2) in {drift_interval:g}s, then one more every "
                        f"{drift_interval:g}s ({drift_module.MAX_STAGE} in all). No more keypresses needed.",
                        "Press Ctrl+C to stop.")
                stage = 0
                while stage < drift_module.MAX_STAGE:
                    if stop.wait(drift_interval):
                        raise KeyboardInterrupt
                    stage += 1
                    roll_out(stage)
                _banner("All sprints rolled out. Traffic keeps flowing on the final contract.",
                        "Press Ctrl+C to stop.")
            else:
                # ---- 5. wait ----
                pause_step(LIVE_CHANGE_PROMPT)
                # ---- 6. the breaking change: v2 contract on the running service; traffic follows next round ----
                roll_out(1)
                _banner(f"Demo running. Dashboard: {url}", "Traffic keeps flowing (v2 from the next round).",
                        "Press Ctrl+C to stop.")
        else:
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
        if live:
            stop.set()
            th = traffic_state["thread"]
            if th is not None and th.is_alive():
                th.join(timeout=15)
            result = traffic_state["result"]
            if result is not None:
                print("traffic summary:\n" + result.summary(), flush=True)
            stop_service()
        _shutdown_demo(stop, server, dash_thread, watch_stop, watch_thread, prism, proxy_server, proxy_thread)
    return 0


def _shutdown_demo(stop, server, dash_thread, watch_stop, watch_thread, prism, proxy_server=None, proxy_thread=None) -> None:
    """Stop replay, dashboard, mock proxy, watcher; the watcher's own cleanup stops Prism. A second
    Ctrl+C during shutdown skips the waiting but still kills Prism."""
    print("\nstopping demo...", flush=True)
    try:
        stop.set()
        server.should_exit = True
        if dash_thread.is_alive():
            dash_thread.join(timeout=5)
        if proxy_server is not None:
            proxy_server.should_exit = True
            if proxy_thread is not None and proxy_thread.is_alive():
                proxy_thread.join(timeout=5)
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
             llm_refine: bool = False, infer_errors: bool = False, mock_proxy: bool = False,
             live: bool = False, service_port: int = DEFAULT_SERVICE_PORT,
             drift: bool = False, drift_interval: float = DEFAULT_DRIFT_INTERVAL) -> int:
    return run_demo(log_path, spec_path, port=port, prism_port=prism_port, use_prism=not no_prism,
                    auto=auto, sample_delay=sample_delay, changed_delay=changed_delay, static=static,
                    llm_refine=llm_refine, infer_errors=infer_errors, mock_proxy=mock_proxy,
                    live=live or drift, service_port=service_port, drift=drift, drift_interval=drift_interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="API contract & mock generator from raw logs")
    sub = ap.add_subparsers(dest="cmd", required=True)

    llm_help = ("also ask the AIDH LLM (AIDH_BASE_URL/AIDH_DOMAIN_ID/AIDH_MODEL) for field and endpoint "
                "descriptions the rule-based inferrer can't produce; off by default, and any LLM failure "
                "just falls back to the rule-based spec")
    infer_errors_help = ("also guess plausible error statuses (404/400/401/403/409/429/500/405) this "
                         "traffic never happened to show, from evidence like path params, request "
                         "bodies and auth headers (see errors.py); each is tagged x-inferred so it's "
                         "never mistaken for something actually observed, and breaking-change detection "
                         "ignores them entirely — off by default")
    mock_proxy_help = ("the mock port is served by a thin proxy in front of Prism (Prism itself moves to "
                       "port+1): stateful 404s, real request validation, auth enforcement, chaos-injected "
                       "errors at realistic rates, and recorded examples for thinly-observed endpoints "
                       "(see mock_proxy.py) — on by default; --no-mock-proxy exposes bare Prism instead")

    sub.add_parser("llm-check", help="send one test prompt to AIDH and print the raw exchange (config/connectivity check)")

    b = sub.add_parser("build", help="infer spec once from a log file")
    b.add_argument("log_path")
    b.add_argument("-o", "--out", default="output/openapi.yaml")
    b.add_argument("--llm-refine", action="store_true", help=llm_help)
    b.add_argument("--infer-errors", action="store_true", help=infer_errors_help)

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
    w.add_argument("--infer-errors", action="store_true", help=infer_errors_help)
    w.add_argument("--mock-proxy", dest="mock_proxy", action="store_true", default=True, help=mock_proxy_help)
    w.add_argument("--no-mock-proxy", dest="mock_proxy", action="store_false", help="bare Prism on the mock port")

    d = sub.add_parser("dashboard", help="run the dashboard")
    d.add_argument("--host", default="0.0.0.0")
    d.add_argument("--port", type=int, default=8000)
    d.add_argument("--spec", default="output/openapi.yaml",
                   help="spec to show; changes.jsonl is read from the same folder")
    d.add_argument("--log", default="live_logs.jsonl",
                   help="traffic log to pull raw request/response samples from")

    m = sub.add_parser("demo", help="run the whole live demo: clean start, watcher + mock, dashboard, the "
                                    "passport service with continuous traffic, and a contract that keeps drifting")
    m.add_argument("--auto", action="store_true", help="don't wait for Enter between steps (for recording)")
    source = m.add_mutually_exclusive_group()
    source.add_argument("--replay", action="store_true",
                        help="the original flow instead: replay sample_logs.jsonl, then changed_logs.jsonl "
                             "(no passport service, no continuous traffic, no drift)")
    source.add_argument("--live", action="store_true",
                        help="passport service + continuous traffic, but stop after the v2 rollout (no further "
                             "sprints)")
    m.add_argument("--service-port", type=int, default=DEFAULT_SERVICE_PORT, help="passport service port")
    m.add_argument("--drift-interval", type=float, default=DEFAULT_DRIFT_INTERVAL,
                   help="seconds from traffic start to the v2 rollout, and between the sprints after it "
                        "(default 60; see demo_app/drift.py)")
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
    m.add_argument("--infer-errors", action="store_true", help=infer_errors_help)
    m.add_argument("--mock-proxy", dest="mock_proxy", action="store_true", default=True, help=mock_proxy_help)
    m.add_argument("--no-mock-proxy", dest="mock_proxy", action="store_false", help="bare Prism on the mock port")

    a = ap.parse_args()
    if a.cmd == "llm-check":
        raise SystemExit(cmd_llm_check())
    if a.cmd == "demo":
        raise SystemExit(cmd_demo(a.auto, a.port, a.prism_port, a.no_prism, a.log, a.spec,
                                  a.sample_delay, a.changed_delay, a.static, a.llm_refine, a.infer_errors,
                                  a.mock_proxy, live=not a.replay, service_port=a.service_port,
                                  drift=not (a.replay or a.live), drift_interval=a.drift_interval))
    if a.cmd == "build":
        raise SystemExit(cmd_build(a.log_path, a.out, a.llm_refine, a.infer_errors))
    if a.cmd == "watch":
        raise SystemExit(cmd_watch(a.log_path, a.spec, a.prism_port, a.no_prism, a.fresh, static=a.static,
                                   llm_refine=a.llm_refine, infer_errors=a.infer_errors,
                                   use_mock_proxy=a.mock_proxy))
    raise SystemExit(cmd_dashboard(a.host, a.port, a.spec, a.log))


if __name__ == "__main__":
    main()
