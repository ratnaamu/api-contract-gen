"""main.py — CLI entry point that wires the modules together.

    python main.py build  sample_logs.jsonl -o output/openapi.yaml   # one-shot
    python main.py watch  live_logs.jsonl                            # continuous mode + Prism
    python main.py dashboard --port 8000                             # FastAPI dashboard
"""
from __future__ import annotations

import argparse
from pathlib import Path

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


def cmd_watch(log_path: str, spec_path: str, prism_port: int, no_prism: bool) -> int:
    """Run watcher.watch (with PrismManager unless --no-prism)."""
    raise NotImplementedError


def cmd_dashboard(host: str, port: int) -> int:
    """uvicorn.run("dashboard.app:app", host=host, port=port)."""
    raise NotImplementedError


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

    d = sub.add_parser("dashboard", help="run the dashboard")
    d.add_argument("--host", default="0.0.0.0")
    d.add_argument("--port", type=int, default=8000)

    a = ap.parse_args()
    if a.cmd == "build":
        raise SystemExit(cmd_build(a.log_path, a.out))
    if a.cmd == "watch":
        raise SystemExit(cmd_watch(a.log_path, a.spec, a.prism_port, a.no_prism))
    raise SystemExit(cmd_dashboard(a.host, a.port))


if __name__ == "__main__":
    main()
