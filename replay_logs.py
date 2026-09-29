"""Stream log lines into a live file to simulate traffic (demo steps 2 and 4).

    python replay_logs.py sample_logs.jsonl live_logs.jsonl --delay 0.2
    python replay_logs.py changed_logs.jsonl live_logs.jsonl --delay 0.5
"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from typing import Callable


def replay(
    source: str | Path,
    target: str | Path,
    delay: float = 0.2,
    stop: threading.Event | None = None,
    on_line: Callable[[int], None] | None = None,
) -> int:
    """Append `source`'s lines to `target` one at a time, `delay` seconds apart (each line flushed, and
    always newline-terminated). Stops early when `stop` is set. Returns the number of lines written."""
    written = 0
    with open(source, encoding="utf-8") as src, open(target, "a", encoding="utf-8", newline="\n") as dst:
        for line in src:
            if stop is not None and stop.is_set():
                break
            if not line.strip():
                continue
            dst.write(line if line.endswith("\n") else line + "\n")
            dst.flush()
            written += 1
            if on_line is not None:
                on_line(written)
            if delay > 0:
                if stop is not None:
                    if stop.wait(delay):
                        break
                else:
                    time.sleep(delay)
    return written


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("target")
    ap.add_argument("--delay", type=float, default=0.2, help="seconds between lines")
    a = ap.parse_args()
    replay(a.source, a.target, a.delay, on_line=lambda i: print(f"\r{i} lines streamed", end="", flush=True))
    print()


if __name__ == "__main__":
    main()
