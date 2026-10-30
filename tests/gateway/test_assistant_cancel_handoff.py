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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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
#:
#: Since 2026-10-23 each milestone writes a NEW file under ``docs/validation/`` and
#: ``docs/开发进度/`` instead of prepending to one document — parallel agents that
#: prepend to the same file always conflict, and GitHub does not dispatch CI at all
#: for a pull request that conflicts. The two single files stay in the list because
#: they are the read-only archive, and an attribution that was wrong in the archive
#: stays wrong until someone corrects it. The directories are scanned whole, so the
#: check does not weaken as entries accumulate: a new file is checked the moment it
#: is committed, without anyone remembering to add its name here.
ATTRIBUTION_DOCS = (
    "qa-plan.md",
    "docs/validation.md",
    "docs/开发进度.md",
    "docs/agents/p0-media-canary.md",
)
ATTRIBUTION_DIRS = (
    PROJECT_ROOT / "docs" / "validation",
    PROJECT_ROOT / "docs" / "开发进度",
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


def _attribution_documents() -> list[tuple[str, str]]:
    """``(repository-relative name, text)`` for every document the checks cover.

    The four named files plus every entry file under the two evidence directories.
    Sorted so a failure names the same document on every run.
    """
    documents = [(name, (PROJECT_ROOT / name).read_text(encoding="utf-8")) for name in ATTRIBUTION_DOCS]
    for directory in ATTRIBUTION_DIRS:
        for path in sorted(directory.rglob("*.md")):
            documents.append((path.relative_to(PROJECT_ROOT).as_posix(), path.read_text(encoding="utf-8")))
    return documents


def test_every_evidence_entry_file_is_covered() -> None:
    """The scan must actually reach the entry files, or the rewrite silently checks nothing.

    A helper that returns only the four archive documents would keep both tests
    green while every new milestone record — the place the attribution drifted
    last time — went unread.
    """
    names = [name for name, _ in _attribution_documents()]
    assert any(name.startswith("docs/validation/") for name in names), names
    assert any(name.startswith("docs/开发进度/") for name in names), names


def test_no_document_still_blames_173_for_the_stop_link() -> None:
    """A stop/cancel line that names #173 must also name the PRD that built it.

    #173 is closed and never implemented the link, so "it belongs to #173" is the
    sentence that let a reviewer sign the row off against an unrelated case.
    """
    for name, text in _attribution_documents():
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
    for name, text in _attribution_documents():
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

#: The cancel routes that exist. Until #499 only the qa line had one and the
#: handoff said so in prose; the diagnosis line gained its own, so this set is
#: now the full expected set — a route added or removed without the handoff
#: following is what this pins.
_CANCEL_ROUTES = {
    "/v1/assistant/questions/{qa_id}/cancel",
    "/v1/dify/trial/{trial_id}/cancel",
}


def test_the_cancel_routes_match_what_the_handoff_documents() -> None:
    """The set of cancel routes is checked against the app, not assumed.

    A route added or removed without the handoff following is the failure this
    catches — it is what caught #499's new route against the handoff's old
    "diagnoses cannot be stopped" text.
    """
    source = (PROJECT_ROOT / "src" / "aiops_diagnostics" / "gateway" / "gateway_api.py").read_text(
        encoding="utf-8"
    )
    routes = set(_CANCEL_ROUTE.findall(source))
    assert routes == _CANCEL_ROUTES, (
        f"cancel routes changed to {sorted(routes)}; update {HANDOFF.name}, "
        "which documents which paths can be cancelled"
    )
    text = HANDOFF.read_text(encoding="utf-8")
    for route in sorted(routes):
        assert route in text, f"{route} is registered but not documented in {HANDOFF.name}"
