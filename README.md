# API Contract & Mock Generator

Reads raw HTTP logs (JSON lines), infers endpoints and schemas, writes a validated OpenAPI 3.0 spec,
and serves it with a Prism mock server. Continuous mode updates the spec as logs arrive and flags breaking changes.

## Setup

Requires Python 3.11+ and Node 18+ (for Prism).

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
npm install -g @stoplight/prism-cli
```

## Generate sample logs

```bash
python generate_logs.py --changed   # -> sample_logs.jsonl (200 entries) + changed_logs.jsonl (breaking changes)
```

Fixed seed: everyone gets the identical file (200 entries: 133×200, 34×201, 12×400, 17×404, 4×500).

## Run

```bash
python main.py build sample_logs.jsonl -o output/openapi.yaml   # one-shot spec
prism mock output/openapi.yaml --port 4010                      # mock it

python main.py watch live_logs.jsonl                            # continuous mode (starts Prism on :4010)
python main.py dashboard --port 8000                            # http://localhost:8000
pytest -q
```

## Demo

One command runs the whole thing: clean start (deletes `live_logs.jsonl` and `output/`), watcher + Prism,
dashboard on http://localhost:8000, then waits for Enter before replaying `sample_logs.jsonl` and again before
replaying `changed_logs.jsonl`. Ctrl+C stops everything, including Prism.

```bash
python main.py demo            # presenter mode: Enter between steps
python main.py demo --auto     # no pauses (3s gaps) — for recording the backup video
```

Or step by step in three terminals:

```bash
# terminal 1  (--fresh deletes live_logs.jsonl, output/openapi.yaml and output/changes.jsonl first)
python main.py watch live_logs.jsonl --fresh
# terminal 2
python main.py dashboard
# terminal 3
python replay_logs.py sample_logs.jsonl live_logs.jsonl --delay 0.2    # endpoints appear live
curl http://localhost:4010/users/1                                      # mock returns valid fake data
python replay_logs.py changed_logs.jsonl live_logs.jsonl --delay 0.5   # breaking-change alerts
```

Delete `live_logs.jsonl` and `output/` to reset.

## Realistic log source: the passport service

`demo_app/` is a small FastAPI "passport application" service whose middleware logs every API call as one
JSON line in the format `parser.py` reads (Authorization, Cookie and X-API-Key values are masked).
Endpoints: `POST /applications`, `GET /applications?status=&office_id=&limit=`, `GET /applications/{id}`,
`PATCH /applications/{id}/status`, `GET /offices` — with 400 validation errors, 404s, 409 for forbidden
status changes and a ~2% random 500. A form at http://127.0.0.1:8001 generates traffic by hand.

```bash
python main.py watch live_logs.jsonl      # terminal 1 (+ python main.py dashboard in terminal 2)
python -m demo_app                        # terminal 3: v1 service on :8001, logging to live_logs.jsonl
python traffic.py --delay 0.02            # terminal 4: exactly 500 mixed requests (seeded, reproducible)

# the sprint change: new required field emergency_contact, date_of_birth renamed to birth_date
#   stop the service (Ctrl+C), then
python -m demo_app --v2                   # or: PASSPORT_API_VERSION=2 python -m demo_app
python traffic.py --delay 0.05            # detects v2; ~10% of submissions still use the old form -> 400
```

Expected on the dashboard after the v2 traffic: 6 BREAKING changes — `emergency_contact` and `birth_date`
as new required request fields, `date_of_birth` no longer sent, and `date_of_birth` removed from the
responses of POST /applications, GET /applications/{id} and PATCH /applications/{id}/status.
The form's "Form version" switch can submit the old v1 form against v2 to show the 400 live.

No server needed for a log file: `python traffic.py --in-process --log demo_logs.jsonl [--v2]`.

## Layout and ownership

| File | Owner | In -> Out |
|---|---|---|
| `models.py` | all | shared types: `LogEntry`, `EndpointSchema`, `ParamInfo`, `SpecChange` |
| `parser.py` | Name 1 | `.jsonl` -> `list[LogEntry]` |
| `normalizer.py` | Name 1 | `list[LogEntry]` -> `dict[(method, template), list[LogEntry]]` |
| `inferrer.py` | Name 2 | grouped entries -> `list[EndpointSchema]` |
| `spec_builder.py` | Name 2 | `list[EndpointSchema]` -> validated `openapi.yaml` |
| `watcher.py` | Name 3 | log changes -> `output/openapi.yaml` + `output/changes.jsonl`, restarts Prism |
| `dashboard/` | Name 4 | reads `output/*` -> web UI |
| `main.py` | Name 4 | CLI wiring |
| `generate_logs.py`, `replay_logs.py` | done | test data + demo streaming |
| `demo_app/`, `traffic.py` | | passport service (realistic log source) + traffic generator |

Watcher and dashboard talk only through files in `output/`, so each can be built and tested alone.

## Known gotchas

- Some fields are sometimes `null` (`orders.notes`) or missing (`users.phone`, `products.rating`).
  genson emits `["string","null"]`; `spec_builder.to_openapi_schema` must turn this into `nullable: true`, or 3.0 validation fails.
- Write the spec atomically, or Prism may crash reading a half-written file.
- On Windows the Prism binary is `prism.cmd`, so use `shutil.which("prism")`.
