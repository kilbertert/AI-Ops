"""The Responses-API status adapter (provider compatibility shim).

Upstream context (2026-09-17): the unified model endpoint rejects any
`/responses` request whose conversation-history items omit `status`, with
`400 missing '***.status' parameter (param: input.status)`. Codex resends such
items on every multi-turn tool exchange and never sends `status`, so every
agent run failed once it had one tool round. Adding the field to the same
captured request turned the 400 into a 200, which is what this shim automates.
"""

from __future__ import annotations

import contextlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

CHUNK_BYTES = 64 * 1024

# Item types the Responses API defines a `status` for. Only these are touched;
# unknown types pass through untouched, because inventing a field for a shape
# we do not own is how an adapter becomes a source of new breakage.
STATUS_BEARING_TYPES = frozenset(
    {
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
        "local_shell_call",
        "local_shell_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "web_search_call",
        "file_search_call",
        "computer_call",
        "computer_call_output",
        "image_generation_call",
        "code_interpreter_call",
        "mcp_call",
        "mcp_list_tools",
        "mcp_approval_request",
        "mcp_approval_response",
    }
)

# Terminal-ish statuses. An item the client echoed back is, by definition,
# already finished; the API has no "in_progress" meaning for a history item.
DEFAULT_STATUS = "completed"


def needs_status(item: Any) -> bool:
    """True when this history item is one the endpoint will reject."""
    if not isinstance(item, dict):
        return False
    if "status" in item:
        return False
    return item.get("type") in STATUS_BEARING_TYPES


def patch_responses_payload(payload: Any) -> tuple[Any, int]:
    """Return ``(payload, patched_count)`` with `status` filled in.

    Only ``input`` items are considered, and only when ``input`` is the list
    form the Responses API uses for history. A string `input` is a plain
    prompt and carries no items, so it is returned unchanged.
    """
    if not isinstance(payload, dict):
        return payload, 0
    items = payload.get("input")
    if not isinstance(items, list):
        return payload, 0

    patched = 0
    new_items: list[Any] = []
    for item in items:
        if needs_status(item):
            item = {**item, "status": DEFAULT_STATUS}
            patched += 1
        new_items.append(item)

    if not patched:
        return payload, 0
    return {**payload, "input": new_items}, patched


def patch_body(raw: bytes) -> tuple[bytes, int]:
    """Patch a JSON request body. Non-JSON or non-object bodies pass through.

    A malformed body is forwarded untouched on purpose: this shim exists to fix
    one known gap, not to become a validator that turns a provider error into a
    shim error.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw, 0
    if not isinstance(payload, dict) or payload.get("input") is None:
        return raw, 0
    patched_payload, count = patch_responses_payload(payload)
    if not count:
        return raw, 0
    return json.dumps(patched_payload, ensure_ascii=False).encode("utf-8"), count


# --------------------------------------------------------------------------
# Transparent streaming proxy
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdapterSettings:
    """Configuration for the status shim, read from the environment."""

    listen_host: str = "127.0.0.1"
    listen_port: int = 8799
    upstream_url: str = ""
    timeout_seconds: float = 600.0
    max_request_bytes: int = 8 * 1024 * 1024

    def validate(self) -> None:
        if not self.upstream_url.startswith(("http://", "https://")):
            raise AdapterConfigError("upstream_url must be an absolute http(s) URL")
        # 0 means "let the OS assign a port", which tests rely on. Anything
        # else must be a real port.
        if not 0 <= self.listen_port <= 65535:
            raise AdapterConfigError("listen_port out of range")
        if self.timeout_seconds <= 0:
            raise AdapterConfigError("timeout_seconds must be positive")
        if self.max_request_bytes <= 0:
            raise AdapterConfigError("max_request_bytes must be positive")


class AdapterConfigError(ValueError):
    """The adapter cannot start with this configuration."""


def build_settings() -> AdapterSettings:
    """Read settings from AIOPS_RESPONSES_ADAPTER_* env vars."""
    settings = AdapterSettings(
        listen_host=os.environ.get("AIOPS_RESPONSES_ADAPTER_HOST", "127.0.0.1"),
        listen_port=_env_int("AIOPS_RESPONSES_ADAPTER_PORT", 8799),
        upstream_url=os.environ.get("AIOPS_RESPONSES_ADAPTER_UPSTREAM", ""),
        timeout_seconds=_env_float("AIOPS_RESPONSES_ADAPTER_TIMEOUT", 600.0),
        max_request_bytes=_env_int("AIOPS_RESPONSES_ADAPTER_MAX_REQUEST", 8 * 1024 * 1024),
    )
    settings.validate()
    return settings


class ResponsesStatusHandler(BaseHTTPRequestHandler):
    """Forwards requests upstream, filling in the `status` field on history.

    The response is relayed as a raw stream. Streaming is not an optimisation
    here: Codex consumes the Responses event stream incrementally, so buffering
    the whole body before forwarding would stall every turn.
    """

    settings: ClassVar[AdapterSettings]
    server_version = "AI-Ops-Responses-Status-Adapter/1.0"
    sys_version = ""
    # HTTP/1.1 is required, not cosmetic: the relay streams the response with
    # `Transfer-Encoding: chunked`, which HTTP/1.0 clients cannot frame. With
    # the stdlib default the client cannot tell where the body ends and drops
    # the connection mid-stream ("stream disconnected before completion").
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        settings = self.settings
        if self.headers.get("Transfer-Encoding"):
            self._send_json(400, {"error": "chunked_requests_are_not_supported"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json(411, {"error": "content_length_required"})
            return
        if not 1 <= length <= settings.max_request_bytes:
            self._send_json(413, {"error": "request_too_large"})
            return
        raw = self.rfile.read(length)
        body, patched = patch_body(raw)
        self._forward(body, patched)

    def _forward(self, body: bytes, patched: int) -> None:
        settings = self.settings
        url = settings.upstream_url.rstrip("/") + self.path
        request = urllib.request.Request(url, data=body, method="POST")
        for name, value in self.headers.items():
            if name.lower() in {"host", "content-length", "connection", "transfer-encoding"}:
                continue
            request.add_header(name, value)
        request.add_header("Content-Length", str(len(body)))

        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=settings.timeout_seconds) as response:
                self._relay(response.status, response.headers, response)
        except urllib.error.HTTPError as exc:
            with contextlib.closing(exc):
                self._relay(exc.code, exc.headers, exc)
        except (urllib.error.URLError, TimeoutError):
            self._send_json(502, {"error": "upstream_unavailable"})

    def _relay(self, status: int, headers: Any, stream: Any) -> None:
        """Relay the upstream response, streaming the body in chunks.

        Always chunked: an SSE event stream has no Content-Length, and
        forwarding one would require buffering it whole. A client that hangs up
        mid-stream (Codex abandoning a turn) is normal, not an error, so the
        broken pipe is swallowed rather than logged as a crash.
        """
        self.send_response(status)
        self.send_header("Content-Type", headers.get("Content-Type", "application/json"))
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

        try:
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The caller stopped reading. Nothing to report and nothing to fix.
            self.close_connection = True

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def make_server(settings: AdapterSettings) -> ThreadingHTTPServer:
    settings.validate()
    handler = type("_BoundHandler", (ResponsesStatusHandler,), {"settings": settings})
    server = ThreadingHTTPServer((settings.listen_host, settings.listen_port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    settings = build_settings()
    server = make_server(settings)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect would bypass the status patch, so never follow one."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise AdapterConfigError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise AdapterConfigError(f"{name} must be a number") from exc
