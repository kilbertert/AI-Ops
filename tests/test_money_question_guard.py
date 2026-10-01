"""One authority for "a money question needs an identifier first" (#406).

Two independent guards used to answer that question, and they disagreed on both
the trigger and the answer:

| guard | where | asks for | basis |
|---|---|---|---|
| `_HIGH_RISK_ORDER_CUES` | **before** the classifier | `order_no` | a hand-maintained regex |
| the model's risk label | **after** it | `context` | Jev's judgement |

So the same question could be asked twice, in two shapes, and which one a user
met depended on whether the regex happened to match. Measured over the
86-question corpus (run against the real model on 41, 2026-10-01):

* the keyword table fires on **3**;
* the model calls **5** high-risk;
* **2** are in both; **1** is only the table's (a German "was I overcharged"
  the model does not call high-risk); **3** are only the model's — `refund`
  alone, and two "check charging anomalies for order …" phrasings.

The table stays as the **fast path** (no model call, and the only one that
still answers when the model is down); what changes is that it is no longer a
second *authority*.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiops_diagnostics.gateway_api import _HIGH_RISK_ORDER_CUES
from aiops_diagnostics.routing import (
    MISSING_CONTEXT,
    MISSING_ORDER_NO,
    RoutingThresholds,
    money_question_context,
)

CORPUS = Path(__file__).parents[1] / "docs" / "jev-recalibration-corpus.json"
#: What the real model answered for this corpus on 41 (2026-10-01). Recorded
#: rather than recomputed: the classifier is a remote dependency, and a test
#: that needs it would be skipped exactly when it matters.
HIGH_RISK_BY_MODEL = {
    "我上个月的充电扣费感觉多扣了,帮我看看对不对",
    "帮我看看我的订单扣费对不对,感觉多扣了钱",
    "帮我检测（REDACTED）这个订单的充电异常",
    "Please check charging anomalies for order (REDACTED)",
    "refund",
}


def _key_for(question: str, *, model_high_risk: bool | None = None) -> str | None:
    """The merged guard's answer, with the model's verdict supplied."""
    decision = {"risk": "high"} if model_high_risk else {"risk": "low"}
    return money_question_context(
        question,
        decision,
        keyword_guard=lambda q: _HIGH_RISK_ORDER_CUES.search(q or "") is not None,
        thresholds=RoutingThresholds(),
    )


def test_the_keyword_table_is_the_fast_path() -> None:
    """A question it catches is answered without consulting the model.

    The table is not decoration: it costs no call, and it is the only half that
    still fires when the model is unavailable.
    """
    question = "帮我看看我的订单扣费对不对,感觉多扣了钱"
    assert _HIGH_RISK_ORDER_CUES.search(question)
    assert _key_for(question, model_high_risk=False) == MISSING_ORDER_NO


def test_the_model_covers_what_the_table_misses() -> None:
    """The three phrasings the table misses still get an identifier asked for.

    `refund` is the recorded example: a bare topic noun the table must NOT treat
    as a dispute (the FAQ carries entries on exactly that subject), and which the
    model still recognises as money-adjacent.
    """
    for question in (
        "refund",
        "Please check charging anomalies for order 2096164064667852801",
    ):
        assert not _HIGH_RISK_ORDER_CUES.search(question), question
        assert _key_for(question, model_high_risk=True) == MISSING_CONTEXT, question


def test_one_question_gets_one_answer() -> None:
    """Never two identifiers for the same question.

    This is what "two authorities" cost: a question the table caught asked for
    `order_no`, and the same question re-asked asked for `context`.
    """
    # The question is what the USER asked, not what the model answered: the two
    # guards used to produce different identifiers for the same question
    # depending on which one ran first. Now the answer is fixed by the merged
    # rule, and the model's own verdict only ever *adds* questions to it.
    for question in (
        "我上个月的充电扣费感觉多扣了,帮我看看对不对",
        "帮我看看我的订单扣费对不对,感觉多扣了钱",
    ):
        with_model = _key_for(question, model_high_risk=True)
        without_model = _key_for(question, model_high_risk=False)
        assert with_model == without_model == MISSING_ORDER_NO, (question, with_model, without_model)
    # A question only the model catches has exactly one answer too — and it is
    # not the table's, because the table did not fire.
    assert _key_for("refund", model_high_risk=True) == MISSING_CONTEXT
    assert _key_for("refund", model_high_risk=False) is None


def test_order_no_wins_when_both_would_fire() -> None:
    """The more actionable identifier, when the table and the model agree.

    `order_no` names something the user can go and find; `context` is the
    fallback for a question only the model recognises.
    """
    question = "帮我看看我的订单扣费对不对,感觉多扣了钱"
    assert _key_for(question, model_high_risk=True) == MISSING_ORDER_NO


def test_neither_guard_firing_asks_for_nothing() -> None:
    assert _key_for("充电桩出现E01故障代码应该如何处理") is None


def test_the_corpus_outcome_is_recorded_per_question() -> None:
    """Every high-risk question in the corpus has a stated destination.

    The PRD asks for this list explicitly: with two overlapping guards, "which
    one caught it" was not answerable, and the coverage depended on a regex
    nobody could enumerate. This asserts the recorded split still matches the
    table's own behaviour — the model's half is a recorded measurement, not a
    live call.
    """
    questions = json.loads(CORPUS.read_text(encoding="utf-8"))["questions"]
    assert len(questions) == 86
    table_only, both, model_only = [], [], []
    for question in questions:
        by_table = bool(_HIGH_RISK_ORDER_CUES.search(question))
        by_model = question in HIGH_RISK_BY_MODEL
        if by_table and by_model:
            both.append(question)
        elif by_table:
            table_only.append(question)
        elif by_model:
            model_only.append(question)
    assert len(both) == 2, both
    assert len(table_only) == 1, table_only
    assert len(model_only) == 3, model_only
    # The union is what actually reaches the user: 6 of 86 in this corpus.
    assert len(both) + len(table_only) + len(model_only) == 6
