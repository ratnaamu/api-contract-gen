"""watcher.py — continuous mode: watch the log file, re-infer, diff the spec, restart Prism.   Owner: [Name 3]

Contract:
    log file changes  ->  output/openapi.yaml (rewritten)  +  output/changes.jsonl (appended SpecChange rows)
The dashboard only reads those two files, so dashboard and watcher can be built/run independently.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from models import LogEntry, SpecChange

DEFAULT_SPEC_PATH = Path("output/openapi.yaml")
DEFAULT_CHANGES_PATH = Path("output/changes.jsonl")
DEFAULT_PRISM_PORT = 4010


def build_from_entries(entries: list[LogEntry]) -> dict[str, Any]:
    """Full pipeline on in-memory entries: normalizer.group_by_endpoint -> inferrer.infer_all -> spec_builder.build_spec."""
    raise NotImplementedError


def diff_specs(old: dict[str, Any] | None, new: dict[str, Any]) -> list[SpecChange]:
    """Compare two OpenAPI dicts. `old=None` means every endpoint is 'endpoint_added'.

    Breaking (breaking=True):
      endpoint_removed, status_removed (2xx), field_removed from a response,
      type_changed anywhere, became_required in a request body.
    Non-breaking: endpoint_added, status_added, field_added, became_optional.
    Walk nested properties/items; `location` is a dotted path like "response.200.body.address.city".
    """
    raise NotImplementedError


def append_changes(changes: list[SpecChange], path: str | Path = DEFAULT_CHANGES_PATH) -> None:
    """Append each change as one JSON line (dataclasses.asdict)."""
    raise NotImplementedError


class PrismManager:
    """Runs `prism mock <spec> --port <port> --host 0.0.0.0 --dynamic` as a subprocess."""

    def __init__(self, spec_path: str | Path = DEFAULT_SPEC_PATH, port: int = DEFAULT_PRISM_PORT) -> None:
        self.spec_path = Path(spec_path)
        self.port = port
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        """Start Prism if not running. On Windows the executable is `prism.cmd` (use shutil.which)."""
        raise NotImplementedError

    def stop(self) -> None:
        """Terminate Prism (wait up to 5s, then kill). No-op if not running."""
        raise NotImplementedError

    def restart(self) -> None:
        """stop() then start(). Called after every spec rewrite."""
        raise NotImplementedError

    def is_running(self) -> bool:
        raise NotImplementedError


def watch(
    log_path: str | Path,
    spec_path: str | Path = DEFAULT_SPEC_PATH,
    changes_path: str | Path = DEFAULT_CHANGES_PATH,
    prism: PrismManager | None = None,
    debounce_seconds: float = 1.0,
    on_update: Callable[[dict[str, Any], list[SpecChange]], None] | None = None,
) -> None:
    """Block forever (Ctrl+C to exit).

    1. Write an initial spec from whatever is already in log_path (empty spec if file missing/empty), start Prism.
    2. watchdog Observer on log_path's directory; on modify of log_path, debounce, then
       parser.read_new_logs -> add to accumulated entries -> build_from_entries -> diff_specs vs previous
       -> if changed: write_spec, append_changes, prism.restart(), on_update(spec, changes).
    Must survive bad log lines and invalid intermediate specs (log the error, keep the last good spec).
    """
    raise NotImplementedError
