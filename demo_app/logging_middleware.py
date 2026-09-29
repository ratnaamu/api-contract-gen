"""ASGI middleware that writes one JSON line per API request in the exact format parser.py reads:

    {"timestamp", "method", "path", "query", "headers", "request_body", "status", "response_body"}

Plain ASGI (not BaseHTTPMiddleware) so it can capture both bodies without interfering with FastAPI.
It also turns unhandled exceptions into a JSON 500 (so they are logged too) and can inject random
500s for realism. Authorization (and other credential headers) are masked before anything is written.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl

log = logging.getLogger(__name__)

# Headers whose values must never reach the log file.
_SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
_REDACTED = "<redacted>"


def mask_header(name: str, value: str) -> str:
    """"Bearer abc.def" -> "Bearer <redacted>" (keep the scheme, it's useful); other secrets -> "<redacted>"."""
    if name.lower() not in _SECRET_HEADERS:
        return value
    if name.lower() in ("authorization", "proxy-authorization"):
        scheme, _, credentials = value.strip().partition(" ")
        if credentials and scheme.isalpha():
            return f"{scheme} {_REDACTED}"
    return _REDACTED


def _canonical(name: str) -> str:
    """"user-agent" -> "User-Agent" (ASGI lower-cases header names)."""
    return "-".join(part[:1].upper() + part[1:] for part in name.split("-"))


def _decode_body(raw: bytes) -> Any:
    """JSON if it parses, the raw text if it doesn't (e.g. a malformed request), None if empty."""
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonLineWriter:
    """Appends lines to a file, thread-safe. Opens the file for every line, so the log can be deleted or
    truncated underneath a running app (e.g. `main.py watch --fresh`) and writing just continues."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line)


class JsonLogMiddleware:
    def __init__(
        self,
        app: Callable,
        writer: JsonLineWriter,
        should_log: Callable[[str], bool],
        error_rate: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        self.app = app
        self.writer = writer
        self.should_log = should_log
        self.error_rate = error_rate
        self.rng = rng or random.Random()
        self._rng_lock = threading.Lock()

    def _inject_error(self) -> bool:
        if self.error_rate <= 0:
            return False
        with self._rng_lock:
            return self.rng.random() < self.error_rate

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or not self.should_log(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        # ---- read the whole request body, then replay it to the app ----
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break  # client disconnected
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        raw_request = b"".join(chunks)
        replayed = False

        async def replay_receive() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": raw_request, "more_body": False}
            return await receive()

        # ---- capture the response as it goes out ----
        status: int | None = None
        response_chunks: list[bytes] = []

        async def capture_send(message: dict) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                response_chunks.append(message.get("body", b""))
            await send(message)

        async def send_json_500(body: dict) -> None:
            data = json.dumps(body).encode()
            await capture_send({"type": "http.response.start", "status": 500,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"content-length", str(len(data)).encode())]})
            await capture_send({"type": "http.response.body", "body": data})

        try:
            if self._inject_error():
                await send_json_500({"error": "internal_error", "message": "unexpected error, please retry",
                                     "request_id": str(uuid.uuid4())})
            else:
                await self.app(scope, replay_receive, capture_send)
        except Exception:  # noqa: BLE001 — log it and answer with JSON instead of a text 500
            log.error("unhandled error on %s %s\n%s", scope.get("method"), scope.get("path"), traceback.format_exc())
            if status is None:
                await send_json_500({"error": "internal_error", "message": "unexpected error",
                                     "request_id": str(uuid.uuid4())})
        finally:
            self._write(scope, raw_request, status, b"".join(response_chunks))

    def _write(self, scope: dict, raw_request: bytes, status: int | None, raw_response: bytes) -> None:
        headers: dict[str, str] = {}
        for k, v in scope.get("headers") or []:
            name = _canonical(k.decode("latin-1"))
            headers[name] = mask_header(name, v.decode("latin-1"))
        query = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True))
        record = {
            "timestamp": _now_iso(),
            "method": scope.get("method", "GET"),
            "path": scope.get("path", "/"),
            "query": query,
            "headers": headers,
            "request_body": _decode_body(raw_request),
            "status": status if status is not None else 500,
            "response_body": _decode_body(raw_response),
        }
        try:
            self.writer.write(record)
        except OSError as e:  # never break the API because the log file is locked
            log.warning("could not write log line to %s: %s", self.writer.path, e)
