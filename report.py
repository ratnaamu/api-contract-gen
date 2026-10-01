"""report.py — a presentable Word report every time the contract breaks.

Whenever the watcher detects BREAKING changes, this module writes `output/reports/contract-report-vX.Y.Z-
<timestamp>.docx` (and refreshes `output/contract-report-latest.docx`): a document a backend team never
wrote and a frontend team actually needs — what changed, why it breaks consumers, what to do about it,
and the full current contract as of this release.

Releases are versioned with semver derived from the change kinds the watcher already classifies:
BREAKING changes bump the major version, additive-only batches the minor. The initial contract is 1.0.0.
Release history lives in `output/reports/releases.json` so each report can list the ones before it.

Batching: changes from one rollout arrive over several rebuilds (seconds apart, as the evidence
accumulates), so a release is a *burst*: ChangeReporter.notify() collects changes, and flush_if_quiet()
writes the report once there has been at least one BREAKING change and nothing new for `quiet_seconds`.
Non-breaking-only bursts are kept and folded into the next report (the user asked for reports on breaking
changes; the additive ones still deserve a mention when that report comes).

Everything here is best-effort: the report must never take down the watcher, so every entry point
catches and logs rather than raises.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_QUIET_SECONDS = 12.0
INITIAL_VERSION = "1.0.0"
LATEST_NAME = "contract-report-latest.docx"

_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


# ---------------------------------------------------------------------------
# Versioning + state
# ---------------------------------------------------------------------------

def bump(version: str, breaking: bool) -> str:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version or "")
    major, minor, patch = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (1, 0, 0)
    return f"{major + 1}.0.0" if breaking else f"{major}.{minor + 1}.0"


def _change_dict(c: Any) -> dict[str, Any]:
    return asdict(c) if is_dataclass(c) else dict(c)


class ReleaseState:
    """releases.json: current version + one entry per report written."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.version = INITIAL_VERSION
        self.releases: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.version = str(data.get("version") or INITIAL_VERSION)
            self.releases = list(data.get("releases") or [])
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"version": self.version, "releases": self.releases}, indent=2) + "\n",
                             encoding="utf-8")


# ---------------------------------------------------------------------------
# Wording: what a change means for a consumer, in one line
# ---------------------------------------------------------------------------

def _field_name(location: str) -> str:
    return location.rsplit(".", 1)[-1] if location else ""


def _where(location: str) -> str:
    """"response.200.body.date_of_birth" -> "200 response body"; "request.body.x" -> "request body";
    "request.query.limit" -> "query parameter"."""
    parts = (location or "").split(".")
    if not parts or not parts[0]:
        return "endpoint"
    if parts[0] == "request":
        if len(parts) > 1 and parts[1] == "body":
            return "request body"
        if len(parts) > 1:
            return f"{parts[1]} parameter"
        return "request"
    if parts[0] == "response" and len(parts) > 1:
        return f"{parts[1]} response body"
    return location


def migration_hint(c: dict[str, Any]) -> str:
    kind, loc, detail = c.get("kind", ""), c.get("location", ""), c.get("detail", "")
    name = _field_name(loc)
    is_request = loc.startswith("request")
    if kind == "field_renamed":
        m = re.search(r"renamed to (\S+)", detail)
        new = m.group(1) if m else "the new name"
        verb = "Send" if is_request else "Read"
        return f"{verb} `{new}` instead of `{name}`; the old name is {'no longer accepted' if is_request else 'no longer returned'}."
    if kind == "field_removed":
        return (f"Stop sending `{name}`; the service ignores or rejects it." if is_request
                else f"Remove any dependency on `{name}` in the {_where(loc)}; it is no longer returned.")
    if kind == "type_changed":
        return f"Update parsing/validation of `{name}`: {detail}."
    if kind == "field_added" and c.get("breaking"):
        return f"Send `{name}` in every request; requests without it are rejected (400)."
    if kind == "became_required":
        return (f"Always send `{name}`; it is now required." if is_request
                else f"`{name}` is now always present — consumers may rely on it.")
    if kind == "became_optional":
        return f"Handle a missing `{name}`; it is no longer guaranteed."
    if kind == "endpoint_removed":
        return "Remove calls to this endpoint; it no longer responds."
    if kind == "endpoint_added":
        return "New endpoint available; no action needed."
    if kind == "status_added":
        return f"Handle this status in error/edge-case paths ({detail})."
    if kind == "status_removed":
        return "This status is no longer observed; nothing to do."
    if kind == "field_added":
        return f"New field `{name}` is available; optional for consumers."
    return detail or "Review the change."


def kind_label(kind: str) -> str:
    return {"field_renamed": "Field renamed", "field_removed": "Field removed", "field_added": "Field added",
            "type_changed": "Type changed", "became_required": "Became required", "became_optional": "Became optional",
            "endpoint_added": "Endpoint added", "endpoint_removed": "Endpoint removed",
            "status_added": "Status added", "status_removed": "Status removed"}.get(kind, kind.replace("_", " "))


# ---------------------------------------------------------------------------
# Contract flattening (spec -> rows for the tables)
# ---------------------------------------------------------------------------

def _type_of(schema: dict[str, Any]) -> str:
    if not isinstance(schema, dict):
        return "any"
    if "anyOf" in schema:
        return " | ".join(_type_of(b) for b in schema["anyOf"])
    t = schema.get("type") or "object"
    if isinstance(t, list):
        t = " | ".join(t)
    if schema.get("format"):
        t = f"{t} ({schema['format']})"
    if schema.get("enum"):
        vals = [str(v) for v in schema["enum"] if v is not None]
        shown = ", ".join(vals[:6]) + (", …" if len(vals) > 6 else "")
        t = f"enum: {shown}"
    if schema.get("nullable"):
        t += ", nullable"
    return t


def flatten_fields(schema: dict[str, Any] | None, prefix: str = "", required: bool = True) -> list[tuple[str, str, str]]:
    """[(dotted name, type, "required"/"optional")] for every property, depth-first."""
    rows: list[tuple[str, str, str]] = []
    if not isinstance(schema, dict):
        return rows
    if "anyOf" in schema and not schema.get("properties"):
        for b in schema["anyOf"]:
            rows.extend(flatten_fields(b, prefix, required))
        return rows
    props = schema.get("properties") or {}
    req = set(schema.get("required") or [])
    for name, sub in props.items():
        dotted = f"{prefix}{name}"
        rows.append((dotted, _type_of(sub), "required" if name in req else "optional"))
        if isinstance(sub, dict):
            if sub.get("type") == "object" or "properties" in sub:
                rows.extend(flatten_fields(sub, dotted + ".", name in req))
            elif sub.get("type") == "array" and isinstance(sub.get("items"), dict) and "properties" in sub["items"]:
                rows.extend(flatten_fields(sub["items"], dotted + "[].", name in req))
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict) and not props:
        rows.extend(flatten_fields(schema["items"], prefix + "[].", required))
    return rows


def operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    out = []
    for path, item in sorted((spec.get("paths") or {}).items()):
        if not isinstance(item, dict):
            continue
        for method in _HTTP_METHODS:
            if method in item and isinstance(item[method], dict):
                out.append((method.upper(), path, item[method]))
    return out


def _json_schema(media_parent: dict[str, Any]) -> dict[str, Any] | None:
    content = (media_parent or {}).get("content") or {}
    media = content.get("application/json") or next(iter(content.values()), {}) if content else {}
    return media.get("schema") if isinstance(media, dict) else None


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

_NAVY = (0x1F, 0x3A, 0x5F)
_RED = (0xB0, 0x1E, 0x1E)
_GREEN = (0x1E, 0x7B, 0x3A)
_GREY = (0x59, 0x59, 0x59)
_HEADER_FILL = "1F3A5F"
_BREAKING_FILL = "FBE9E7"
_OK_FILL = "E8F5E9"
_ZEBRA_FILL = "F5F7FA"


def _rgb(t: tuple[int, int, int]):
    from docx.shared import RGBColor
    return RGBColor(*t)


def _shade(cell, fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def _set_cell(cell, text: str, bold: bool = False, color: tuple[int, int, int] | None = None,
              size: float = 9.0, mono: bool = False) -> None:
    from docx.shared import Pt
    cell.text = ""
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(0)
    run = p.add_run(str(text))
    run.bold = bold
    run.font.size = Pt(size)
    if mono:
        run.font.name = "Consolas"
    if color:
        run.font.color.rgb = _rgb(color)


def _table(doc, headers: list[str], rows: list[list[str]], widths_cm: list[float],
           row_fill: Any = None, mono_cols: set[int] = frozenset()) -> None:
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.shared import Cm
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(headers):
        cell = table.rows[0].cells[i]
        _set_cell(cell, h, bold=True, color=(0xFF, 0xFF, 0xFF), size=9)
        _shade(cell, _HEADER_FILL)
    for r_i, row in enumerate(rows):
        cells = table.add_row().cells
        fill = row_fill(r_i, row) if row_fill else (_ZEBRA_FILL if r_i % 2 else None)
        for i, value in enumerate(row):
            _set_cell(cells[i], value, size=8.5, mono=i in mono_cols)
            if fill:
                _shade(cells[i], fill)
    table.autofit = False  # Word honours cell widths; LibreOffice only with autofit off + column widths
    for i, w in enumerate(widths_cm):
        table.columns[i].width = Cm(w)
    for row in table.rows:
        for i, w in enumerate(widths_cm):
            row.cells[i].width = Cm(w)
    doc.add_paragraph()


def _heading(doc, text: str, level: int) -> None:
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        run.font.color.rgb = _rgb(_NAVY)


def _para(doc, text: str, size: float = 10.5, italic: bool = False, color: tuple[int, int, int] | None = None):
    from docx.shared import Pt
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.size = Pt(size)
    run.italic = italic
    if color:
        run.font.color.rgb = _rgb(color)
    return p


def _endpoint_label(c: dict[str, Any]) -> str:
    return f"{c.get('method', '')} {c.get('path', '')}".strip()


def rule_based_summary(spec_title: str, old_v: str, new_v: str, breaking: list[dict], other: list[dict],
                       n_endpoints: int) -> str:
    eps = sorted({_endpoint_label(c) for c in breaking})
    kinds = sorted({kind_label(c["kind"]).lower() for c in breaking})
    text = (f"The {spec_title} contract moved from version {old_v} to {new_v}. Traffic analysis found "
            f"{len(breaking)} breaking change{'s' if len(breaking) != 1 else ''}")
    if eps:
        text += f" affecting {len(eps)} endpoint{'s' if len(eps) != 1 else ''} ({', '.join(eps[:4])}{', …' if len(eps) > 4 else ''})"
    if kinds:
        text += f": {', '.join(kinds)}"
    text += "."
    if other:
        text += f" {len(other)} further change{'s were' if len(other) != 1 else ' was'} additive and need no client action."
    text += (f" Clients built against {old_v} will fail on the breaking items below until updated; the "
             f"mock server and the OpenAPI document already reflect version {new_v} ({n_endpoints} endpoints).")
    return text


def llm_summary(llm: Any, spec_title: str, old_v: str, new_v: str, breaking: list[dict], other: list[dict]) -> str | None:
    """Optional: ask the AIDH client for a short executive summary. Any failure -> None (rule-based used)."""
    if llm is None:
        return None
    try:
        lines = [f"- {'BREAKING' if c.get('breaking') else 'ok'} {c['kind']} {_endpoint_label(c)} {c.get('location', '')}: {c.get('detail', '')}"
                 for c in (breaking + other)[:25]]
        system = ("You write the executive summary of an API change report for frontend and mobile developers. "
                  "Reply with 3-4 plain sentences: what changed, who is affected, what they must do. "
                  "No markdown, no bullet points, no preamble.")
        user = f"API: {spec_title}. Version {old_v} -> {new_v}.\nDetected changes:\n" + "\n".join(lines)
        text = llm.chat(system, user) if hasattr(llm, "chat") else None
        if isinstance(text, str) and 40 < len(text.strip()) < 1500:
            return text.strip()
    except Exception as e:  # noqa: BLE001
        log.warning("LLM summary failed: %s", e)
    return None


def build_report(spec: dict[str, Any], changes: list[Any], out_path: str | Path, *, old_version: str,
                 new_version: str, releases: list[dict[str, Any]] | None = None, log_path: str | Path | None = None,
                 llm: Any = None, generated_at: datetime | None = None) -> Path:
    """Write the .docx. `changes` are SpecChange dataclasses or dicts (this release's burst)."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    rows = [_change_dict(c) for c in changes]
    breaking = [c for c in rows if c.get("breaking")]
    other = [c for c in rows if not c.get("breaking")]
    when = generated_at or datetime.now(timezone.utc)
    title = (spec.get("info") or {}).get("title") or "Inferred API"
    ops = operations(spec)

    doc = Document()
    for section in doc.sections:
        section.left_margin = section.right_margin = Cm(2.0)
        section.top_margin = section.bottom_margin = Cm(1.8)
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10.5)

    # ---- title block ----
    t = doc.add_paragraph()
    r = t.add_run("API Contract Change Report")
    r.bold = True
    r.font.size = Pt(24)
    r.font.color.rgb = _rgb(_NAVY)
    sub = doc.add_paragraph()
    r = sub.add_run(f"{title}  ·  version {old_version} → {new_version}")
    r.font.size = Pt(13)
    r.font.color.rgb = _rgb(_GREY)
    meta = doc.add_paragraph()
    r = meta.add_run(f"Generated {when.strftime('%Y-%m-%d %H:%M UTC')} from observed traffic"
                     + (f" ({Path(log_path).name})" if log_path else "") + ". No documentation was hand-written.")
    r.font.size = Pt(9)
    r.italic = True
    r.font.color.rgb = _rgb(_GREY)

    # ---- at a glance ----
    _heading(doc, "At a glance", 1)
    affected = sorted({_endpoint_label(c) for c in breaking})
    glance = doc.add_table(rows=1, cols=4)
    glance.style = "Table Grid"
    for i, (label, value, color) in enumerate((
        ("Breaking changes", str(len(breaking)), _RED if breaking else _GREEN),
        ("Additive changes", str(len(other)), _GREEN),
        ("Endpoints affected", str(len(affected)), _RED if affected else _GREEN),
        ("Endpoints in contract", str(len(ops)), _NAVY),
    )):
        cell = glance.rows[0].cells[i]
        cell.text = ""
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(value)
        run.bold = True
        run.font.size = Pt(20)
        run.font.color.rgb = _rgb(color)
        p2 = cell.add_paragraph(label)
        p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p2.runs[0].font.size = Pt(8.5)
        p2.runs[0].font.color.rgb = _rgb(_GREY)
    doc.add_paragraph()

    # ---- summary ----
    _heading(doc, "Summary", 1)
    summary = llm_summary(llm, title, old_version, new_version, breaking, other)
    if summary:
        _para(doc, summary)
        _para(doc, "Summary written by the configured LLM from the detected changes; the tables below are "
                   "the evidence.", size=8.5, italic=True, color=_GREY)
    else:
        _para(doc, rule_based_summary(title, old_version, new_version, breaking, other, len(ops)))

    # ---- breaking changes ----
    _heading(doc, f"Breaking changes ({len(breaking)})", 1)
    if breaking:
        _para(doc, "Each row is a change that will break a client built against the previous version. "
                   "The last column says what that client must do.", size=9.5, color=_GREY)
        _table(doc, ["#", "Endpoint", "Where", "Change", "Detected", "What consumers must do"],
               [[str(i + 1), _endpoint_label(c), _where(c.get("location", "")),
                 f"{kind_label(c['kind'])}: {c.get('detail', '')}",
                 (c.get("detected_at") or "")[11:19], migration_hint(c)] for i, c in enumerate(breaking)],
               [0.7, 4.2, 2.2, 4.8, 1.7, 4.4], row_fill=lambda i, r: _BREAKING_FILL, mono_cols={1})
    else:
        _para(doc, "None in this release.", italic=True, color=_GREY)

    # ---- other changes ----
    _heading(doc, f"Other changes ({len(other)})", 1)
    if other:
        _para(doc, "Additive or informational — existing clients keep working.", size=9.5, color=_GREY)
        _table(doc, ["#", "Endpoint", "Where", "Change", "Note"],
               [[str(i + 1), _endpoint_label(c), _where(c.get("location", "")),
                 f"{kind_label(c['kind'])}: {c.get('detail', '')}", migration_hint(c)] for i, c in enumerate(other)],
               [0.7, 4.4, 2.3, 5.6, 5.0], row_fill=lambda i, r: _OK_FILL if i % 2 == 0 else None, mono_cols={1})
    else:
        _para(doc, "None in this release.", italic=True, color=_GREY)

    # ---- current contract ----
    doc.add_page_break()
    _heading(doc, f"Current contract — version {new_version}", 1)
    _para(doc, f"The full API as observed in traffic, {len(ops)} endpoint{'s' if len(ops) != 1 else ''}. "
               "Types, required/optional flags and status codes are inferred from real requests and responses; "
               "the same contract is served by the mock server and published as openapi.json.", size=9.5, color=_GREY)
    affected_set = set(affected)
    for method, path, op in ops:
        label = f"{method} {path}"
        _heading(doc, label + ("   ⚠ changed in this release" if label in affected_set else ""), 2)
        if op.get("summary") and op["summary"] != label:
            _para(doc, op["summary"], size=10)
        if op.get("description"):
            _para(doc, op["description"], size=9.5, color=_GREY)
        facts = []
        if op.get("x-sample-count"):
            facts.append(f"{op['x-sample-count']} calls observed")
        if op.get("security"):
            facts.append("requires Authorization")
        if facts:
            _para(doc, " · ".join(facts), size=8.5, italic=True, color=_GREY)

        params = op.get("parameters") or []
        if params:
            _para(doc, "Parameters", size=9.5).runs[0].bold = True
            _table(doc, ["Name", "In", "Type", "Required"],
                   [[p.get("name", ""), p.get("in", ""), _type_of(p.get("schema") or {}),
                     "yes" if p.get("required") else "no"] for p in params],
                   [5.0, 2.5, 6.0, 2.5], mono_cols={0})

        req_schema = _json_schema(op.get("requestBody") or {})
        if req_schema is not None:
            required_body = (op.get("requestBody") or {}).get("required", True)
            _para(doc, f"Request body ({'required' if required_body else 'optional'})", size=9.5).runs[0].bold = True
            fields = flatten_fields(req_schema)
            _table(doc, ["Field", "Type", "Required"],
                   [[n, t, r] for n, t, r in fields] or [["(empty object)", "object", ""]],
                   [7.0, 7.0, 3.0], mono_cols={0})

        responses = op.get("responses") or {}
        if responses:
            _para(doc, "Responses", size=9.5).runs[0].bold = True
            resp_rows = []
            for status in sorted(responses, key=lambda s: str(s)):
                resp = responses[status] or {}
                schema = _json_schema(resp)
                fields = flatten_fields(schema)
                names = ", ".join(n for n, _, _ in fields if "." not in n and "[]" not in n)
                if len(names) > 160:
                    names = names[:157] + "…"
                tag = " (inferred, not observed)" if resp.get("x-inferred") else ""
                resp_rows.append([str(status), (resp.get("description") or "") + tag, names or "(no body)"])
            _table(doc, ["Status", "Meaning", "Body fields"], resp_rows, [2.0, 4.5, 11.5], mono_cols={0})
            # the success body in full (nested fields included) — what a consumer actually codes against;
            # error bodies are small and already summarised above
            for status in sorted(responses, key=lambda s: str(s)):
                if not str(status).startswith("2"):
                    continue
                fields = flatten_fields(_json_schema(responses[status] or {}))
                if fields:
                    _para(doc, f"{status} response body", size=9.5).runs[0].bold = True
                    _table(doc, ["Field", "Type", "Required"], [[n, t, r] for n, t, r in fields],
                           [7.0, 7.0, 3.0], mono_cols={0})

    # ---- release history ----
    if releases:
        doc.add_page_break()
        _heading(doc, "Release history", 1)
        _table(doc, ["Version", "Date (UTC)", "Breaking", "Additive", "Report"],
               [[r.get("version", ""), (r.get("generated_at") or "")[:16].replace("T", " "),
                 str(r.get("breaking", 0)), str(r.get("additive", 0)), r.get("file", "")] for r in reversed(releases)],
               [2.5, 3.5, 2.0, 2.0, 8.0], mono_cols={4})

    foot = doc.add_paragraph()
    fr = foot.add_run("Generated automatically by the API contract generator from raw HTTP logs. "
                      "Breaking/additive classification follows the watcher's rules (see README).")
    fr.font.size = Pt(8)
    fr.italic = True
    fr.font.color.rgb = _rgb(_GREY)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    doc.save(str(tmp))
    tmp.replace(out)
    return out


# ---------------------------------------------------------------------------
# The reporter the watcher drives
# ---------------------------------------------------------------------------

class ChangeReporter:
    """Collects the watcher's changes and writes one report per breaking burst (see module docstring).

    notify(spec, changes, now) after every rebuild that produced changes; flush_if_quiet(now) on every
    loop tick; flush() at shutdown so a burst still settling when the demo stops is not lost."""

    def __init__(self, spec_path: str | Path, reports_dir: str | Path | None = None, *,
                 quiet_seconds: float = DEFAULT_QUIET_SECONDS, log_path: str | Path | None = None,
                 llm: Any = None, say: Any = None) -> None:
        self.spec_path = Path(spec_path)
        self.reports_dir = Path(reports_dir) if reports_dir else self.spec_path.parent / "reports"
        self.quiet_seconds = quiet_seconds
        self.log_path = log_path
        self.llm = llm
        self.say = say or (lambda m: log.info("%s", m))
        self.state = ReleaseState(self.reports_dir / "releases.json")
        self.pending: list[dict[str, Any]] = []
        self.spec: dict[str, Any] | None = None
        self.last_change: float | None = None
        self.written: list[Path] = []

    @property
    def pending_breaking(self) -> int:
        return sum(1 for c in self.pending if c.get("breaking"))

    def notify(self, spec: dict[str, Any], changes: list[Any], now: float) -> None:
        if not changes:
            return
        self.pending.extend(_change_dict(c) for c in changes)
        self.spec = spec
        self.last_change = now

    def flush_if_quiet(self, now: float) -> Path | None:
        if self.pending_breaking == 0 or self.last_change is None:
            return None
        if now - self.last_change < self.quiet_seconds:
            return None
        return self.flush()

    def flush(self) -> Path | None:
        """Write the report for whatever is pending (only if it includes a breaking change)."""
        if self.pending_breaking == 0 or self.spec is None:
            return None
        changes, self.pending = self.pending, []
        old_v = self.state.version
        new_v = bump(old_v, breaking=True)
        when = datetime.now(timezone.utc)
        name = f"contract-report-v{new_v}-{when.strftime('%Y%m%d-%H%M%S')}.docx"
        try:
            out = build_report(self.spec, changes, self.reports_dir / name, old_version=old_v, new_version=new_v,
                               releases=self.state.releases, log_path=self.log_path, llm=self.llm, generated_at=when)
            latest = self.spec_path.parent / LATEST_NAME
            try:
                latest.write_bytes(out.read_bytes())
            except OSError as e:  # Word may hold the previous "latest" open on Windows
                log.warning("could not refresh %s: %s", latest, e)
            self.state.version = new_v
            self.state.releases.append({
                "version": new_v, "previous": old_v, "generated_at": when.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "breaking": sum(1 for c in changes if c.get("breaking")),
                "additive": sum(1 for c in changes if not c.get("breaking")),
                "file": out.name, "changes": changes,
            })
            self.state.save()
            self.written.append(out)
            self.say(f"REPORT: contract v{old_v} -> v{new_v}, {self.state.releases[-1]['breaking']} breaking / "
                     f"{self.state.releases[-1]['additive']} additive -> {out}")
            return out
        except Exception as e:  # noqa: BLE001 — never take the watcher down over a document
            log.warning("could not write change report: %s: %s", type(e).__name__, e)
            self.say(f"WARNING: could not write change report: {type(e).__name__}: {e}")
            self.pending = changes + self.pending  # try again with the next burst
            return None
