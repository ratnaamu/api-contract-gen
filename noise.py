"""noise.py — A7: inject realistic data-quality problems into a clean log file, so the pipeline's
graceful-degradation claims (Steps 1-3 of the review fix) can be measured against ground truth (see
score.py) instead of just asserted.

Contract:
    clean .jsonl file, a seed, per-mutation-type rates  ->  noisy .jsonl file (same line count)
Each line independently rolls each mutation type at its own rate (a line can pick up more than one).
Deterministic for a given seed, mirroring generate_logs.py's own random.Random(seed) pattern.
"""
from __future__ import annotations

import argparse
import json
import random
from http import HTTPStatus
from pathlib import Path
from typing import Any

DEFAULT_RATE = 0.1
MUTATION_KINDS = ("drop_field", "null_field", "casing", "version_mix", "truncate", "corrupt_json", "status_text")


def _status_phrase(status: Any) -> str:
    try:
        return HTTPStatus(int(status)).phrase
    except (ValueError, TypeError):
        return "Unknown"


class Mutator:
    """Applies each mutation kind independently, at its own rate, to one log entry (or raw line, for
    corrupt_json). `applied` tallies how many times each kind actually fired — useful for a demo
    ("here's a log at 20% noise" should be able to show what that 20% actually did)."""

    def __init__(self, seed: int, rate: float, **overrides: float) -> None:
        self.rng = random.Random(seed)
        self.rates = {kind: overrides.get(kind, rate) for kind in MUTATION_KINDS}
        self.applied: dict[str, int] = {kind: 0 for kind in MUTATION_KINDS}

    def _roll(self, kind: str) -> bool:
        return self.rng.random() < self.rates[kind]

    def _pick_field(self, body: dict[str, Any]) -> str | None:
        return self.rng.choice(list(body.keys())) if body else None

    def maybe_corrupt_line(self, line: str) -> str | None:
        """A whole-line JSON corruption (the "cut mid-object" case) — returns the corrupted line, or
        None if it didn't fire (the caller then proceeds to the field-level mutations instead)."""
        if not self._roll("corrupt_json"):
            return None
        self.applied["corrupt_json"] += 1
        cut = max(1, len(line) * 2 // 3)
        return line[:cut]

    def _mutate_body(self, body: dict[str, Any]) -> dict[str, Any]:
        body = dict(body)
        if self._roll("truncate"):
            # A truncated JSON *string* standing in for the whole object — parser.py's repair/flag
            # path (A1) is exactly what's meant to catch this.
            text = json.dumps(body)
            self.applied["truncate"] += 1
            return text[: max(4, len(text) * 2 // 3)]
        field = self._pick_field(body)
        if field and self._roll("drop_field"):
            del body[field]
            self.applied["drop_field"] += 1
            field = self._pick_field(body)
        if field and self._roll("null_field"):
            body[field] = None
            self.applied["null_field"] += 1
        field = self._pick_field(body)
        if field:
            if self._roll("casing"):
                variant = field.upper() if self.rng.random() < 0.5 else (field[:1].upper() + field[1:])
                if variant != field:
                    body[variant] = body.pop(field)
                    self.applied["casing"] += 1
            elif self._roll("version_mix"):
                body[f"{field}_v2"] = body.pop(field)
                self.applied["version_mix"] += 1
        return body

    def mutate_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        e = dict(entry)
        for key in ("request_body", "response_body"):
            body = e.get(key)
            if isinstance(body, dict):
                e[key] = self._mutate_body(body)
        if self._roll("status_text"):
            e["status"] = f"{e.get('status')} {_status_phrase(e.get('status'))}"
            self.applied["status_text"] += 1
        return e


def mutate_file(src: Path, dst: Path, seed: int, rate: float = DEFAULT_RATE, **overrides: float) -> dict[str, int]:
    """Mutate every line of `src` into `dst`. Returns how many times each mutation kind fired."""
    mutator = Mutator(seed, rate, **overrides)
    out_lines: list[str] = []
    with src.open("r", encoding="utf-8-sig") as f:
        for raw in f:
            raw = raw.rstrip("\n")
            if not raw.strip():
                out_lines.append(raw)
                continue
            corrupted = mutator.maybe_corrupt_line(raw)
            if corrupted is not None:
                out_lines.append(corrupted)
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                out_lines.append(raw)  # already-invalid input; leave it as its own kind of noise
                continue
            if not isinstance(entry, dict):
                out_lines.append(raw)
                continue
            out_lines.append(json.dumps(mutator.mutate_entry(entry), ensure_ascii=False))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return mutator.applied


def main() -> None:
    ap = argparse.ArgumentParser(description="Inject realistic data-quality noise into a clean log file")
    ap.add_argument("log_path")
    ap.add_argument("-o", "--output", default="noisy_logs.jsonl")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--rate", type=float, default=DEFAULT_RATE,
                    help="base probability for every mutation type (default 0.1)")
    for kind in MUTATION_KINDS:
        ap.add_argument(f"--{kind.replace('_', '-')}-rate", type=float, default=None,
                        help=f"override --rate for the {kind.replace('_', ' ')} mutation")
    a = ap.parse_args()
    overrides = {kind: v for kind in MUTATION_KINDS
                if (v := getattr(a, f"{kind}_rate")) is not None}
    applied = mutate_file(Path(a.log_path), Path(a.output), a.seed, a.rate, **overrides)
    print(f"wrote {a.output}")
    for kind, n in applied.items():
        print(f"  {kind:12} {n}")


if __name__ == "__main__":
    main()
