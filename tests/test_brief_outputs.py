"""The hackathon brief's required outputs: openapi.json from build/watch/demo, Prism in dynamic mode (-d)
with a --static switch, and a realistic contract from demo_app traffic. Prism is never needed."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import traffic  # noqa: E402
import watcher  # noqa: E402
from spec_builder import build_spec, spec_paths, validate_spec, write_spec  # noqa: E402

SAMPLE = ROOT / "sample_logs.jsonl"


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# ---------- 1. openapi.json ----------

def test_write_spec_writes_json_and_yaml(tmp_path):
    spec = build_spec([])
    yaml_path, json_path = write_spec(spec, tmp_path / "out" / "openapi.yaml")
    assert (yaml_path.name, json_path.name) == ("openapi.yaml", "openapi.json")
    assert load_json(json_path) == spec == yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    # openapi.mock.json is A4's Prism-only real-bounds variant, always written alongside; no temp files
    assert sorted(p.name for p in json_path.parent.iterdir()) == ["openapi.json", "openapi.mock.json", "openapi.yaml"]


def test_write_spec_accepts_the_json_path(tmp_path):
    yaml_path, json_path = write_spec(build_spec([]), tmp_path / "openapi.json")
    assert json_path == tmp_path / "openapi.json" and yaml_path == tmp_path / "openapi.yaml"
    assert yaml_path.exists() and json_path.exists()
    assert spec_paths(tmp_path / "a.yml") == (tmp_path / "a.yml", tmp_path / "a.json")


def test_build_prints_and_writes_openapi_json(tmp_path, capsys):
    out = tmp_path / "output" / "openapi.yaml"
    assert main.cmd_build(str(SAMPLE), str(out)) == 0
    json_path = tmp_path / "output" / "openapi.json"
    assert f"openapi.json     : {json_path}" in capsys.readouterr().out
    spec = load_json(json_path)
    assert validate_spec(spec) == [] and len(spec["paths"]) >= 5


def test_watch_writes_openapi_json_on_baseline_and_rebuild(tmp_path):
    log = tmp_path / "live.jsonl"
    spec_yaml, spec_json = tmp_path / "out" / "openapi.yaml", tmp_path / "out" / "openapi.json"
    cw = watcher.ContractWatcher(log, spec_yaml, tmp_path / "out" / "changes.jsonl")
    with contextlib.redirect_stdout(io.StringIO()):
        cw.initialize()
        assert load_json(spec_json)["paths"] == {}
        log.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        cw.process()
    assert load_json(spec_json) == yaml.safe_load(spec_yaml.read_text(encoding="utf-8"))
    assert len(load_json(spec_json)["paths"]) >= 5


def test_watch_fresh_removes_openapi_json_too(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "watch", lambda *a, **kw: None)
    out = tmp_path / "output"
    out.mkdir()
    for name in ("openapi.yaml", "openapi.json", "changes.jsonl"):
        (out / name).write_text("x", encoding="utf-8")
    assert main.cmd_watch(str(tmp_path / "live.jsonl"), str(out / "openapi.yaml"), 4010, True, fresh=True) == 0
    assert list(out.iterdir()) == []


def test_demo_writes_openapi_json(tmp_path):
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    out = tmp_path / "output"
    out.mkdir()
    (out / "openapi.json").write_text('{"stale": true}', encoding="utf-8")
    stop = threading.Event()
    seen = {}

    def pause(prompt: str) -> None:
        seen["json"] = load_json(out / "openapi.json")
        stop.set()

    rc = main.run_demo(tmp_path / "live.jsonl", out / "openapi.yaml", port=port, use_prism=False,
                       pause=pause, stop_event=stop, debounce=0.2)
    assert rc == 0
    assert seen["json"]["paths"] == {} and "stale" not in seen["json"]   # clean start, then the baseline


# ---------- 2. Prism dynamic (-d) / --static ----------

class _Popen:
    launched: list[list[str]] = []
    pid = 42

    def __init__(self, cmd, **kw):
        _Popen.launched.append(cmd)

    def poll(self):
        return None


@pytest.mark.parametrize("dynamic", [True, False])
def test_prism_command_uses_d_unless_static(tmp_path, monkeypatch, dynamic):
    monkeypatch.setattr(watcher, "find_prism", lambda: "/usr/bin/prism")
    monkeypatch.setattr(watcher.subprocess, "Popen", _Popen)
    _Popen.launched = []
    spec = tmp_path / "openapi.yaml"
    spec.write_text("openapi: 3.0.3\n", encoding="utf-8")
    pm = watcher.PrismManager(spec, 4012, dynamic=dynamic)
    pm.start()
    try:
        cmd = _Popen.launched[0]
        assert cmd[:3] == ["/usr/bin/prism", "mock", str(spec)]
        assert ("-d" in cmd) is dynamic
    finally:
        pm.proc = None
        pm._close_log()


def test_prism_is_dynamic_by_default():
    assert watcher.PrismManager().dynamic is True


@pytest.mark.parametrize("static", [False, True])
def test_watch_passes_static_to_prism(tmp_path, monkeypatch, static):
    seen = {}
    monkeypatch.setattr(watcher, "watch", lambda log, spec, changes, prism, **kw: seen.update(prism=prism))
    main.cmd_watch(str(tmp_path / "live.jsonl"), str(tmp_path / "openapi.yaml"), 4010, False, static=static)
    assert seen["prism"].dynamic is (not static)


def test_static_flag_on_the_cli(monkeypatch):
    calls = {}
    monkeypatch.setattr(main, "cmd_watch", lambda *a, **kw: calls.update(watch=kw) or 0)
    monkeypatch.setattr(main, "run_demo", lambda *a, **kw: calls.update(demo=kw) or 0)
    for argv in (["watch", "live.jsonl", "--static"], ["demo", "--static", "--auto"]):
        monkeypatch.setattr(sys, "argv", ["main.py", *argv])
        with pytest.raises(SystemExit):
            main.main()
    assert calls["watch"]["static"] is True and calls["demo"]["static"] is True
    monkeypatch.setattr(sys, "argv", ["main.py", "demo"])
    with pytest.raises(SystemExit):
        main.main()
    assert calls["demo"]["static"] is False


def test_demo_starts_prism_static_when_asked(tmp_path, monkeypatch):
    made = []

    class Recording(watcher.PrismManager):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    monkeypatch.setattr(watcher, "PrismManager", Recording)
    monkeypatch.setattr(watcher, "find_prism", lambda: None)   # "not installed": nothing is launched
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    stop = threading.Event()
    main.run_demo(tmp_path / "live.jsonl", tmp_path / "out" / "openapi.yaml", port=port, static=True,
                  pause=lambda p: stop.set(), stop_event=stop, debounce=0.2)
    assert made and made[0].dynamic is False


# ---------- 4. demo_app traffic -> openapi.json ----------

@pytest.fixture(scope="module")
def passport_spec(tmp_path_factory) -> dict:
    d = tmp_path_factory.mktemp("passport")
    log = d / "demo_logs.jsonl"
    with contextlib.redirect_stdout(io.StringIO()):
        assert traffic.main(["--in-process", "--log", str(log)]) == 0      # the brief's command, 500 requests
        assert main.cmd_build(str(log), str(d / "output" / "openapi.yaml")) == 0
    assert len(log.read_text(encoding="utf-8").splitlines()) == 500
    return load_json(d / "output" / "openapi.json")


def test_passport_openapi_json_is_valid_with_all_5_endpoints(passport_spec):
    assert validate_spec(passport_spec) == []
    ops = {(m.upper(), p) for p, item in passport_spec["paths"].items() for m in item}
    assert ops == {("POST", "/applications"), ("GET", "/applications"), ("GET", "/applications/{id}"),
                   ("PATCH", "/applications/{id}/status"), ("GET", "/offices")}


def _prop(schema: dict, *names: str) -> dict:
    for n in names:
        schema = schema["items"] if n == "[]" else schema["properties"][n]
    return schema


def test_passport_contract_has_realistic_mock_hints(passport_spec):
    post = passport_spec["paths"]["/applications"]["post"]
    req = post["requestBody"]["content"]["application/json"]["schema"]
    assert _prop(req, "email")["format"] == "email"
    assert _prop(req, "date_of_birth")["format"] == "date"
    assert _prop(req, "passport_type")["enum"] == ["express", "standard"]
    assert _prop(req, "pages")["x-observed-range"] == [32, 48]
    app = passport_spec["paths"]["/applications/{id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert _prop(app, "status")["enum"] == ["approved", "in_review", "issued", "rejected", "submitted"]
    assert _prop(app, "submitted_at")["format"] == "date-time"
    assert _prop(app, "status_history", "[]", "status")["enum"] == _prop(app, "status")["enum"]
    assert "enum" not in _prop(app, "full_name") and "enum" not in _prop(app, "id")   # open-ended values


def test_passport_examples_satisfy_their_schemas(passport_spec):
    """Prism --static serves these examples; with the added enum/format/range they must still validate."""
    from openapi_schema_validator import OAS30Validator
    checked = 0
    for path, item in passport_spec["paths"].items():
        for method, op in item.items():
            for status, resp in op["responses"].items():
                media = (resp.get("content") or {}).get("application/json")
                if media and "example" in media:
                    errors = [e.message for e in OAS30Validator(media["schema"]).iter_errors(media["example"])]
                    assert errors == [], (method, path, status, errors[:3])
                    checked += 1
    assert checked >= 10
