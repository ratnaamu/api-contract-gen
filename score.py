"""score.py — A7/B6: compare the spec built from a noisy log against the spec built from its clean
original, and print a scorecard instead of just asserting "graceful degradation" works.

Contract:
    clean .jsonl, noisy .jsonl  ->  a scorecard dict (and, run as a script, printed to stdout)
Also reports observed-vs-inferred error-status coverage (B6) when a `holdout` log — the same traffic
with 4xx/5xx down-sampled in `noisy`/`clean` but present in full in `holdout` — is given.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from errors import collect_api_evidence, collect_evidence, infer_error_responses, learn_error_envelope, mine_validation_evidence
from inferrer import infer_all
from normalizer import group_by_endpoint
from parser import read_logs
from spec_builder import build_spec

EndpointKey = tuple[str, str]


def build_from_log(path: str | Path, infer_errors: bool = False) -> dict[str, Any]:
    entries = read_logs(Path(path))
    grouped = group_by_endpoint(entries)
    endpoints = infer_all(grouped)
    inferred = None
    if infer_errors:
        envelope, observed = learn_error_envelope(entries)
        api = collect_api_evidence(entries)
        evidence = collect_evidence(endpoints, grouped)
        inferred = {
            (ep.method, ep.path_template): infer_error_responses(
                ep, evidence[(ep.method, ep.path_template)], api, envelope, observed,
                mine_validation_evidence(ep, grouped[(ep.method, ep.path_template)]))
            for ep in endpoints
        }
    return build_spec(endpoints, inferred=inferred)


def _flatten_fields(schema: Any, prefix: str, out: dict[str, dict[str, Any]]) -> None:
    """{"dotted.path": {..schema.., "_required": bool}} for every property under `schema`."""
    if not isinstance(schema, dict):
        return
    props = schema.get("properties")
    if isinstance(props, dict):
        required = set(schema.get("required") or [])
        for name, sub in props.items():
            path = f"{prefix}.{name}" if prefix else name
            node = dict(sub) if isinstance(sub, dict) else {}
            node["_required"] = name in required
            out[path] = node
            _flatten_fields(sub, path, out)
    items = schema.get("items")
    if isinstance(items, dict):
        _flatten_fields(items, f"{prefix}[]" if prefix else "[]", out)
    for branch in schema.get("anyOf") or []:
        _flatten_fields(branch, prefix, out)


def _endpoint_fields(spec: dict[str, Any]) -> dict[EndpointKey, dict[str, dict[str, Any]]]:
    """{(METHOD, path): {field_path: node}} across the request body and every OBSERVED response body
    (x-inferred responses are excluded — they're a guess, not something to score field accuracy on)."""
    out: dict[EndpointKey, dict[str, dict[str, Any]]] = {}
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            fields: dict[str, dict[str, Any]] = {}
            req = ((op.get("requestBody") or {}).get("content") or {}).get("application/json", {}).get("schema")
            if req:
                _flatten_fields(req, "request", fields)
            for status, resp in (op.get("responses") or {}).items():
                if isinstance(resp, dict) and not resp.get("x-inferred"):
                    s = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
                    if s:
                        _flatten_fields(s, f"response.{status}", fields)
            out[(method.upper(), path)] = fields
    return out


def _endpoint_statuses(spec: dict[str, Any], observed_only: bool) -> dict[EndpointKey, set[str]]:
    out: dict[EndpointKey, set[str]] = {}
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            statuses = {s for s, r in (op.get("responses") or {}).items()
                       if not observed_only or not (isinstance(r, dict) and r.get("x-inferred"))}
            out[(method.upper(), path)] = statuses
    return out


def score(clean_spec: dict[str, Any], noisy_spec: dict[str, Any]) -> dict[str, Any]:
    """Field-level precision/recall/required/nullable/false-enum accuracy of `noisy_spec` against
    `clean_spec` as ground truth. Endpoints only in one spec are skipped (a real gap, but a different
    one from field-shape accuracy — endpoint coverage would need its own metric)."""
    clean_by_ep = _endpoint_fields(clean_spec)
    noisy_by_ep = _endpoint_fields(noisy_spec)
    shared = set(clean_by_ep) & set(noisy_by_ep)

    tp = fp = fn = 0
    required_correct = required_total = 0
    nullable_correct = nullable_total = 0
    false_enum = 0
    for key in shared:
        clean, noisy = clean_by_ep[key], noisy_by_ep[key]
        clean_names, noisy_names = set(clean), set(noisy)
        tp += len(clean_names & noisy_names)
        fn += len(clean_names - noisy_names)
        fp += len(noisy_names - clean_names)
        for name in clean_names & noisy_names:
            c, n = clean[name], noisy[name]
            required_total += 1
            required_correct += c["_required"] == n["_required"]
            nullable_total += 1
            nullable_correct += bool(c.get("nullable")) == bool(n.get("nullable"))
            if "enum" in n and "enum" not in c:
                false_enum += 1

    def ratio(a: int, b: int) -> float:
        return round(a / b, 4) if b else 1.0

    return {
        "endpoints_clean": len(clean_by_ep),
        "endpoints_noisy": len(noisy_by_ep),
        "endpoints_shared": len(shared),
        "field_precision": ratio(tp, tp + fp),
        "field_recall": ratio(tp, tp + fn),
        "required_accuracy": ratio(required_correct, required_total),
        "nullable_accuracy": ratio(nullable_correct, nullable_total),
        "false_enum_count": false_enum,
    }


def score_error_coverage(training_spec: dict[str, Any], holdout_spec: dict[str, Any]) -> dict[str, Any]:
    """B6: what fraction of the ground-truth (holdout) status codes per endpoint did the training-only
    build capture — with vs without --infer-errors (the caller builds `training_spec` both ways and
    calls this twice)."""
    truth = _endpoint_statuses(holdout_spec, observed_only=True)
    got = _endpoint_statuses(training_spec, observed_only=False)
    total = sum(len(v) for v in truth.values())
    covered = sum(len(truth[k] & got.get(k, set())) for k in truth)
    return {"ground_truth_statuses": total, "covered_statuses": covered,
            "coverage": round(covered / total, 4) if total else 1.0}


def run(clean_log: str | Path, noisy_log: str | Path, holdout_log: str | Path | None = None) -> dict[str, Any]:
    """The full scorecard: field accuracy always; error-status coverage (observed vs with --infer-errors)
    only when `holdout_log` is given. Never raises — a build crash is reported as crash_count instead."""
    result: dict[str, Any] = {"crash_count": 0}
    try:
        clean_spec = build_from_log(clean_log)
    except Exception as e:  # noqa: BLE001 — a crash is a result, not a reason to stop scoring
        result["crash_count"] += 1
        result["clean_build_error"] = f"{type(e).__name__}: {e}"
        return result
    try:
        noisy_spec = build_from_log(noisy_log)
    except Exception as e:  # noqa: BLE001
        result["crash_count"] += 1
        result["noisy_build_error"] = f"{type(e).__name__}: {e}"
        return result
    result.update(score(clean_spec, noisy_spec))

    if holdout_log is not None:
        try:
            holdout_spec = build_from_log(holdout_log)
            observed_only_spec = build_from_log(noisy_log, infer_errors=False)
            with_inference_spec = build_from_log(noisy_log, infer_errors=True)
            result["error_coverage_observed_only"] = score_error_coverage(observed_only_spec, holdout_spec)
            result["error_coverage_with_inference"] = score_error_coverage(with_inference_spec, holdout_spec)
        except Exception as e:  # noqa: BLE001
            result["crash_count"] += 1
            result["holdout_build_error"] = f"{type(e).__name__}: {e}"
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Score a noisy-log build against its clean original")
    ap.add_argument("clean_log")
    ap.add_argument("noisy_log")
    ap.add_argument("--holdout", help="full-traffic log (see generate_logs.py --holdout-errors) for B6's "
                                     "observed-vs-inferred error-status coverage")
    ap.add_argument("--json", action="store_true", help="print the scorecard as JSON instead of a table")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show parser.py's per-line skip warnings (noisy on purpose: quiet by default "
                         "so the scorecard is what's on screen, not a wall of expected parse failures)")
    a = ap.parse_args()
    if not a.verbose:
        logging.getLogger("parser").setLevel(logging.ERROR)
    result = run(a.clean_log, a.noisy_log, a.holdout)
    if a.json:
        print(json.dumps(result, indent=2))
        return
    for k, v in result.items():
        if isinstance(v, dict):
            print(f"{k}:")
            for k2, v2 in v.items():
                print(f"  {k2:24} {v2}")
        else:
            print(f"{k:26} {v}")


if __name__ == "__main__":
    main()
