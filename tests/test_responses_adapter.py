"""Tests for the Responses-API status adapter.

The shape under test is not invented: it is the request captured from the live
gateway on 2026-09-17, which the provider rejected with
`400 missing '*.status' parameter (param: input.status)` and which returned 200
once `status` was added. These tests pin that transformation and the proxy
behaviour around it.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aiops_diagnostics.responses_adapter import (
    DEFAULT_STATUS,
    AdapterConfigError,
    AdapterSettings,
    build_settings,
    make_server,
    patch_body,
    patch_responses_payload,
)

# The item types that actually appeared in the captured failing request.
CAPTURED_HISTORY = [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    {"type": "reasoning", "summary": []},
    {"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": "c1"},
    {"type": "function_call_output", "call_id": "c1", "output": "ok"},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
]


def _captured_payload() -> dict:
    return {
        "model": "deepseek-v4-flash",
        "stream": True,
        "store": False,
        "input": [dict(item) for item in CAPTURED_HISTORY],
        "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
        "tool_choice": "auto",
    }


def test_every_captured_history_item_gets_a_status() -> None:
    """The exact regression: history items missing `status` are rejected."""
    payload, count = patch_responses_payload(_captured_payload())

    assert count == len(CAPTURED_HISTORY)
    history = payload["input"]
    assert all("status" in item for item in history)
    assert all(item["status"] == DEFAULT_STATUS for item in history)


def test_items_that_already_have_a_status_are_left_alone() -> None:
    """An item the client already annotated must not be overwritten."""
    payload = {"input": [{"type": "message", "status": "in_progress", "content": []}]}
    patched, count = patch_responses_payload(payload)
    assert count == 0
    assert patched["input"][0]["status"] == "in_progress"


def test_unknown_item_types_are_not_touched() -> None:
    """Guessing a status for a shape we do not own is how an adapter breaks
    something new, so unknown types pass through untouched."""
    payload = {"input": [{"type": "some_future_item_type", "data": 1}]}
    patched, count = patch_responses_payload(payload)
    assert count == 0
    assert "status" not in patched["input"][0]


def test_string_input_is_a_plain_prompt_and_is_unchanged() -> None:
    payload = {"model": "m", "input": "just a prompt"}
    patched, count = patch_responses_payload(payload)
    assert count == 0
    assert patched["input"] == "just a prompt"


def test_non_dict_payload_is_returned_unchanged() -> None:
    for value in (None, [], "text", 7):
        patched, count = patch_responses_payload(value)
        assert count == 0
        assert patched == value


def test_other_request_fields_survive_the_patch() -> None:
    """Only `input` changes; tools, model and streaming flags must be intact."""
    original = _captured_payload()
    patched, count = patch_responses_payload(original)
    assert count > 0
    for key in ("model", "stream", "store", "tools", "tool_choice"):
        assert patched[key] == original[key]


def test_patch_body_handles_bytes_and_leaves_bad_json_alone() -> None:
    body = json.dumps(_captured_payload()).encode()
    patched, count = patch_body(body)
    assert count == len(CAPTURED_HISTORY)
    assert json.loads(patched)["input"][0]["status"] == DEFAULT_STATUS

    # Malformed JSON is forwarded untouched rather than turned into a shim error.
    raw = b"{not json"
    assert patch_body(raw) == (raw, 0)
    # A body with nothing to fix is returned byte-identical (no needless re-encode).
    already = json.dumps({"input": [{"type": "message", "status": "completed"}]}).encode()
    assert patch_body(already) == (already, 0)


def test_settings_reject_a_bad_upstream() -> None:
    with pytest.raises(AdapterConfigError):
        AdapterSettings(upstream_url="not-a-url").validate()
    with pytest.raises(AdapterConfigError):
        AdapterSettings(upstream_url="https://ok", listen_port=70000).validate()
    # 0 is allowed: it asks the OS for a free port (used by the e2e test).
    AdapterSettings(upstream_url="https://ok", listen_port=0).validate()


def test_build_settings_requires_an_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOPS_RESPONSES_ADAPTER_UPSTREAM", raising=False)
    with pytest.raises(AdapterConfigError):
        build_settings()


# --------------------------------------------------------------------------
# End to end: a real upstream server, through the real adapter
# --------------------------------------------------------------------------


class _RecordingUpstream(BaseHTTPRequestHandler):
    """Stands in for the provider: 400s when any history item lacks status."""

    received: list[dict] = []
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).received.append(payload)

        missing = [
            item
            for item in payload.get("input", [])
            if isinstance(item, dict) and item.get("type") == "message" and "status" not in item
        ]
        if missing:
            body = json.dumps(
                {"error": {"message": "missing `*.status` parameter", "param": "input.status"}}
            ).encode()
            self.send_response(400)
        else:
            body = json.dumps({"ok": True, "items": len(payload.get("input", []))}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def test_proxy_turns_the_rejected_request_into_an_accepted_one() -> None:
    """The whole point: the same body that 400s directly must 200 through the
    adapter, with everything else preserved."""
    _RecordingUpstream.received = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingUpstream)
    _start(upstream)
    adapter = make_server(
        AdapterSettings(
            listen_host="127.0.0.1",
            listen_port=0,
            upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
        )
    )
    _start(adapter)
    try:
        payload = _captured_payload()

        # Direct to the upstream: rejected, exactly like the live provider.
        direct = urllib.request.Request(
            f"http://127.0.0.1:{upstream.server_address[1]}/responses",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(direct, timeout=10)
        assert err.value.code == 400

        # Through the adapter: accepted, and the upstream saw the status field.
        through = urllib.request.Request(
            f"http://127.0.0.1:{adapter.server_address[1]}/responses",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer test"},
        )
        with urllib.request.urlopen(through, timeout=10) as response:
            assert response.status == 200
            assert json.loads(response.read())["ok"] is True

        seen = _RecordingUpstream.received[-1]
        assert all("status" in item for item in seen["input"])
        # Non-input fields must arrive intact.
        assert seen["model"] == payload["model"]
        assert seen["tools"] == payload["tools"]
        # Auth is forwarded (with the response's chunked framing re-encoded by
        # the client, so compare against what we sent).
        assert _RecordingUpstream.received[-1]["input"][0]["type"] == "message"
    finally:
        adapter.shutdown()
        upstream.shutdown()
        adapter.server_close()
        upstream.server_close()


def test_proxy_healthz() -> None:
    adapter = make_server(
        AdapterSettings(listen_host="127.0.0.1", listen_port=0, upstream_url="http://127.0.0.1:1")
    )
    _start(adapter)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{adapter.server_address[1]}/healthz", timeout=5
        ) as response:
            assert json.loads(response.read()) == {"ok": True}
    finally:
        adapter.shutdown()
        adapter.server_close()


def test_adapter_protocol_is_http_11_so_chunked_streaming_can_be_framed() -> None:
    """Regression (41 live, 2026-09-17): with the stdlib's HTTP/1.0 default the
    relay's `Transfer-Encoding: chunked` was unframeable by the client, which
    then dropped the connection mid-stream — Codex reported "stream disconnected
    before completion" and every turn failed. The version is load-bearing."""
    from aiops_diagnostics.responses_adapter import ResponsesStatusHandler

    assert ResponsesStatusHandler.protocol_version == "HTTP/1.1"


def test_streamed_response_is_relayed_with_chunked_framing() -> None:
    """The body must arrive intact through the chunked relay."""
    import http.client

    class _Streaming(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            # An SSE-ish stream with no Content-Length, like the provider's.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(5):
                chunk = f"event: tick\ndata: {i}\n\n".encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Streaming)
    _start(upstream)
    adapter = make_server(
        AdapterSettings(
            listen_host="127.0.0.1",
            listen_port=0,
            upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
        )
    )
    _start(adapter)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", adapter.server_address[1], timeout=10)
        conn.request(
            "POST",
            "/responses",
            body=json.dumps({"input": [{"type": "message", "content": []}]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        body = response.read().decode()
        conn.close()

        assert response.status == 200
        # All five events survived the relay, in order and complete.
        assert body.count("event: tick") == 5
        assert "data: 4" in body
        # The version the client actually sees. http.client tolerates chunked
        # framing regardless of version, so the body assertions above cannot
        # distinguish 1.0 from 1.1 — but a strict client (Codex) refuses to
        # frame a chunked stream advertised as HTTP/1.0 and drops the
        # connection. Assert the wire version, which is what it checks.
        assert response.version == 11, (
            f"adapter must advertise HTTP/1.1, got {response.version} "
            "(10 = HTTP/1.0, which cannot carry a chunked stream)"
        )
    finally:
        adapter.shutdown()
        upstream.shutdown()
        adapter.server_close()
        upstream.server_close()


# --------------------------------------------------------------------------
# Grammar x tools conflict (2026-09-20)
#
# Measured against the live endpoint through the same base URL the gateway
# uses: tools-only 200, grammar-only 200, both 400 `Constrained
# response_format/guided_grammar cannot be combined with active tools`. The
# agent always sends both, so every diagnosis failed.
# --------------------------------------------------------------------------


def _grammar() -> dict[str, object]:
    return {
        "type": "json_schema",
        "name": "agent_turn",
        "strict": True,
        "schema": {"type": "object", "properties": {"kind": {"type": "string"}}},
    }


def _tool(name: str = "order_snapshot") -> dict[str, object]:
    return {
        "type": "function",
        "name": name,
        "description": "read one order",
        "parameters": {"type": "object", "properties": {}},
    }


def _statused_input() -> list[dict[str, object]]:
    """A history item the status patch leaves alone, so grammar tests stay isolated."""
    return [{"type": "message", "status": "completed"}]


def test_grammar_is_dropped_when_the_request_also_declares_tools() -> None:
    payload = {"input": _statused_input(), "tools": [_tool()], "text": {"format": _grammar()}}

    patched, count = patch_body(json.dumps(payload).encode())

    assert count == 1
    result = json.loads(patched)
    assert "format" not in (result.get("text") or {})
    assert result["tools"] == [_tool()]


def test_the_empty_text_object_is_dropped_too_not_left_as_a_stub() -> None:
    payload = {"input": _statused_input(), "tools": [_tool()], "text": {"format": _grammar()}}

    patched, _ = patch_body(json.dumps(payload).encode())

    assert json.loads(patched)["text"] is None


def test_grammar_survives_when_there_are_no_tools() -> None:
    """The conflict is with ACTIVE tools; a tool-less request keeps its schema."""
    payload = {"input": _statused_input(), "text": {"format": _grammar()}}
    body = json.dumps(payload).encode()

    assert patch_body(body) == (body, 0)


def test_an_empty_tool_list_is_not_a_declaration() -> None:
    payload = {"input": _statused_input(), "tools": [], "text": {"format": _grammar()}}
    body = json.dumps(payload).encode()

    assert patch_body(body) == (body, 0)


def test_other_text_fields_survive_the_grammar_drop() -> None:
    payload = {
        "input": _statused_input(),
        "tools": [_tool()],
        "text": {"format": _grammar(), "verbosity": "low"},
    }

    patched, _ = patch_body(json.dumps(payload).encode())

    assert json.loads(patched)["text"] == {"verbosity": "low"}


def test_both_gaps_are_repaired_in_one_pass() -> None:
    """A real agent turn has both: history items missing status AND the conflict."""
    payload = {
        "input": [{"type": "function_call", "name": "order_snapshot"}],
        "tools": [_tool()],
        "text": {"format": _grammar()},
    }

    patched, count = patch_body(json.dumps(payload).encode())

    result = json.loads(patched)
    assert count == 2, "one history item patched, one grammar dropped"
    assert result["input"][0]["status"] == "completed"
    assert json.loads(patched).get("text") is None
