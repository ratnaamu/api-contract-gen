"""Passport application service — a realistic source of logs for the contract generator.

    python -m demo_app                    # v1 on http://127.0.0.1:8001, logs to live_logs.jsonl
    python -m demo_app --v2               # v2: new required field + a renamed field
    PASSPORT_API_VERSION=2 python -m demo_app

Endpoints (every one of them is logged as one JSON line; the form page at / is not):
    POST  /applications                 201 created | 400 validation error
    GET   /applications                 200 list (?status=&office_id=&limit=) | 400 bad filter
    GET   /applications/{id}            200 | 404
    PATCH /applications/{id}/status     200 | 400 bad value | 404 | 409 transition not allowed
    GET   /offices                      200
Any API call can also fail with a random 500 (error_rate, default 2%).

v2 (the "sprint" change): the request field `date_of_birth` is renamed `birth_date`, and a new
required object `emergency_contact` {name, phone, relationship?} is added. Responses follow suit.

Beyond v2 the contract can keep drifting (see drift.py): `create_app(stage=N)` starts at any sprint, and
`app.state.set_stage(N)` moves a *running* service to another one — every handler reads the stage per
request, so the demo rolls out sprints without restarting the server. GET /meta publishes the current
contract (renames, required fields) so clients that care (traffic.py) can follow.
"""
# No `from __future__ import annotations`: FastAPI must see the real (per-version) body model class
# in create_application's signature, and that class is a local variable of create_app().
import os
import random
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import drift
from .logging_middleware import JsonLineWriter, JsonLogMiddleware

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_LOG_PATH = "live_logs.jsonl"
DEFAULT_ERROR_RATE = 0.02
SEED_APPLICATIONS = 25
API_PREFIXES = ("/applications", "/offices")

STATUSES = ("submitted", "in_review", "approved", "rejected", "issued")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "submitted": ("in_review", "rejected"),
    "in_review": ("approved", "rejected"),
    "approved": ("issued",),
    "rejected": (),
    "issued": (),
}

OFFICES: list[dict[str, Any]] = [
    {"id": "OFF-LON", "name": "London Passport Office", "city": "London", "country": "GB",
     "services": ["standard", "express"], "open_hours": "08:00-18:00"},
    {"id": "OFF-MAN", "name": "Manchester Passport Office", "city": "Manchester", "country": "GB",
     "services": ["standard"], "open_hours": "09:00-17:00"},
    {"id": "OFF-EDI", "name": "Edinburgh Passport Office", "city": "Edinburgh", "country": "GB",
     "services": ["standard", "express"], "open_hours": "09:00-17:00"},
    {"id": "OFF-BLR", "name": "Bengaluru Consular Office", "city": "Bengaluru", "country": "IN",
     "services": ["standard"], "open_hours": "09:30-16:30"},
    {"id": "OFF-NYC", "name": "New York Consular Office", "city": "New York", "country": "US",
     "services": ["standard", "express"], "open_hours": "08:30-16:00"},
]
OFFICE_IDS = {o["id"] for o in OFFICES}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 \-]{6,19}$")


def is_v2_from_env() -> bool:
    return os.environ.get("PASSPORT_API_VERSION", "1").strip() in ("2", "v2")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class Address(BaseModel):
    street: str = Field(min_length=1, max_length=120)
    city: str = Field(min_length=1, max_length=60)
    postal_code: str = Field(min_length=2, max_length=12)
    country: str = Field(pattern=r"^[A-Z]{2}$")


class EmergencyContact(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str
    relationship: Optional[str] = Field(default=None, max_length=40)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        if not _PHONE_RE.match(v):
            raise ValueError("invalid phone number")
        return v


class _ApplicationBase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str = Field(min_length=2, max_length=100)
    nationality: str = Field(pattern=r"^[A-Z]{2}$", description="ISO 3166 alpha-2")
    email: str
    phone: Optional[str] = None
    passport_type: Literal["standard", "express"] = "standard"
    pages: Literal[32, 48] = 32
    office_id: str
    address: Optional[Address] = None
    previous_passport_number: Optional[str] = Field(default=None, pattern=r"^[A-Z0-9]{6,9}$")

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _PHONE_RE.match(v):
            raise ValueError("invalid phone number")
        return v


def _check_birth_date(v: date) -> date:
    today = date.today()
    if v >= today:
        raise ValueError("must be in the past")
    if (today.year - v.year) > 120:
        raise ValueError("must be within the last 120 years")
    return v


class ApplicationInV1(_ApplicationBase):
    date_of_birth: date

    @field_validator("date_of_birth")
    @classmethod
    def _dob(cls, v: date) -> date:
        return _check_birth_date(v)


class ApplicationInV2(_ApplicationBase):
    birth_date: date                       # renamed from date_of_birth
    emergency_contact: EmergencyContact    # new, required

    @field_validator("birth_date")
    @classmethod
    def _dob(cls, v: date) -> date:
        return _check_birth_date(v)


class ApplicationInCanonical(_ApplicationBase):
    """Every sprint's request body, after `translate_request` has mapped the public names back to the
    canonical ones and checked the sprint's required extras. Validation rules are the same at every stage."""
    date_of_birth: date
    emergency_contact: Optional[EmergencyContact] = None
    consent: Optional[bool] = None

    @field_validator("date_of_birth")
    @classmethod
    def _dob(cls, v: date) -> date:
        return _check_birth_date(v)


def translate_request(body: dict[str, Any], stage: int) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Map a request body in `stage`'s public shape onto canonical field names. Returns (canonical body,
    problems): a renamed field sent under its OLD name counts as missing under the new one (the old name
    is simply ignored, exactly like a real backend that stopped knowing it), and each of the stage's
    required extras must be present."""
    rules = drift.request_contract(stage)
    out = dict(body)
    problems: list[dict[str, str]] = []
    for canonical, public in rules["renames"].items():
        out.pop(canonical, None)                 # the old spelling is no longer understood
        if public in out:
            out[canonical] = out.pop(public)
        else:
            problems.append({"field": public, "message": "Field required"})
    for name in rules["required"]:
        if out.get(name) is None:
            problems.append({"field": name, "message": "Field required"})
    return out, problems


def _pydantic_details(exc: ValidationError, stage: int) -> list[dict[str, str]]:
    renames = drift.request_contract(stage)["renames"]
    details = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ())]
        if loc:
            loc[0] = renames.get(loc[0], loc[0])  # report the field under the name the client used
        msg = str(err.get("msg", "invalid value"))
        details.append({"field": ".".join(loc) or "body", "message": msg.removeprefix("Value error, ")})
    return details


class StatusUpdate(BaseModel):
    status: Literal["submitted", "in_review", "approved", "rejected", "issued"]
    note: Optional[str] = Field(default=None, max_length=200)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Store:
    """In-memory applications, seeded deterministically so GET /applications/{id} works straight away."""

    def __init__(self, seed_count: int = SEED_APPLICATIONS, seed: int = 1) -> None:
        self._lock = threading.Lock()
        self._next = 1
        self.items: dict[str, dict[str, Any]] = {}
        rng = random.Random(seed)
        first = ["Aarav", "Sofia", "Liam", "Mei", "Olivia", "Kwame", "Ines", "Noah", "Priya", "Lucas"]
        last = ["Sharma", "Garcia", "Smith", "Tanaka", "Brown", "Mensah", "Silva", "Müller", "Iyer", "Rossi"]
        for _ in range(seed_count):
            name = f"{rng.choice(first)} {rng.choice(last)}"
            record = self.create({
                "full_name": name,
                "birth_date": date(rng.randint(1950, 2006), rng.randint(1, 12), rng.randint(1, 28)),
                "nationality": rng.choice(["GB", "IN", "US", "PT", "DE"]),
                "email": name.lower().replace(" ", ".").replace("ü", "u") + "@example.com",
                "phone": f"+44 7700 {rng.randint(100000, 999999)}" if rng.random() < 0.6 else None,
                "passport_type": rng.choice(["standard", "standard", "express"]),
                "pages": rng.choice([32, 32, 48]),
                "office_id": rng.choice(sorted(OFFICE_IDS)),
                "address": None,
                "previous_passport_number": None,
                "emergency_contact": {"name": "Alex " + rng.choice(last), "phone": f"+44 7700 {rng.randint(100000, 999999)}",
                                      "relationship": rng.choice(["parent", "partner", "sibling"])},
            })
            for target in rng.choice([[], ["in_review"], ["in_review", "approved"], ["rejected"]]):
                note = rng.choice([None, "documents verified", "photo does not meet requirements"])
                self.set_status(record["id"], target, note)

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            app_id = f"PA-{self._next:06d}"
            self._next += 1
            now = _now()
            record = {**data, "id": app_id, "status": "submitted", "submitted_at": now, "updated_at": now,
                      "status_history": [{"status": "submitted", "changed_at": now}]}
            self.items[app_id] = record
            return record

    def get(self, app_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self.items.get(app_id)

    def set_status(self, app_id: str, status: str, note: str | None) -> dict[str, Any]:
        with self._lock:
            record = self.items[app_id]
            now = _now()
            entry: dict[str, Any] = {"status": status, "changed_at": now}
            if note:
                entry["note"] = note
            record["status"] = status
            record["updated_at"] = now
            record["status_history"].append(entry)
            return record

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.items.values())


# ---------------------------------------------------------------------------
# Representation
# ---------------------------------------------------------------------------

def _office_ref(office_id: str) -> dict[str, str]:
    o = next(o for o in OFFICES if o["id"] == office_id)
    return {"id": o["id"], "name": o["name"], "city": o["city"]}


def to_public(record: dict[str, Any], stage: int | bool) -> dict[str, Any]:
    """The API view of a stored application at `stage` (True/False still mean v2/v1). Optional fields are
    omitted when empty. Response-side sprint changes live here — see drift.SPRINTS for the list:
      stage >= 1  birth_date (not date_of_birth), emergency_contact
      stage >= 2  pages as a string
      stage >= 3  updated_at dropped
      stage >= 4  citizenship (not nationality)
      stage >= 5  tracking_number added
      stage >= 7  created_at (not submitted_at)"""
    stage = drift.clamp(1 if stage is True else 0 if stage is False else stage)
    out: dict[str, Any] = {"id": record["id"], "status": record["status"], "full_name": record["full_name"]}
    out["birth_date" if stage >= 1 else "date_of_birth"] = record["birth_date"].isoformat()
    out["citizenship" if stage >= 4 else "nationality"] = record["nationality"]
    out["email"] = record["email"]
    if record.get("phone"):
        out["phone"] = record["phone"]
    out.update({"passport_type": record["passport_type"],
                "pages": str(record["pages"]) if stage >= 2 else record["pages"],
                "office": _office_ref(record["office_id"])})
    if record.get("address"):
        out["address"] = record["address"]
    if record.get("previous_passport_number"):
        out["previous_passport_number"] = record["previous_passport_number"]
    if stage >= 1 and record.get("emergency_contact"):
        out["emergency_contact"] = {k: v for k, v in record["emergency_contact"].items() if v is not None}
    if stage >= 5:
        out["tracking_number"] = "TRK-" + record["id"].removeprefix("PA-") + "-" + record["office_id"][-3:]
    if stage >= 6 and record.get("consent") is not None:
        out["consent"] = record["consent"]
    out["created_at" if stage >= 7 else "submitted_at"] = record["submitted_at"]
    if stage < 3:
        out["updated_at"] = record["updated_at"]
    out["status_history"] = [dict(h) for h in record["status_history"]]
    return out


def to_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {"id": record["id"], "status": record["status"], "full_name": record["full_name"],
            "office_id": record["office_id"], "submitted_at": record["submitted_at"]}


def _error(status: int, error: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "message": message, **extra})


def _not_found(app_id: str) -> JSONResponse:
    return _error(404, "not_found", f"application {app_id} not found")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    v2: bool | None = None,
    log_path: str | Path | None = None,
    error_rate: float | None = None,
    seed: int | None = None,
    stage: int | None = None,
) -> FastAPI:
    """Build the service. Defaults come from env vars: PASSPORT_API_VERSION (1|2),
    PASSPORT_APP_LOG (live_logs.jsonl), PASSPORT_ERROR_RATE (0.02), PASSPORT_SEED (random if unset).
    `stage` (0..drift.MAX_STAGE) picks the contract sprint; it overrides `v2` (stage 1 == v2) and can be
    changed later on the running app with `app.state.set_stage(n)`."""
    if stage is None:
        if v2 is None:
            v2 = is_v2_from_env()
        stage = 1 if v2 else 0
    stage = drift.clamp(stage)
    if log_path is None:
        log_path = os.environ.get("PASSPORT_APP_LOG", DEFAULT_LOG_PATH)
    if error_rate is None:
        error_rate = float(os.environ.get("PASSPORT_ERROR_RATE", DEFAULT_ERROR_RATE))
    if seed is None and os.environ.get("PASSPORT_SEED"):
        seed = int(os.environ["PASSPORT_SEED"])

    store = Store()

    app = FastAPI(title="Passport Application Service", version=f"{1 if stage == 0 else 2}.0.0",
                  docs_url=None, redoc_url=None, openapi_url=None)  # the contract comes from the logs
    app.state.store = store
    app.state.stage = stage
    app.state.log_path = Path(log_path)

    def current_stage() -> int:
        return int(app.state.stage)

    def set_stage(n: int) -> int:
        """Move the running service to sprint `n` (clamped). Takes effect on the next request."""
        app.state.stage = drift.clamp(n)
        return app.state.stage

    app.state.set_stage = set_stage
    app.add_middleware(
        JsonLogMiddleware,
        writer=JsonLineWriter(log_path),
        should_log=lambda path: path.startswith(API_PREFIXES),
        error_rate=error_rate,
        rng=random.Random(seed),
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = []
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
            msg = str(err.get("msg", "invalid value"))
            if err.get("type") == "json_invalid":
                loc, msg = ["body"], "request body is not valid JSON"
            details.append({"field": ".".join(loc) or "body", "message": msg.removeprefix("Value error, ")})
        return _error(400, "validation_error", "request validation failed", details=details)

    # ---- UI (not logged) ----
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/meta", include_in_schema=False)
    def meta() -> dict[str, Any]:
        """For the form page and traffic.py: which contract is running. Not under an API prefix, so not
        logged. `version` is 1 or 2 (what v1/v2-era clients look at); `stage`/`request`/`previous`
        describe the current sprint precisely (see drift.contract)."""
        return {**drift.contract(current_stage()), "statuses": list(STATUSES), "transitions": TRANSITIONS}

    # ---- API ----
    @app.get("/offices")
    def list_offices() -> list[dict[str, Any]]:
        return OFFICES

    @app.post("/applications", status_code=201)
    def create_application(body: dict[str, Any]) -> Any:
        stage = current_stage()
        canonical, problems = translate_request(body, stage)
        if problems:
            return _error(400, "validation_error", "request validation failed", details=problems)
        try:
            parsed = ApplicationInCanonical.model_validate(canonical)
        except ValidationError as exc:
            return _error(400, "validation_error", "request validation failed", details=_pydantic_details(exc, stage))
        data = parsed.model_dump()
        if data["office_id"] not in OFFICE_IDS:
            return _error(400, "validation_error", "request validation failed",
                          details=[{"field": "office_id", "message": f"unknown office '{data['office_id']}'"}])
        office = next(o for o in OFFICES if o["id"] == data["office_id"])
        if data["passport_type"] not in office["services"]:
            return _error(400, "validation_error", "request validation failed",
                          details=[{"field": "passport_type",
                                    "message": f"{office['name']} does not offer {data['passport_type']} service"}])
        data["birth_date"] = data.pop("date_of_birth")
        record = store.create(data)
        return JSONResponse(status_code=201, content=to_public(record, stage))

    @app.get("/applications")
    def list_applications(
        status: Optional[Literal["submitted", "in_review", "approved", "rejected", "issued"]] = None,
        office_id: Optional[str] = None,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        items = [r for r in store.all()
                 if (status is None or r["status"] == status) and (office_id is None or r["office_id"] == office_id)]
        items.sort(key=lambda r: r["id"], reverse=True)  # newest first
        return {"items": [to_summary(r) for r in items[:limit]], "total": len(items), "limit": limit}

    @app.get("/applications/{app_id}")
    def get_application(app_id: str) -> Any:
        record = store.get(app_id)
        return _not_found(app_id) if record is None else to_public(record, current_stage())

    @app.patch("/applications/{app_id}/status")
    def update_status(app_id: str, body: StatusUpdate) -> Any:
        record = store.get(app_id)
        if record is None:
            return _not_found(app_id)
        current = record["status"]
        if body.status not in TRANSITIONS[current]:
            return _error(409, "invalid_transition", f"cannot change status from {current} to {body.status}",
                          current_status=current, allowed=list(TRANSITIONS[current]))
        return to_public(store.set_status(app_id, body.status, body.note), current_stage())

    return app
