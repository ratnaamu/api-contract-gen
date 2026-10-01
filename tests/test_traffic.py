"""Tests for traffic.py, and the whole story: demo service logs -> contract -> v2 change detected."""
from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import traffic  # noqa: E402
from demo_app import create_app  # noqa: E402
from parser import parse_line, read_logs  # noqa: E402
from spec_builder import load_spec, validate_spec  # noqa: E402
from watcher import ContractWatcher  # noqa: E402


def run_in_process(log: Path, v2: bool = False, count: int = 500, seed: int = 7) -> traffic.Traffic:
    client = TestClient(create_app(v2=v2, log_path=log, error_rate=0.02, seed=seed))
    t = traffic.Traffic(client, v2=v2, seed=seed)
    t.run(count)
    return t


@pytest.fixture(scope="module")
def logs(tmp_path_factory) -> dict[str, Path]:
    """One v1 and one v2 run of exactly 500 requests each (shared: they take a few seconds)."""
    d = tmp_path_factory.mktemp("traffic")
    out = {"v1": d / "v1.jsonl", "v2": d / "v2.jsonl"}
    run_in_process(out["v1"])
    run_in_process(out["v2"], v2=True)
    return out


# ---------- the mix ----------

def test_exactly_500_requests_become_500_log_lines(logs):
    for path in logs.values():
        raw = path.read_text(encoding="utf-8").splitlines()
        assert len(raw) == 500
        assert all(parse_line(line) is not None for line in raw)


def test_mix_covers_valid_invalid_and_missing(logs):
    entries = read_logs(logs["v1"])
    statuses = Counter(e["status"] for e in entries)
    for code in (200, 201, 400, 404, 409, 500):
        assert statuses[code] > 0, statuses
    assert statuses[200] + statuses[201] > 250          # mostly successful traffic
    assert 5 <= statuses[500] <= 30                      # ~2% random server errors
    endpoints = Counter((e["method"], e["path"].split("/")[1], e["status"]) for e in entries)
    assert endpoints[("POST", "applications", 201)] >= 10  # enough to warm up
    methods = {e["method"] for e in entries}
    assert methods == {"GET", "POST", "PATCH"}


def test_every_endpoint_is_exercised(logs):
    from normalizer import normalize_path
    templates = {(e["method"], normalize_path(e["path"])[0]) for e in read_logs(logs["v1"])}
    assert templates == {("GET", "/offices"), ("POST", "/applications"), ("GET", "/applications"),
                         ("GET", "/applications/{id}"), ("PATCH", "/applications/{id}/status")}


def test_counters_match_log(tmp_path):
    t = run_in_process(tmp_path / "log.jsonl", count=120)
    assert t.sent == 120 == sum(t.by_status.values()) == sum(t.by_scenario.values())
    assert len(read_logs(tmp_path / "log.jsonl")) == 120
    assert "sent 120 requests" in t.summary()


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_small_counts_are_exact(tmp_path, count):
    t = run_in_process(tmp_path / "log.jsonl", count=count)
    assert t.sent == count
    assert len(read_logs(tmp_path / "log.jsonl")) == count


def test_same_seed_same_traffic(tmp_path):
    def shape(path: Path) -> list[tuple]:
        return [(e["method"], e["path"], json.dumps(e["query"], sort_keys=True), e["status"]) for e in read_logs(path)]
    run_in_process(tmp_path / "a.jsonl", count=150)
    run_in_process(tmp_path / "b.jsonl", count=150)
    run_in_process(tmp_path / "c.jsonl", count=150, seed=99)
    assert shape(tmp_path / "a.jsonl") == shape(tmp_path / "b.jsonl")
    assert shape(tmp_path / "a.jsonl") != shape(tmp_path / "c.jsonl")


def test_authorization_is_sent_but_never_logged(logs):
    text = logs["v1"].read_text(encoding="utf-8")
    auth = {e["headers"].get("Authorization") for e in read_logs(logs["v1"])}
    assert auth == {"Bearer <redacted>", None}
    assert not any(len(tok) == 32 for tok in text.split() if tok.strip('",').isalnum())


def test_v2_traffic_has_old_clients_getting_400(logs):
    entries = read_logs(logs["v2"])
    posts = [e for e in entries if e["method"] == "POST"]
    ok = [e for e in posts if e["status"] == 201]
    assert ok and all("birth_date" in e["request_body"] and "emergency_contact" in e["request_body"] for e in ok)
    old = [e for e in posts if isinstance(e["request_body"], dict) and "date_of_birth" in e["request_body"]]
    assert old and all(e["status"] in (400, 500) for e in old)


# ---------- CLI ----------

def test_cli_in_process(tmp_path, capsys):
    log = tmp_path / "cli.jsonl"
    assert traffic.main(["--in-process", "--log", str(log), "--count", "40", "--error-rate", "0"]) == 0
    assert len(read_logs(log)) == 40
    out = capsys.readouterr().out
    assert "sent 40 requests (API v1)" in out and "500" not in out.split("by status")[1].split("\n")[0]


def _cli(log: Path, *extra: str) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return traffic.main(["--in-process", "--log", str(log), *extra])


def test_in_process_overwrites_log_by_default(tmp_path):
    """The brief requires exactly 500 lines: running the command twice must not give 1000."""
    log = tmp_path / "demo_logs.jsonl"
    assert _cli(log) == 0
    first = log.read_text(encoding="utf-8")
    assert _cli(log) == 0
    assert len(log.read_text(encoding="utf-8").splitlines()) == 500
    assert all(parse_line(line) for line in log.read_text(encoding="utf-8").splitlines())
    assert len(first.splitlines()) == 500


def test_in_process_overwrite_replaces_unrelated_content(tmp_path):
    log = tmp_path / "logs" / "demo_logs.jsonl"          # parent folder doesn't exist yet
    log.parent.mkdir()
    log.write_text("old line\n" * 7, encoding="utf-8")
    assert _cli(log, "--count", "30") == 0
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 30 and "old line" not in lines


def test_in_process_append_keeps_existing_lines(tmp_path):
    log = tmp_path / "demo_logs.jsonl"
    assert _cli(log, "--count", "40") == 0
    assert _cli(log, "--count", "25", "--append") == 0
    assert len(read_logs(log)) == 65
    assert _cli(log, "--count", "10") == 0               # and a plain run overwrites again
    assert len(read_logs(log)) == 10


def test_in_process_creates_missing_parent_folder(tmp_path):
    log = tmp_path / "new" / "dir" / "demo_logs.jsonl"
    assert _cli(log, "--count", "5") == 0
    assert len(read_logs(log)) == 5


def test_append_without_in_process_warns(capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert traffic.main(["--url", f"http://127.0.0.1:{port}", "--append", "--count", "1"]) == 1
    assert "--append only applies with --in-process" in capsys.readouterr().err


def test_cli_without_server_fails_cleanly(capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]           # nothing listening here once closed
    assert traffic.main(["--url", f"http://127.0.0.1:{port}", "--count", "5"]) == 1
    assert "no demo service" in capsys.readouterr().err


def test_cli_against_real_server_autodetects_v2(tmp_path, capsys):
    import uvicorn
    log = tmp_path / "server.jsonl"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(v2=True, log_path=log, error_rate=0), host="127.0.0.1",
                                           port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        assert traffic.main(["--url", f"http://127.0.0.1:{port}", "--count", "60"]) == 0
    finally:
        server.should_exit = True
        th.join(timeout=10)
    assert "sent 60 requests (API v2)" in capsys.readouterr().out
    entries = read_logs(log)
    assert len(entries) == 60
    assert any(e["status"] == 201 and "birth_date" in e["response_body"] for e in entries)


# ---------- the whole story: logs -> contract -> change detected ----------

def test_v1_logs_build_a_valid_contract(logs, tmp_path, capsys):
    import main
    out = tmp_path / "openapi.yaml"
    assert main.cmd_build(str(logs["v1"]), str(out)) == 0
    spec = load_spec(out)
    assert validate_spec(spec) == []
    assert set(spec["paths"]) == {"/offices", "/applications", "/applications/{id}", "/applications/{id}/status"}
    post = spec["paths"]["/applications"]["post"]
    req = post["requestBody"]["content"]["application/json"]["schema"]
    assert {"full_name", "date_of_birth", "nationality", "email", "office_id"} <= set(req["required"])
    assert "phone" not in req.get("required", [])           # optional in the traffic -> optional in the spec
    limit = next(p for p in spec["paths"]["/applications"]["get"]["parameters"] if p["name"] == "limit")
    assert limit["schema"] == {"type": "integer"}           # ?limit=all only ever got 400s
    auth = [p for p in post.get("parameters", []) if p["name"].lower() == "authorization"]
    assert auth == []


def _replay(cw: ContractWatcher, source: Path, log: Path, chunk: int) -> list:
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    changes = []
    with contextlib.redirect_stdout(io.StringIO()):
        for i in range(0, len(lines), chunk):
            with log.open("a", encoding="utf-8", newline="\n") as f:
                f.write("".join(lines[i:i + chunk]))
            changes += cw.process()
    return changes


_SPRINT: dict[int, tuple[list, list, Path]] = {}


@pytest.fixture(params=[5, 25], ids=["rebuild-every-5", "rebuild-every-25"])
def sprint(request, logs, tmp_path_factory) -> tuple[list, list, Path]:
    """v1 traffic, then v2 traffic, through the real watcher; (v1 changes, v2 changes, changes.jsonl).
    A rebuild every 5 lines = 200 rebuilds (1 per line takes ~70s). Cached per batch size."""
    chunk = request.param
    if chunk not in _SPRINT:
        d = tmp_path_factory.mktemp(f"sprint{chunk}")
        log = d / "live.jsonl"
        log.touch()
        cw = ContractWatcher(log, d / "openapi.yaml", d / "changes.jsonl")
        with contextlib.redirect_stdout(io.StringIO()):
            cw.initialize()
            v1 = _replay(cw, logs["v1"], log, chunk)
            v2 = _replay(cw, logs["v2"], log, chunk)
        _SPRINT[chunk] = (v1, v2, d / "changes.jsonl")
    return _SPRINT[chunk]


def _key(c) -> tuple:
    return (c.kind, c.method, c.path, c.location)


def test_v1_traffic_has_no_false_alarms(sprint):
    v1, _, _ = sprint
    assert [c for c in v1 if c.breaking] == []
    assert [c for c in v1 if c.kind == "field_added"] == []   # everything in v1 is the baseline


def test_v2_sprint_change_is_exactly_these_breaking_changes(sprint):
    _, v2, _ = sprint
    assert {_key(c) for c in v2 if c.breaking} == {
        # the new required form field
        ("field_added", "POST", "/applications", "request.body.emergency_contact"),
        # the renamed field, in the request and in every response that carries an application
        ("field_renamed", "POST", "/applications", "request.body.date_of_birth"),
        ("field_renamed", "POST", "/applications", "response.201.body.date_of_birth"),
        ("field_renamed", "GET", "/applications/{id}", "response.200.body.date_of_birth"),
        ("field_renamed", "PATCH", "/applications/{id}/status", "response.200.body.date_of_birth"),
    }
    keys = [_key(c) for c in v2]
    assert len(keys) == len(set(keys))                        # nothing reported twice


def test_v2_new_required_request_field_is_breaking_from_the_start(sprint):
    _, v2, _ = sprint
    ec = [c for c in v2 if c.location.startswith("request.body.emergency_contact")]
    assert len(ec) == 1                                       # no "ok, new optional field" before it
    assert ec[0].kind == "field_added" and ec[0].breaking
    assert "new required field" in ec[0].detail


def test_v2_rename_replaces_removed_and_added(sprint):
    _, v2, _ = sprint
    renames = [c for c in v2 if c.kind == "field_renamed"]
    assert len(renames) == 4
    assert {c.detail for c in renames} == {"date_of_birth appears to be renamed to birth_date"}
    for c in v2:  # neither name shows up separately anywhere
        assert not (c.kind in ("field_removed", "field_added", "became_optional", "became_required")
                    and c.location.rsplit(".", 1)[-1] in ("date_of_birth", "birth_date")), c


def test_v2_new_response_fields_stay_ok(sprint):
    _, v2, _ = sprint
    added = {(c.method, c.location): c.breaking for c in v2 if c.kind == "field_added"
             and c.location.startswith("response.")}
    assert added == {("POST", "response.201.body.emergency_contact"): False,
                     ("GET", "response.200.body.emergency_contact"): False,
                     ("PATCH", "response.200.body.emergency_contact"): False}


def test_v2_changes_reach_changes_jsonl(sprint):
    _, _, changes_file = sprint
    rows = [json.loads(line) for line in changes_file.read_text(encoding="utf-8").splitlines()]
    renamed = [r for r in rows if r["kind"] == "field_renamed"]
    assert len(renamed) == 4 and all(r["breaking"] is True for r in renamed)
    assert sum(r["breaking"] for r in rows) == 5


# ---------- continuous mode ----------

class _Switching:
    """A client whose backend flips from v1 to v2 after `flip_after` /meta probes — a service restart."""

    def __init__(self, v1, v2, flip_after: int) -> None:
        self.v1, self.v2, self.flip_after, self.probes = v1, v2, flip_after, 0

    def _c(self):
        return self.v2 if self.probes > self.flip_after else self.v1

    def get(self, path, **kw):
        if path == "/meta":
            self.probes += 1
        return self._c().get(path, **kw)

    def request(self, method, path, **kw):
        return self._c().request(method, path, **kw)


def test_continuous_rounds_follow_a_service_restart(tmp_path):
    log = tmp_path / "cont.jsonl"
    v1 = TestClient(create_app(v2=False, log_path=log, error_rate=0))
    v2 = TestClient(create_app(v2=True, log_path=log, error_rate=0))
    said: list[str] = []
    t = traffic.run_continuous(_Switching(v1, v2, flip_after=2), min_count=20, max_count=30,
                               min_gap=0, max_gap=0, delay=0, rounds=4, say=said.append)
    assert len([m for m in said if "round" in m]) == 4
    assert 4 * 20 <= t.sent <= 4 * 30 and t.sent == sum(t.by_status.values())
    assert "v1 payloads" in said[0] and "v2 payloads" in said[-1]   # switched without a restart
    assert t.v2 is True
    # one Traffic object across rounds: applications created early are still known (and advanced) later
    assert t.known and t.by_scenario["patch_valid"] > 0
    assert len(read_logs(log)) == t.sent


def test_continuous_retries_while_the_service_is_down():
    class Down:
        def __init__(self): self.calls = 0
        def get(self, path, **kw):
            self.calls += 1
            raise ConnectionError("refused")
        def request(self, *a, **kw):
            raise ConnectionError("refused")

    stop = threading.Event()
    said: list[str] = []
    traffic.RETRY_GAP, saved = 0.01, traffic.RETRY_GAP
    try:
        threading.Timer(0.2, stop.set).start()
        t = traffic.run_continuous(Down(), min_count=5, max_count=5, min_gap=0, max_gap=0, rounds=3,
                                   stop=stop, say=said.append)
    finally:
        traffic.RETRY_GAP = saved
    assert t.sent == 0 and said and all("unreachable" in m for m in said)


def test_continuous_cli_rejects_in_process_and_bad_ranges():
    with pytest.raises(SystemExit):
        traffic.main(["--continuous", "--in-process"])
    with pytest.raises(SystemExit):
        traffic.main(["--continuous", "--min-count", "10", "--max-count", "5"])


def test_traffic_follows_the_contract_published_by_meta(tmp_path):
    """payload() is shaped by /meta's renames + required extras, so the generator survives every sprint;
    old-client payloads use the previous sprint's shape."""
    from demo_app import drift
    log = tmp_path / "drift.jsonl"
    client = TestClient(create_app(log_path=log, error_rate=0, stage=drift.MAX_STAGE))
    t = traffic.Traffic(client, v2=False, seed=3)
    t.run(60, 0)
    assert t.stage == drift.MAX_STAGE and t.v2 is True
    body = t.payload()
    assert {"birth_date", "citizenship", "emergency_contact", "consent"} <= set(body)
    assert "date_of_birth" not in body and "nationality" not in body
    old = t.payload(previous=True)                     # sprint 6 shape: has consent, still citizenship
    assert "consent" in old and "citizenship" in old and "created_at" not in old
    assert t.by_status[201] > 0 and t.by_scenario["create_valid"] > 0
    # every create_valid went through (the shape matched), only the deliberate bad ones were rejected
    posts = [r for r in read_logs(log) if r["method"] == "POST" and r["status"] == 201]
    assert posts and all(r["request_body"].get("consent") is True for r in posts)
