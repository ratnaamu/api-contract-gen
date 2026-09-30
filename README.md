# API Contract & Mock Generator

Reads raw HTTP logs (JSON lines), infers endpoints and schemas, writes a validated OpenAPI 3.0 spec,
and serves it with a Prism mock server. Continuous mode updates the spec as logs arrive and flags breaking changes.

## Setup

Requires Python 3.10+ and Node 18+ (for Prism).

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
python main.py build sample_logs.jsonl -o output/openapi.yaml   # one-shot spec -> output/openapi.json (+ .yaml)
prism mock output/openapi.json --port 4010 -d                   # mock it with fresh generated data per call

python main.py watch live_logs.jsonl                            # continuous mode (starts Prism -d on :4010)
python main.py watch live_logs.jsonl --static                   # ... Prism serves the recorded examples instead
python main.py dashboard --port 8000                            # http://localhost:8000
pytest -q                                                        # full suite
pytest -q -m "not slow"                                          # skip test_demo.py (spawns real servers)
```

Outputs: `output/openapi.json` is the main output; `output/openapi.yaml` is the same spec (plus
`output/openapi.mock.json`, a Prism-only variant — see "Evidence-graded inference" below) and
`output/quality.json`, a parse-rate/ambiguity report the dashboard's "Input quality" card reads. Both
main files are written atomically by build, watch and demo. The pipeline is rule-based (no AI):

- **Tolerant parsing.** `parser.FIELD_ALIASES` accepts common log shapes from other tools, matched
  case-insensitively: `verb` / `http_method`, `url` / `uri` / `request.url`, `statusCode` / `status_code`
  / `response.status`, `requestBody` / `request.body`, `responseBody` / `response.body`,
  `request.headers`, bodies logged as JSON strings (truncated ones get a best-effort repair, or are
  excluded and flagged rather than mistyped), status logged as text (`"200 OK"`), full URLs (scheme/host
  dropped), and HAR files (`parser.read_har` — a browser's "Export HAR" just works). See
  `tests/data/alt_format_logs.jsonl` (the sample logs in four other shapes; same contract).
- **Evidence-graded inference.** `required`/`optional` come from a presence *ratio* + sample size, not
  genson's "in literally every sample" — a field missing from one of 200 samples is still `required`
  (`x-presence` reports a Wilson lower bound); `enum` only fires on genuinely categorical fields (name
  guard rails, cardinality ratio, not just a value cap); numeric ranges live in `x-observed-range`
  rather than a contract `minimum`/`maximum` a real 30th record could violate. Mixed API versions
  sharing one path (an unrecognised `v1`/`v2` split, or a field silently renamed) are flagged
  `x-ambiguity` with a proposed rename instead of silently merging into one superset schema. See
  `output/quality.json` (and the dashboard's "Input quality" card) for a parse-rate/ambiguity summary.
- **Error-path inference** (`--infer-errors`, off by default): guesses plausible statuses
  (404/400/401/403/409/429/500/405) an endpoint's traffic never happened to show, from evidence like
  path params, request bodies and auth headers (`errors.py`) — each tagged `x-inferred` so it's never
  mistaken for something observed, and excluded entirely from breaking-change detection.
- **Mock proxy** (`--mock-proxy` on `watch`/`demo`, off by default): a thin layer in front of Prism
  (`mock_proxy.py`) adding stateful 404s (unknown ids get a real 404, not a fake 200), real request
  validation, auth enforcement, and chaos-injected errors at realistic rates — `X-Mock-Scenario: <code>`
  header forces one deterministically.
- **AIDH-written descriptions** (`--llm-refine`, off by default): field/operation descriptions from
  Unisys's AIDH LLM gateway for what the rule-based inferrer can't produce — meaning, not shape. Needs
  `AIDH_BASE_URL`/`AIDH_DOMAIN_ID`/`AIDH_MODEL` (`.env` or shell); `python main.py llm-check` verifies
  the connection. Any LLM failure just falls back to the rule-based spec; descriptions are marked "AI"
  in the dashboard.
- **Score yourselves** (`noise.py` + `score.py`, A7/B6): `noise.py` injects configurable-rate data-quality
  problems (dropped/nulled fields, casing swaps, version mixes, truncated/corrupted JSON) into a clean
  log; `score.py` builds both and reports field precision/recall, required/nullable accuracy and false-
  enum count against the clean build as ground truth. `generate_logs.py --error-rate/--holdout-errors`
  also lets it report observed-vs-`--infer-errors` status coverage:
  ```bash
  python generate_logs.py -n 300 -o clean.jsonl --error-rate 0.2 --holdout-errors holdout.jsonl
  python noise.py clean.jsonl -o noisy.jsonl --rate 0.15
  python score.py clean.jsonl noisy.jsonl --holdout holdout.jsonl
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

Expected on the dashboard after the v2 traffic: 5 BREAKING changes — `emergency_contact` as a new
required request field, and "date_of_birth appears to be renamed to birth_date" (`field_renamed`) in the
POST /applications request and in the responses of POST /applications, GET /applications/{id} and
PATCH /applications/{id}/status. `emergency_contact` in responses shows as an ok (additive) new field.
The form's "Form version" switch can submit the old v1 form against v2 to show the 400 live.

No server needed for a log file: `python traffic.py --in-process --log demo_logs.jsonl [--v2]`.

## Layout and ownership

<!-- TODO: replace "Name 1".."Name 4" below with the actual team members' names. -->

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
