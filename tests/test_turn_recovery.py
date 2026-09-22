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

from aiops_diagnostics.turn_recovery import (
    CLASSIFIER_TURN_OPENINGS,
    DIAGNOSIS_TURN_OPENINGS,
    QA_TURN_OPENINGS,
    ZERO_ORDER_OPENINGS,
    _looks_like_classifier_turn,
    _looks_like_diagnosis_turn,
    _looks_like_qa_turn,
    _looks_like_zero_order_turn,
    parse_turn,
    repair_truncated_turn_head,
)

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
    intact = (
        '{"kind":"tool_requests","tool_requests":'
        '[{"tool":"knowledge_search","query":"充电","reason":"needs kb"}]}'
    )
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
    assert (
        repair_truncated_turn_head(
            '":"unknown","blocks":[]}', openings=QA_TURN_OPENINGS, is_valid=_looks_like_qa_turn
        )
        is None
    )


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


@pytest.mark.parametrize("language", ["zh", "en", "de", "fr", "es", "pt"])
def test_the_failure_copy_resolves_in_every_supported_language(language: str) -> None:
    """Every supported language has copy, so the fallback never has to fire.

    The fallback branch is what broke first: it referenced a constant that was
    not imported, so an unknown language name raised NameError at the moment a
    customer was already seeing a failure. Pinning all six languages keeps the
    fallback genuinely unreachable for real requests.
    """
    from aiops_diagnostics.gateway_api import _qa_user_message
    from aiops_diagnostics.i18n import QA_FALLBACK_MESSAGES

    message = _qa_user_message(language, "KB_UNAVAILABLE")
    assert message == QA_FALLBACK_MESSAGES[language]["unavailable"]
    assert message.strip()


def test_an_unknown_language_still_yields_copy() -> None:
    """A language tag the pack does not carry must not turn into a crash."""
    from aiops_diagnostics.gateway_api import _qa_user_message

    message = _qa_user_message("xx-not-a-language", "QA_FAILED")
    assert message.strip()


# --- The recovery must cover every turn contract, not just the QA one. ---
# An earlier revision knew only the QA opening, so three of the four schemas
# stayed unrecoverable while the fix looked complete.


def test_the_diagnosis_turn_is_recovered() -> None:
    """The diagnosis contract opens `{"kind":"diagnosis"`, not the QA opening."""
    intact = '{"kind":"diagnosis","diagnosis":{"status":"diagnosed","summary":"ok"},"tool_requests":[]}'
    repaired = parse_turn(intact[6:], openings=DIAGNOSIS_TURN_OPENINGS, is_valid=_looks_like_diagnosis_turn)
    assert repaired == json.loads(intact)


def test_a_diagnosis_tool_request_is_recovered() -> None:
    intact = '{"kind":"tool_requests","tool_requests":[{"tool":"order_snapshot"}],"diagnosis":null}'
    repaired = parse_turn(intact[9:], openings=DIAGNOSIS_TURN_OPENINGS, is_valid=_looks_like_diagnosis_turn)
    assert repaired == json.loads(intact)


def test_the_classifier_turn_is_recovered() -> None:
    """The classifier opens `{"intent":` — it has no `kind` at all."""
    intact = '{"intent":"casual","confidence":"high","risk":"low","answer":"你好"}'
    repaired = parse_turn(intact[1:], openings=CLASSIFIER_TURN_OPENINGS, is_valid=_looks_like_classifier_turn)
    assert repaired == json.loads(intact)


def test_the_zero_order_turn_is_recovered() -> None:
    """`run_zero_order_answer` expects `{"text", "reminder"}` with no `kind`."""
    intact = '{"text":"你好，我可以帮你解答充电问题。","reminder":true}'
    repaired = parse_turn(intact[1:], openings=ZERO_ORDER_OPENINGS, is_valid=_looks_like_zero_order_turn)
    assert repaired == json.loads(intact)


def test_a_wrong_contract_does_not_accept_another_schemas_body() -> None:
    """Recovery is schema-aware: the QA openings must not swallow a zero-order body."""
    zero_order_body = '"text":"hi","reminder":true}'
    assert parse_turn(zero_order_body, openings=QA_TURN_OPENINGS, is_valid=_looks_like_qa_turn) is None


# --- The failure copy must name the cause that actually failed. ---


def test_a_provider_failure_does_not_blame_the_knowledge_base() -> None:
    """`QA_FAILED` covers provider errors and contract defects, not retrieval.

    Telling a customer the library is down when their model provider rejected
    the request names the wrong broken thing and points at the wrong remedy.
    """
    from aiops_diagnostics.gateway_api import _qa_user_message
    from aiops_diagnostics.i18n import QA_FALLBACK_MESSAGES

    generic = _qa_user_message("zh", "QA_FAILED")
    assert generic == QA_FALLBACK_MESSAGES["zh"]["generation_failed"]
    assert generic != QA_FALLBACK_MESSAGES["zh"]["unavailable"]


def test_only_a_verified_retrieval_failure_blames_the_knowledge_base() -> None:
    from aiops_diagnostics.gateway_api import _qa_user_message
    from aiops_diagnostics.i18n import QA_FALLBACK_MESSAGES

    kb = _qa_user_message("en", "KB_UNAVAILABLE")
    assert kb == QA_FALLBACK_MESSAGES["en"]["unavailable"]


@pytest.mark.parametrize("language", ["zh", "en", "de", "fr", "es", "pt"])
@pytest.mark.parametrize("code", ["QA_FAILED", "KB_UNAVAILABLE", None])
def test_every_failure_path_yields_copy_in_every_language(language: str, code: str | None) -> None:
    """No combination may fall through to a missing key or an empty string."""
    from aiops_diagnostics.gateway_api import _qa_user_message

    assert _qa_user_message(language, code).strip()
