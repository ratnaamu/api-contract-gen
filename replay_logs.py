"""Stream log lines into a live file to simulate traffic (demo steps 2 and 4).

    python replay_logs.py sample_logs.jsonl live_logs.jsonl --delay 0.2
    python replay_logs.py changed_logs.jsonl live_logs.jsonl --delay 0.5
"""
import argparse
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("target")
    ap.add_argument("--delay", type=float, default=0.2, help="seconds between lines")
    a = ap.parse_args()
    with open(a.source, encoding="utf-8") as src, open(a.target, "a", encoding="utf-8", newline="\n") as dst:
        for i, line in enumerate(src, 1):
            dst.write(line if line.endswith("\n") else line + "\n")
            dst.flush()
            print(f"\r{i} lines streamed", end="")
            time.sleep(a.delay)
    print()


if __name__ == "__main__":
    main()
