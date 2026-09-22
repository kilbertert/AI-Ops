"""The cancel contract handed to BFF and frontend is the one the API returns (#358).

This ticket hands the waiting-state contract to two teams that are not in this
repository. A handoff whose response samples were typed by hand is the failure this
repository has already lived once: three documents attributed the stop-generation
link to #173, a reviewer signed it off against an unrelated conversation-lifecycle
case, and two defects survived a PASS that never had implementation behind it.

So the samples in the handoff are not written by hand. They are captured here,
drive the real gateway stack once over — real ``GatewayRuntime``, real
``GatewayStore``, real HTTP routes through ``TestClient`` — and compared field by
field with what the document shows. The document drifting away from the API is a
test failure, not something the next reader has to notice.

Two things are masked because they cannot be reproduced: the job id (``qa_`` and
``conv_`` prefixed uuid4 hex) and ISO-8601 timestamps. Everything else — status
codes, every field name, every value — is the response the gateway produced.

The other half of the ticket is the broken attribution that let the two defects
survive: six places in this repository pointed the stop-generation link at the
already-closed #173, whose acceptance row was signed off against CONV-01 — a
conversation-lifecycle case with no stop interface in it. The last two tests keep
that chain pointing at #346 and at a real test, because a corrected document is
exactly what drifts back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HANDOFF = PROJECT_ROOT / "docs" / "agents" / "assistant-cancel-handoff.md"

#: Minted per run, so a sample cannot show the real one and stay reproducible.
_JOB_ID = re.compile(r"\b(qa)_[0-9a-f]{32}\b")
_CONVERSATION_ID = re.compile(r"\b(conv)_[0-9a-f]{32}\b")
_MASKED_JOB_ID = "qa_c1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6"
_MASKED_CONVERSATION_ID = "conv_d4e5f6a7b8c9d0e1f2a3b4c5d6e7f801"
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")
_MASKED_TIMESTAMP = "<ISO 8601 时间戳>"

#: ``<!-- contract-sample: <name> status=<code> -->`` followed by the body fence.
#: The status travels with the sample so the document is the single place a
#: reader checks what the gateway answers, and the test the single place that
#: proves it.
_SAMPLE_MARKER = re.compile(r"<!--\s*contract-sample:\s*(\S+)\s+status=(\d+)\s*-->")

STOP_QUESTION = "电动车的电池保养怎么做"
FAQ_QUESTION = "充电前支付的预授权/预充值金额，什么时候退回？"
UNKNOWN_JOB_ID = "qa_nonexistent000000000000000000000001"

#: The documents the PRD names as carrying the broken #173 attribution, plus the
#: runbook whose merged evidence row was the proof of it. A stop/cancel link here
#: may only be attributed to the PRD that implements it.
ATTRIBUTION_DOCS = (
    "qa-plan.md",
    "docs/validation.md",
    "docs/开发进度.md",
    "docs/agents/p0-media-canary.md",
)

#: Words that mark a line as being about the user-stop chain.
_STOP_WORDS = ("停止生成", "用户停止", "取消/超时", "停止按钮", "取消链路")

#: Words a document may only use for the stop chain next to a ticket number.
_ACCEPTANCE_WORDS = ("PASS", "已验收", "已通过", "已实测", "已上线")


def _mask(value: Any) -> Any:
    """Replace what the gateway mints per run, keep everything it decides."""
    if isinstance(value, str):
        masked = _JOB_ID.sub(_MASKED_JOB_ID, value)
        masked = _CONVERSATION_ID.sub(_MASKED_CONVERSATION_ID, masked)
        return _TIMESTAMP.sub(_MASKED_TIMESTAMP, masked)
    if isinstance(value, list):
        return [_mask(item) for item in value]
    if isinstance(value, dict):
        return {key: _mask(item) for key, item in value.items()}
    return value


def _capture_samples(tmp_path: Path, monkeypatch) -> dict[str, dict[str, Any]]:
    """Drive the real stack through one stop, keeping every response the handoff shows.

    The model turn is held inside the (patched) model call while the stop lands,
    which is the state a stop request actually arrives in: the worker is running,
    the conversation slot is taken, and nothing has been written yet.
    """
    from test_assistant_api import _gates, _headers, _real_runtime_client, _Turn

    turn = _Turn()
    gates = _gates()
    gates["before_turn"].set()  # let the turn start as soon as the model runs
    client, runtime = _real_runtime_client(tmp_path, monkeypatch, gates=gates, turn=turn)
    try:
        conversation = client.post(
            "/v1/conversations",
            json={"agent_version_key": "agt_abcdef1234567890#v1"},
            headers=_headers(),
        ).json()
        cid = conversation["conversation_id"]

        asked = client.post(
            "/v1/assistant/questions",
            json={"question": STOP_QUESTION, "conversation_id": cid},
            headers=_headers(),
        )
        qa_id = asked.json()["qa_id"]
        assert gates["turn_started"].wait(10), "the worker never reached the model turn"

        samples = {
            "ask-202": {"status": asked.status_code, "body": asked.json()},
            "conversation-busy": {
                "status": 200,
                "body": client.get(f"/v1/conversations/{cid}", headers=_headers()).json(),
            },
        }

        stopped = client.post(f"/v1/assistant/questions/{qa_id}/cancel", headers=_headers())
        samples["cancel-200"] = {"status": stopped.status_code, "body": stopped.json()}

        polled = client.get(f"/v1/assistant/questions/{qa_id}", headers=_headers())
        samples["poll-cancelled"] = {"status": polled.status_code, "body": polled.json()}

        # The unlock event the frontend reads is the job's terminal state: the
        # same conversation, one boolean flipped.
        freed = client.get(f"/v1/conversations/{cid}", headers=_headers())
        samples["conversation-free"] = {"status": freed.status_code, "body": freed.json()}

        # A stop the gateway cannot act on is not a failure of the request.
        missing = client.post(f"/v1/assistant/questions/{UNKNOWN_JOB_ID}/cancel", headers=_headers())
        samples["cancel-404"] = {"status": missing.status_code, "body": missing.json()}

        # The synchronous branch, shown beside the waiting one so a client can
        # tell them apart before it renders anything.
        faq = client.post("/v1/assistant/questions", json={"question": FAQ_QUESTION}, headers=_headers())
        samples["faq-200"] = {"status": faq.status_code, "body": faq.json()}

        assert turn.interrupted, "the stop request never reached the live turn"
        return {name: {"status": s["status"], "body": _mask(s["body"])} for name, s in samples.items()}
    finally:
        gates["release"].set()
        runtime.shutdown()
        client.close()


def _documented_samples() -> dict[str, dict[str, Any]]:
    """Every ``contract-sample`` block in the handoff, keyed by its name."""
    lines = HANDOFF.read_text(encoding="utf-8").splitlines()
    samples: dict[str, dict[str, Any]] = {}
    pending: tuple[str, int] | None = None
    fence_open = False
    body: list[str] = []
    for line in lines:
        if pending is None:
            found = _SAMPLE_MARKER.search(line)
            if found:
                pending = (found.group(1), int(found.group(2)))
            continue
        if not fence_open:
            # The marker only counts when the JSON fence follows it directly.
            fence_open = line.startswith("```json")
            continue
        if line.startswith("```"):
            samples[pending[0]] = {"status": pending[1], "body": json.loads("\n".join(body))}
            pending, fence_open, body = None, False, []
            continue
        body.append(line)
    return samples


def test_the_handoff_shows_what_the_gateway_answers(tmp_path: Path, monkeypatch) -> None:
    captured = _capture_samples(tmp_path, monkeypatch)
    documented = _documented_samples()

    assert documented, f"no contract-sample blocks found in {HANDOFF.relative_to(PROJECT_ROOT)}"
    assert set(documented) == set(captured), (
        "the handoff documents samples the gateway did not produce (or omits ones it did)"
    )
    for name in sorted(captured):
        assert documented[name] == captured[name], (
            f"sample {name!r} in the handoff is not the response the gateway returns"
        )


def _blocks(text: str) -> list[tuple[int, str]]:
    """``(line number, text)`` per claim: a wrapped paragraph or one table row.

    These documents wrap their sentences, so a line-based check would either miss
    a claim or blame the wrong one. A table row is its own unit because each row
    carries its own evidence.
    """
    units: list[tuple[int, str]] = []
    buffer: list[tuple[int, str]] = []

    def flush() -> None:
        if buffer:
            units.append((buffer[0][0], "\n".join(text for _, text in buffer)))
            buffer.clear()

    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("|"):
            flush()
            units.append((number, line))
            continue
        if not line.strip():
            flush()
            continue
        buffer.append((number, line))
    flush()
    return units


def test_no_document_still_blames_173_for_the_stop_link() -> None:
    """A stop/cancel line that names #173 must also name the PRD that built it.

    #173 is closed and never implemented the link, so "it belongs to #173" is the
    sentence that let a reviewer sign the row off against an unrelated case.
    """
    for name in ATTRIBUTION_DOCS:
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        for number, block in _blocks(text):
            if "#173" not in block or not any(word in block for word in _STOP_WORDS):
                continue
            assert "#346" in block, (
                f"{name}:{number} attributes the stop-generation link to #173; it is delivered by PRD #346"
            )


def test_no_document_claims_the_stop_link_was_already_accepted() -> None:
    """Nothing may report the user-stop link as accepted without an owner.

    The claim is only allowed next to a ticket number (#346) or a real test file,
    which is what an acceptance claim has to rest on.
    """
    for name in ATTRIBUTION_DOCS:
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        for number, block in _blocks(text):
            if not any(word in block for word in _STOP_WORDS):
                continue
            if not any(word in block for word in _ACCEPTANCE_WORDS):
                continue
            assert "#346" in block or "tests/" in block, (
                f"{name}:{number} reports the stop-generation link as accepted "
                "without pointing at the PRD or a test that proves it"
            )


#: Every route the app registers whose path promises a cancellation.
_CANCEL_ROUTE = re.compile(r'@app\.(?:get|post|put|delete|patch)\(\s*"([^"]*cancel[^"]*)"')

#: The one cancel route that exists. Diagnoses deliberately have none (PRD #346
#: Out of Scope), and the handoff says so in prose.
_ONLY_CANCEL_ROUTE = "/v1/assistant/questions/{qa_id}/cancel"


def test_the_handoff_warns_that_a_diagnosis_cannot_be_stopped() -> None:
    """Diagnoses lock the input box without offering a stop — and must say so.

    The handoff splits "who waits" and "who can be cancelled" across different
    tables, so a reader wiring the waiting-state table alone draws a stop button
    for diagnoses that can only ever answer 404. The warning is load-bearing.
    """
    text = HANDOFF.read_text(encoding="utf-8")
    assert "没有取消路由" in text, (
        "the handoff no longer states that the diagnosis path has no cancel route; "
        "a frontend wiring the waiting-state table will draw a stop button that cannot work"
    )
    assert "刻意保留" in text, (
        "the handoff no longer says the diagnosis asymmetry is deliberate, "
        "so it reads as an oversight and invites a workaround"
    )


def test_only_the_assistant_question_route_can_be_cancelled() -> None:
    """The doc's "diagnoses cannot be stopped" claim is checked against the app.

    If a diagnosis cancel route is ever added, this fails and points at the
    handoff, which would otherwise keep telling two teams to render a stop-less
    waiting state for a flow that had since gained a stop.
    """
    source = (PROJECT_ROOT / "src" / "aiops_diagnostics" / "gateway_api.py").read_text(encoding="utf-8")
    routes = set(_CANCEL_ROUTE.findall(source))
    assert routes == {_ONLY_CANCEL_ROUTE}, (
        f"cancel routes changed to {sorted(routes)}; update {HANDOFF.name} §1.1, "
        "which states that only the assistant-question path can be cancelled"
    )
