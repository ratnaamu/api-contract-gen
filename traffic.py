"""Send exactly N (default 500) mixed requests to the passport demo service, producing its JSON-line log.

    python -m demo_app &                          # v1 service on :8001, logging to live_logs.jsonl
    python traffic.py                             # 500 requests (API version auto-detected)
    python traffic.py --delay 0.02                # paced, nicer to watch live on the dashboard
    python traffic.py --continuous                # rounds of 50-150 requests every 5-7s until Ctrl+C;
                                                  # re-detects v1/v2 each round, survives a service restart

    python traffic.py --in-process --log demo_logs.jsonl          # no server needed: writes the log directly
    python traffic.py --in-process --v2 --log demo_v2_logs.jsonl
    python traffic.py --in-process --v2 --log demo_logs.jsonl --append   # add to an existing log

With --in-process the log file is overwritten by default, so a run leaves exactly --count lines;
--append keeps what is already there. (Against a running server the server owns its log file.)

The mix (seeded, so a run is reproducible): valid submissions, validation errors (400), lookups of
existing and missing applications (200/404), status filters incl. invalid ones, status changes incl.
forbidden transitions (409), office listings. In v2, a share of submissions still uses the v1 shape
(an "old client") and gets 400s — the realistic fallout of the new required field.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import uuid
from collections import Counter
from datetime import date, timedelta
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8001"
DEFAULT_COUNT = 500

OFFICES = {  # id -> services (mirrors demo_app; refreshed from GET /offices at start)
    "OFF-LON": ["standard", "express"], "OFF-MAN": ["standard"], "OFF-EDI": ["standard", "express"],
    "OFF-BLR": ["standard"], "OFF-NYC": ["standard", "express"],
}
STATUSES = ["submitted", "in_review", "approved", "rejected", "issued"]
NEXT = {"submitted": ["in_review", "rejected"], "in_review": ["approved", "rejected"], "approved": ["issued"],
        "rejected": [], "issued": []}

FIRST = ["Aarav", "Sofia", "Liam", "Mei", "Olivia", "Kwame", "Inês", "Noah", "Priya", "Lucas", "Amara", "Jonas",
         "Chen", "Fatima", "Mateo", "Yuki", "Hannah", "Omar", "Zara", "Diego"]
LAST = ["Sharma", "Garcia", "Smith", "Tanaka", "Brown", "Mensah", "Silva", "Müller", "Iyer", "Rossi", "Okafor",
        "Nguyen", "Kowalski", "Haddad", "Johansson", "Costa"]
CITIES = [("London", "GB", "SW1A 1AA"), ("Manchester", "GB", "M1 1AE"), ("Edinburgh", "GB", "EH1 1YZ"),
          ("Bengaluru", "IN", "560001"), ("New York", "US", "10001"), ("Lisbon", "PT", "1100-148")]
AGENTS = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5)",
          "PassportKiosk/2.3", "passport-mobile/5.1 (Android 14)", "partner-integration/1.0 python-requests/2.32"]

# scenario -> weight (percent)
MIX = [
    ("create_valid", 25), ("create_invalid", 8),
    ("get_existing", 22), ("get_missing", 6),
    ("list_valid", 15), ("list_invalid", 3),
    ("patch_valid", 12), ("patch_conflict", 3), ("patch_invalid", 2), ("patch_missing", 2),
    ("offices", 2),
]


# Request-shape rules the generator follows (the same structure GET /meta publishes — see
# demo_app/drift.py): which canonical fields are sent under another name, and which extra fields the
# service requires. Stage 0 (v1) and stage 1 (v2) are built in for runs with no /meta to ask.
Contract = dict[str, Any]
CONTRACT_V1: Contract = {"stage": 0, "name": "v1", "version": 1,
                         "request": {"renames": {}, "required": []}, "previous": None}
CONTRACT_V2: Contract = {"stage": 1, "name": "v2", "version": 2,
                         "request": {"renames": {"date_of_birth": "birth_date"}, "required": ["emergency_contact"]},
                         "previous": {"stage": 0, "name": "v1", "request": CONTRACT_V1["request"]}}


class Traffic:
    def __init__(self, client: Any, v2: bool, seed: int = 7) -> None:
        self.client = client
        self.contract: Contract = CONTRACT_V2 if v2 else CONTRACT_V1
        self.rng = random.Random(seed)
        self.known: dict[str, str] = {}              # application id -> last known status
        self.by_scenario: Counter[str] = Counter()
        self.by_status: Counter[int] = Counter()
        self.sent = 0

    # ---- contract ----
    @property
    def v2(self) -> bool:
        """True from the v2 sprint on (kept for summaries/tests; `contract` is the precise state)."""
        return int(self.contract.get("stage", 0)) >= 1

    @v2.setter
    def v2(self, value: bool) -> None:
        self.contract = CONTRACT_V2 if value else CONTRACT_V1

    @property
    def stage(self) -> int:
        return int(self.contract.get("stage", 0))

    def adopt(self, meta: dict[str, Any] | None) -> None:
        """Follow the contract GET /meta describes (ignored if it doesn't look like one)."""
        if isinstance(meta, dict) and isinstance(meta.get("request"), dict):
            self.contract = meta
        elif isinstance(meta, dict) and "version" in meta:  # an older service with just a version
            self.v2 = int(meta["version"]) == 2

    def _rules(self, previous: bool = False) -> dict[str, Any]:
        if previous:
            prev = self.contract.get("previous")
            return prev["request"] if isinstance(prev, dict) and prev.get("request") else self.contract["request"]
        return self.contract["request"]

    def field(self, canonical: str, previous: bool = False) -> str:
        """The name the service currently expects for a canonical request field."""
        return self._rules(previous)["renames"].get(canonical, canonical)

    # ---- plumbing ----
    def headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": self.rng.choice(AGENTS),
             "X-Request-ID": str(uuid.UUID(int=self.rng.getrandbits(128)))}
        if self.rng.random() < 0.8:  # most clients authenticate; the middleware must mask this
            h["Authorization"] = "Bearer " + "".join(self.rng.choice("abcdef0123456789") for _ in range(32))
        return h

    def send(self, scenario: str, method: str, path: str, **kw: Any) -> Any:
        r = self.client.request(method, path, headers=self.headers(), **kw)
        self.sent += 1
        self.by_scenario[scenario] += 1
        self.by_status[r.status_code] += 1
        try:
            body = r.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "id" in body and "status" in body and r.status_code in (200, 201):
            self.known[body["id"]] = body["status"]
        return r, body

    # ---- payloads ----
    def _extra(self, name: str, family_name: str) -> Any:
        """A valid value for one of the contract's extra required fields."""
        rng = self.rng
        if name == "emergency_contact":
            contact: dict[str, Any] = {"name": f"{rng.choice(FIRST)} {family_name}",
                                       "phone": f"+44 7{rng.randint(100, 999)} {rng.randint(100000, 999999)}"}
            if rng.random() < 0.7:
                contact["relationship"] = rng.choice(["parent", "partner", "sibling", "friend"])
            return contact
        if name == "consent":
            return True
        return "yes"  # an extra this generator predates: send *something* rather than fail the sprint

    def payload(self, v2_shape: bool | None = None, previous: bool = False) -> dict[str, Any]:
        """A valid submission in the current contract's shape (`previous=True`: the sprint before — what an
        un-upgraded client sends). `v2_shape` is the pre-drift spelling: True/False pins v2/v1."""
        rng = self.rng
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        office = rng.choice(sorted(OFFICES))
        body: dict[str, Any] = {
            "full_name": name,
            "nationality": rng.choice(["GB", "IN", "US", "PT", "DE", "NG", "JP"]),
            "email": f"{name.split()[0].lower()}.{rng.randint(1, 999)}@example.com",
            "passport_type": rng.choice(OFFICES[office]),
            "pages": rng.choice([32, 32, 48]),
            "office_id": office,
            "date_of_birth": (date(1950, 1, 1) + timedelta(days=rng.randint(0, 20000))).isoformat(),
        }
        if rng.random() < 0.6:
            body["phone"] = f"+44 7{rng.randint(100, 999)} {rng.randint(100000, 999999)}"
        if rng.random() < 0.4:
            city, country, postal = rng.choice(CITIES)
            body["address"] = {"street": f"{rng.randint(1, 250)} {rng.choice(['High St', 'Oak Ave', 'Rua Augusta', 'MG Road'])}",
                               "city": city, "postal_code": postal, "country": country}
        if rng.random() < 0.2:
            body["previous_passport_number"] = "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ0123456789") for _ in range(9))
        if v2_shape is None:
            rules = self._rules(previous)
        else:
            rules = (CONTRACT_V2 if v2_shape else CONTRACT_V1)["request"]
        for canonical, public in rules["renames"].items():
            if canonical in body:
                body[public] = body.pop(canonical)
        for extra in rules["required"]:
            body[extra] = self._extra(extra, name.split()[1])
        return body

    def invalid_payload(self) -> tuple[dict[str, Any] | None, bytes | None]:
        """(json, raw) — exactly one of them set. Every variant must be rejected with 400."""
        body = self.payload()
        kinds = ["missing", "email", "future_dob", "office", "express", "nationality", "pages", "malformed"]
        if "emergency_contact" in self._rules()["required"]:
            kinds.append("contact")
        kind = self.rng.choice(kinds)
        dob_key, nat_key = self.field("date_of_birth"), self.field("nationality")
        if kind == "missing":
            body.pop(self.rng.choice(["full_name", "email", "office_id", dob_key, nat_key]))
        elif kind == "email":
            body["email"] = body["email"].replace("@", " at ")
        elif kind == "future_dob":
            body[dob_key] = (date.today() + timedelta(days=30)).isoformat()
        elif kind == "office":
            body["office_id"] = "OFF-XXX"
        elif kind == "express":
            body["office_id"], body["passport_type"] = "OFF-MAN", "express"
        elif kind == "nationality":
            body[nat_key] = "Portugal"
        elif kind == "pages":
            body["pages"] = 40
        elif kind == "contact":
            body["emergency_contact"]["phone"] = "call me"
        else:
            return None, b'{"full_name": "Broken JSON", '
        return body, None

    # ---- scenarios ----
    def pick_known(self, needs_next_status: bool = False) -> str | None:
        """A known application id; with needs_next_status, one that still has an allowed transition."""
        ids = sorted(i for i, s in self.known.items() if not needs_next_status or NEXT[s])
        return self.rng.choice(ids) if ids else None

    def missing_id(self) -> str:
        return f"PA-{self.rng.randint(900000, 999999):06d}"

    def run_scenario(self, scenario: str) -> None:
        rng = self.rng
        if scenario == "create_valid":
            # after any sprint, some clients still send the previous sprint's shape -> 400 (if the
            # sprint changed the request at all; a response-only sprint leaves them working)
            old_client = self.stage >= 1 and rng.random() < 0.1
            self.send("create_old_client" if old_client else scenario, "POST", "/applications",
                      json=self.payload(previous=old_client))
        elif scenario == "create_invalid":
            body, raw = self.invalid_payload()
            if raw is not None:
                self._send_raw(scenario, raw)
            else:
                self.send(scenario, "POST", "/applications", json=body)
        elif scenario == "get_existing":
            app_id = self.pick_known()
            self.send(scenario, "GET", f"/applications/{app_id}" if app_id else f"/applications/{self.missing_id()}")
        elif scenario == "get_missing":
            self.send(scenario, "GET", f"/applications/{self.missing_id()}")
        elif scenario == "list_valid":
            params: dict[str, Any] = {}
            if rng.random() < 0.8:
                params["status"] = rng.choice(STATUSES)
            if rng.random() < 0.3:
                params["office_id"] = rng.choice(sorted(OFFICES))
            if rng.random() < 0.4:
                params["limit"] = rng.choice([5, 10, 50])
            self.send(scenario, "GET", "/applications", params=params)
        elif scenario == "list_invalid":
            params = rng.choice([{"status": "pending"}, {"status": "APPROVED"}, {"limit": 0}, {"limit": "all"}])
            self.send(scenario, "GET", "/applications", params=params)
        elif scenario == "patch_valid":
            app_id = self.pick_known(needs_next_status=True)
            if app_id is None:
                return self.run_scenario("get_existing")
            body: dict[str, Any] = {"status": rng.choice(NEXT[self.known[app_id]])}
            if rng.random() < 0.4:
                body["note"] = rng.choice(["documents verified", "photo accepted", "biometrics complete",
                                           "identity check passed"])
            self.send(scenario, "PATCH", f"/applications/{app_id}/status", json=body)
        elif scenario == "patch_conflict":
            app_id = self.pick_known()  # every status has at least one forbidden target
            if app_id is None:
                return self.run_scenario("get_missing")
            current = self.known[app_id]
            forbidden = [s for s in STATUSES if s not in NEXT[current]]
            self.send(scenario, "PATCH", f"/applications/{app_id}/status", json={"status": rng.choice(forbidden)})
        elif scenario == "patch_invalid":
            app_id = self.pick_known() or self.missing_id()
            body = rng.choice([{"status": "done"}, {"state": "approved"}, {"status": "approved", "note": "x" * 300}])
            self.send(scenario, "PATCH", f"/applications/{app_id}/status", json=body)
        elif scenario == "patch_missing":
            self.send(scenario, "PATCH", f"/applications/{self.missing_id()}/status", json={"status": "in_review"})
        elif scenario == "offices":
            self.send(scenario, "GET", "/offices")
        else:
            raise ValueError(scenario)

    def _send_raw(self, scenario: str, raw: bytes) -> None:
        h = self.headers()
        h["Content-Type"] = "application/json"
        r = self.client.request("POST", "/applications", headers=h, content=raw)
        self.sent += 1
        self.by_scenario[scenario] += 1
        self.by_status[r.status_code] += 1

    # ---- driver ----
    def run(self, count: int = DEFAULT_COUNT, delay: float = 0.0, stop: Any = None) -> None:
        """Send `count` more requests (exactly `count` on a fresh instance). The first two of a run
        discover offices and existing applications. `stop` (a threading.Event) ends the run early."""
        if count <= 0:
            return
        target = self.sent + count
        self.adopt(fetch_meta(self.client))  # follow whatever sprint the service is on right now
        _, offices = self.send("offices", "GET", "/offices")
        if isinstance(offices, list):
            for o in offices:
                if isinstance(o, dict) and "id" in o:
                    OFFICES[o["id"]] = list(o.get("services") or ["standard"])
        if self.sent < target:
            _, listing = self.send("list_valid", "GET", "/applications", params={"limit": 100})
            for item in (listing or {}).get("items", []) if isinstance(listing, dict) else []:
                self.known[item["id"]] = item["status"]
        names, weights = zip(*MIX)
        while self.sent < target and not (stop is not None and stop.is_set()):
            if delay > 0:
                time.sleep(delay)
            self.run_scenario(self.rng.choices(names, weights)[0])

    def summary(self) -> str:
        classes = Counter(f"{s // 100}xx" for s in self.by_status.elements())
        lines = [f"sent {self.sent} requests (API v{2 if self.v2 else 1})",
                 f"  contract  : sprint {self.stage} ({self.contract.get('name', '?')})",
                 "  by status : " + ", ".join(f"{k} {v}" for k, v in sorted(self.by_status.items())),
                 "  by class  : " + ", ".join(f"{k} {v}" for k, v in sorted(classes.items())),
                 "  scenarios : " + ", ".join(f"{k} {v}" for k, v in sorted(self.by_scenario.items()))]
        return "\n".join(lines)


def fetch_meta(client: Any) -> dict[str, Any] | None:
    """GET /meta as a dict, or None if the service isn't there (or has no /meta)."""
    try:
        r = client.get("/meta")
        data = r.json() if r.status_code == 200 else None
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def detect_version(client: Any) -> int | None:
    meta = fetch_meta(client)
    try:
        return int(meta["version"]) if meta else None
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Continuous mode: rounds of traffic for as long as the demo runs
# ---------------------------------------------------------------------------

DEFAULT_MIN_COUNT, DEFAULT_MAX_COUNT = 50, 150   # requests per round
DEFAULT_MIN_GAP, DEFAULT_MAX_GAP = 5.0, 7.0      # seconds between rounds
RETRY_GAP = 2.0                                  # seconds between attempts while the service is down


def run_continuous(client: Any, *, seed: int = 7, min_count: int = DEFAULT_MIN_COUNT,
                   max_count: int = DEFAULT_MAX_COUNT, min_gap: float = DEFAULT_MIN_GAP,
                   max_gap: float = DEFAULT_MAX_GAP, delay: float = 0.02, rounds: int | None = None,
                   force_v2: bool | None = None, stop: Any = None,
                   say: Any = print) -> Traffic:
    """Keep sending traffic in rounds: each round is a random `min_count`..`max_count` requests, then a
    random `min_gap`..`max_gap` second pause. Runs until `rounds` is reached, `stop` (threading.Event) is
    set, or Ctrl+C. Returns the Traffic object (cumulative counters for the summary).

    One Traffic object lives for the whole session — its RNG and the application ids it has created
    carry over, so later rounds look up and advance applications made earlier instead of restarting
    from nothing (and successive rounds don't repeat the same seeded sequence).

    The API version is re-detected from /meta before every round (unless `force_v2` pins it), so
    restarting the service as v2 flips the payload shape automatically. A service that is down
    (restarting, say) is retried every RETRY_GAP seconds rather than ending the run."""
    import threading
    stop = stop or threading.Event()
    pacing = random.Random(seed ^ 0x5EED)  # round sizes/gaps: separate stream, so the traffic itself
    t = Traffic(client, v2=bool(force_v2), seed=seed)  # stays identical to a one-shot run's
    done = 0
    while not stop.is_set() and (rounds is None or done < rounds):
        meta = fetch_meta(client)
        if meta is None:
            say("traffic: service unreachable, retrying...")
            if stop.wait(RETRY_GAP):
                break
            continue
        if force_v2 is not None:
            t.v2 = bool(force_v2)
        else:
            before_stage = t.stage
            t.adopt(meta)
            if done and t.stage != before_stage:
                say(f"traffic: service moved to sprint {t.stage} ({t.contract.get('name')}); following it")
        n = pacing.randint(min_count, max_count)
        before = t.sent
        try:
            t.run(n, delay, stop=stop)
        except Exception as e:  # noqa: BLE001 — the service went away mid-round; next loop retries
            say(f"traffic: round {done + 1} aborted after {t.sent - before} requests: {type(e).__name__}: {e}")
            if stop.wait(RETRY_GAP):
                break
            continue
        done += 1
        say(f"traffic: round {done}: {t.sent - before} requests (v{2 if t.v2 else 1} payloads, sprint {t.stage}), "
            f"{t.sent} total")
        if rounds is not None and done >= rounds:
            break
        if stop.wait(pacing.uniform(min_gap, max_gap)):
            break
    return t


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help="base URL of a running demo service")
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between requests")
    ver = ap.add_mutually_exclusive_group()
    ver.add_argument("--v2", action="store_true", help="send v2-shaped payloads (default: auto-detect)")
    ver.add_argument("--v1", action="store_true", help="send v1-shaped payloads (default: auto-detect)")
    ap.add_argument("--in-process", action="store_true",
                    help="run the service inside this process (no server needed); implies --log")
    ap.add_argument("--log", default="live_logs.jsonl",
                    help="with --in-process: where the service logs (overwritten unless --append)")
    ap.add_argument("--append", action="store_true",
                    help="with --in-process: add to the log file instead of overwriting it")
    ap.add_argument("--error-rate", type=float, default=0.02, help="with --in-process: random 500 rate")
    cont = ap.add_argument_group("continuous mode",
                                 "keep sending rounds of traffic until Ctrl+C (the API version is "
                                 "re-detected every round, so restarting the service as v2 just works)")
    cont.add_argument("--continuous", action="store_true", help="rounds of traffic instead of one --count run")
    cont.add_argument("--min-count", type=int, default=DEFAULT_MIN_COUNT, help="requests per round, lower bound")
    cont.add_argument("--max-count", type=int, default=DEFAULT_MAX_COUNT, help="requests per round, upper bound")
    cont.add_argument("--min-gap", type=float, default=DEFAULT_MIN_GAP, help="seconds between rounds, lower bound")
    cont.add_argument("--max-gap", type=float, default=DEFAULT_MAX_GAP, help="seconds between rounds, upper bound")
    cont.add_argument("--rounds", type=int, default=None, help="stop after this many rounds (default: run until Ctrl+C)")
    a = ap.parse_args(argv)
    if a.continuous and a.in_process:
        ap.error("--continuous needs a running service (--url); it does not combine with --in-process")
    if a.continuous and (a.min_count < 1 or a.max_count < a.min_count or a.min_gap < 0 or a.max_gap < a.min_gap):
        ap.error("--min-count/--max-count/--min-gap/--max-gap must satisfy 1 <= min-count <= max-count and 0 <= min-gap <= max-gap")

    if a.append and not a.in_process:
        print("warning: --append only applies with --in-process (a running server owns its log file)",
              file=sys.stderr)

    if a.in_process:
        from pathlib import Path

        from fastapi.testclient import TestClient

        from demo_app import create_app
        log = Path(a.log)
        if not a.append:
            # Truncate rather than delete: a watcher tailing this file sees a normal truncation.
            log.parent.mkdir(parents=True, exist_ok=True)
            try:
                with log.open("w", encoding="utf-8"):
                    pass
            except OSError as e:
                print(f"error: could not overwrite {log}: {e} (use --append to add to it)", file=sys.stderr)
                return 1
        v2 = a.v2
        client = TestClient(create_app(v2=v2, log_path=a.log, error_rate=a.error_rate, seed=a.seed))
        where = f"in-process service -> {a.log} ({'appending' if a.append else 'overwritten'})"
    else:
        import httpx
        client = httpx.Client(base_url=a.url, timeout=10)
        server_version = detect_version(client)
        if server_version is None:
            print(f"error: no demo service at {a.url}. Start it with: python -m demo_app", file=sys.stderr)
            return 1
        v2 = a.v2 or (not a.v1 and server_version == 2)
        if (a.v2 and server_version != 2) or (a.v1 and server_version != 1):
            print(f"warning: service runs v{server_version} but sending v{2 if v2 else 1} payloads", file=sys.stderr)
        where = f"{a.url} (service v{server_version})"

    if a.continuous:
        print(f"continuous traffic to {where}: {a.min_count}-{a.max_count} requests every "
              f"{a.min_gap:g}-{a.max_gap:g}s" + (f", {a.rounds} rounds" if a.rounds else ", Ctrl+C to stop"), flush=True)
        force_v2 = True if a.v2 else (False if a.v1 else None)
        t = None
        try:
            t = run_continuous(client, seed=a.seed, min_count=a.min_count, max_count=a.max_count,
                               min_gap=a.min_gap, max_gap=a.max_gap, delay=a.delay or 0.02,
                               rounds=a.rounds, force_v2=force_v2)
        except KeyboardInterrupt:
            print("interrupted", file=sys.stderr)
        if t is not None:
            print(t.summary())
        return 0

    print(f"sending {a.count} requests to {where} ...", flush=True)
    t = Traffic(client, v2=v2, seed=a.seed)
    try:
        t.run(a.count, a.delay)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — e.g. the server went away mid-run
        print(f"error after {t.sent} requests: {type(e).__name__}: {e}", file=sys.stderr)
        print(t.summary())
        return 1
    print(t.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
