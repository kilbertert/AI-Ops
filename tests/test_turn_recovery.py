"""A streamed turn whose head the transport dropped is still the model's turn.

Captured from the live provider on 41 (2026-09-22): the first ``output_text``
delta of the answer body went missing in 5 of 6 runs of one fixed prompt, always
as a prefix (2-18 characters) and never the tail. The provider was passing the
body through a transparent relay, and the same loss reproduced when the relay
was bypassed — so the defect is upstream of this repository, and the repair has
to live here or the customer sees an error card instead of their answer.

The bodies in these tests are the real captured traffic, not invented shapes:
the production case is the literal string the gateway received with the head
already gone.
"""

from __future__ import annotations

import json

import pytest

from aiops_diagnostics.turn_recovery import parse_turn, repair_truncated_turn_head

#: The body the gateway actually received for qa_e42533c3fd234a4a958cd59d559a643e
#: (41, 2026-09-22T10:18:41Z). ``{"kind":`` is what went missing.
PRODUCTION_HEAD_LOSS = (
    ' "answer", "retrieval_status": "not_attempted", "reminder": true, '
    '"blocks": [{"type": "text", "text": "您好！抱歉听到您的车辆出现问题。"}]}'
)

#: The intact body that same turn should have been.
PRODUCTION_INTACT = (
    '{"kind": "answer", "retrieval_status": "not_attempted", "reminder": true, '
    '"blocks": [{"type": "text", "text": "您好！抱歉听到您的车辆出现问题。"}]}'
)


def test_the_production_body_is_repaired_to_the_turn_the_model_sent() -> None:
    """The real captured body parses to exactly the object the intact one does."""
    repaired = parse_turn(PRODUCTION_HEAD_LOSS)
    assert repaired is not None, "the production head-loss body must be repaired"
    assert repaired == json.loads(PRODUCTION_INTACT)


@pytest.mark.parametrize("cut", range(1, 19))
def test_every_prefix_loss_the_transport_was_observed_to_make(cut: int) -> None:
    """Loss of any length the endpoint produced is recovered, mid-token included.

    A cut is not aligned to JSON tokens: losing ``{"kind`` ends inside a key
    name, so a repair built only from whole tokens recovers nothing. This is the
    regression that turned the first version of the module into a no-op.
    """
    repaired = parse_turn(PRODUCTION_INTACT[cut:])
    assert repaired == json.loads(PRODUCTION_INTACT), f"cut of {cut} chars was not repaired"


def test_a_tool_request_turn_is_repaired_too() -> None:
    """Both turn kinds share the opening, so both need the fallback."""
    intact = '{"kind":"tool_requests","tool_requests":[{"tool":"knowledge_search","query":"充电","reason":"needs kb"}]}'
    repaired = parse_turn(intact[6:])
    assert repaired == json.loads(intact)


def test_an_intact_body_is_returned_unchanged() -> None:
    """The fallback must not perturb the ordinary case."""
    assert parse_turn(PRODUCTION_INTACT) == json.loads(PRODUCTION_INTACT)


def test_a_fenced_body_still_parses() -> None:
    """Fence tolerance predates this change and must survive it."""
    fenced = "```json\n" + PRODUCTION_INTACT + "\n```"
    assert parse_turn(fenced) == json.loads(PRODUCTION_INTACT)


def test_an_outer_span_is_still_extracted() -> None:
    """Prose around the object is not head loss; the span rule still applies."""
    assert parse_turn("Sure: " + PRODUCTION_INTACT + " done") == json.loads(PRODUCTION_INTACT)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "您好！很高兴为您服务。",
        "zzz not json at all",
        "[1, 2, 3]",
    ],
)
def test_a_body_that_is_not_a_turn_is_not_invented(body: str) -> None:
    """The repair never turns junk into an answer.

    It only re-attaches openings the turn contract permits, so prose or an
    array stays unparsed rather than being coerced into a turn. An intact JSON
    object that is simply not a turn is a different matter: parsing it is this
    function's job, and rejecting it belongs to the caller's shape check.
    """
    assert parse_turn(body) is None


def test_the_repair_does_not_re_attach_a_wrong_opening() -> None:
    """A reconstruction must satisfy the contract, not merely be valid JSON.

    ``kind`` is the contract's own discriminator: a body that would need an
    opening the schema does not allow is refused rather than repaired.
    """
    # A body whose true opening is neither of the two permitted ones.
    assert repair_truncated_turn_head('":"unknown","blocks":[]}') is None


def test_a_failed_qa_job_reports_copy_a_customer_can_act_on() -> None:
    """The poll response must not carry the harness's internal reason.

    The job record keeps the internal reason on purpose: a contract violation is
    a defect an engineer must see, and replacing it there once hid a real bug
    behind "service temporarily unavailable" (2026-09-17). The response is read
    by a customer, so it gets localized copy instead — this is the boundary that
    tells the two audiences apart.
    """
    from aiops_diagnostics.gateway_api import _assistant_question_response

    record = {
        "qa_id": "qa_" + "0" * 32,
        "question": "你好",
        "status": "failed",
        "error_code": "QA_FAILED",
        # The internal reason, exactly as the record holds it.
        "error_message": "customer QA turn returned invalid JSON",
    }
    body = _assistant_question_response(record, "zh")
    assert body["error"]["code"] == "QA_FAILED"
    assert "invalid JSON" not in body["error"]["message"], (
        "the internal harness reason must not reach a customer-facing response"
    )
    assert body["error"]["message"] != record["error_message"]

    en = _assistant_question_response(record, "en")
    assert "invalid JSON" not in en["error"]["message"]
    assert en["error"]["message"] != body["error"]["message"], (
        "the copy is localized from the request's language, not fixed to Chinese"
    )
