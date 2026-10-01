"""Contract drift: the passport API's history as a sequence of "sprints", each changing the contract a
little, the way a real backend team's releases do. Stage 0 is v1; stage 1 is the familiar v2 (the demo's
original breaking change); stages 2.. are further sprints the `--drift` demo rolls out over time so the
dashboard keeps finding breaking changes for as long as it runs.

A stage is a *cumulative* position in SPRINTS: the contract at stage N includes every sprint before it.
`contract(stage)` is what GET /meta returns and what traffic.py reads to shape its payloads, so the
generator follows the service through every sprint on its own (apart from the deliberate share of
"old clients" that keep sending the previous sprint's shape and get 400s — the realistic fallout).

Request-side changes are described as data (renames + required extras) because both the service and the
traffic generator need them. Response-side changes are applied in app.to_public, keyed by stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Sprint:
    name: str
    summary: str                                   # one line for the demo console / dashboard
    breaking: bool                                 # what the contract generator should flag
    request_renames: dict[str, str] = field(default_factory=dict)   # canonical name -> public name
    request_required: tuple[str, ...] = ()         # extra required request fields (public names)


SPRINTS: tuple[Sprint, ...] = (
    Sprint("v2", "request+response: date_of_birth renamed to birth_date; new REQUIRED request field "
                 "emergency_contact {name, phone, relationship?}", True,
           request_renames={"date_of_birth": "birth_date"}, request_required=("emergency_contact",)),
    Sprint("pages_as_string", "response: `pages` is now a string (\"32\") instead of an integer", True),
    Sprint("updated_at_dropped", "response: `updated_at` is no longer returned (it was in every response)", True),
    Sprint("citizenship", "request+response: `nationality` renamed to `citizenship`", True,
           request_renames={"nationality": "citizenship"}),
    Sprint("tracking_number", "response: new field `tracking_number` (additive — not breaking)", False),
    Sprint("consent", "request: new REQUIRED boolean field `consent`", True, request_required=("consent",)),
    Sprint("created_at", "response: `submitted_at` renamed to `created_at`", True),
)
MAX_STAGE = len(SPRINTS)


def clamp(stage: int) -> int:
    return max(0, min(int(stage), MAX_STAGE))


def stage_name(stage: int) -> str:
    stage = clamp(stage)
    return "v1" if stage == 0 else SPRINTS[stage - 1].name


def sprint(stage: int) -> Sprint | None:
    """The sprint that *introduced* `stage` (None for stage 0)."""
    stage = clamp(stage)
    return None if stage == 0 else SPRINTS[stage - 1]


def request_contract(stage: int) -> dict[str, Any]:
    """Cumulative request-shape rules at `stage`: {"renames": {canonical: public}, "required": [public...]}."""
    renames: dict[str, str] = {}
    required: list[str] = []
    for s in SPRINTS[:clamp(stage)]:
        renames.update(s.request_renames)
        required.extend(s.request_required)
    return {"renames": renames, "required": required}


def contract(stage: int) -> dict[str, Any]:
    """What GET /meta publishes about the running contract (version kept for the v1/v2-era clients)."""
    stage = clamp(stage)
    return {
        "stage": stage,
        "name": stage_name(stage),
        "version": 1 if stage == 0 else 2,
        "request": request_contract(stage),
        "previous": None if stage == 0 else {"stage": stage - 1, "name": stage_name(stage - 1),
                                              "request": request_contract(stage - 1)},
    }


def public_name(canonical: str, stage: int) -> str:
    return request_contract(stage)["renames"].get(canonical, canonical)
