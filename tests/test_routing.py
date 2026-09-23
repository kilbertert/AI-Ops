"""Routing from Jev's typed answers (#392, PRD #383).

Three properties carry this ticket, and each has a failure mode that already
happened once in this repository:

* the decision must reproduce the classifier contract exactly, because six
  consumers compare those strings with `==`;
* losing the decision must cost the user nothing — a routing hint is an
  optimisation, and failing the request it was meant to improve is strictly
  worse than not having it;
* losing the decision must be *visible*. This component previously broke
  completely and silently, and nobody noticed for a long time.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from aiops_diagnostics.jev_decisions import (
    ChoiceAnswer,
    Decision,
    JevError,
    JevInvalidResponse,
    JevUnavailable,
    NoulAnswer,
)
from aiops_diagnostics.routing import (
    INTENTS,
    ROUTING_INVALID,
    ROUTING_UNAVAILABLE,
    RoutingThresholds,
    classify_with_jev,
    decide,
    thresholds_from,
)


class _Client:
    """A Jev client that returns a scripted decision, or raises a scripted error."""

    def __init__(self, *, choice: str = "casual", confidence: float = 1.0, risk: float = 0.02, error=None):
        self._choice, self._confidence, self._risk, self._error = choice, confidence, risk, error
        self.seen: dict[str, Any] | None = None

    def decide(self, state: str, questions: Any) -> Decision:
        self.seen = {"state": state, "questions": questions}
        if self._error is not None:
            raise self._error
        return Decision(
            model="typesafe/jev",
            answers={
                "intent": ChoiceAnswer(choice=self._choice, confidence=self._confidence, probabilities={}),
                "risk": NoulAnswer(noul=self._risk),
            },
        )


# --- The decision contract ---


def test_the_six_intents_are_byte_identical_to_the_models() -> None:
    """Downstream compares these with `==` and passes them to `_promo_route`."""
    assert INTENTS == (
        "knowledge",
        "casual",
        "order_issue",
        "report_fault",
        "case_exploration",
        "solution_discovery",
    )


@pytest.mark.parametrize("intent", INTENTS)
def test_every_intent_survives_the_round_trip(intent: str) -> None:
    decision = decide("q", _Client(choice=intent), thresholds=RoutingThresholds())
    assert decision.intent == intent
    assert decision.as_dict()["intent"] == intent


def test_the_question_is_sent_verbatim_as_the_state() -> None:
    client = _Client()
    decide("你好，你好，你好。", client, thresholds=RoutingThresholds())
    assert client.seen is not None
    assert client.seen["state"] == "你好，你好，你好。"


# --- Threshold mapping ---


def test_confidence_maps_to_the_three_buckets() -> None:
    thresholds = RoutingThresholds(risk_at_least=0.5, confidence_at_least=0.8)
    assert thresholds.confidence_bucket(1.0) == "high"
    assert thresholds.confidence_bucket(0.8) == "high"  # boundary belongs to the confident side
    assert thresholds.confidence_bucket(0.79) == "medium"
    assert thresholds.confidence_bucket(0.4) == "medium"
    assert thresholds.confidence_bucket(0.39) == "low"


def test_risk_maps_to_the_two_buckets() -> None:
    thresholds = RoutingThresholds(risk_at_least=0.5)
    assert decide("q", _Client(risk=0.9), thresholds=thresholds).risk == "high"
    assert decide("q", _Client(risk=0.5), thresholds=thresholds).risk == "high"
    assert decide("q", _Client(risk=0.49), thresholds=thresholds).risk == "low"


def test_the_asymmetric_rule_still_holds_on_continuous_values() -> None:
    """`risk high AND confidence not high` → ask. The #346 rule, re-expressed.

    This is the property that must survive the cutover: a high-risk question the
    service is unsure about still has to reach the clarification branch, and one
    it is sure about still must not be needlessly interrupted.
    """
    thresholds = RoutingThresholds(risk_at_least=0.5, confidence_at_least=0.8)

    risky_unsure = decide("q", _Client(risk=0.92, confidence=0.57), thresholds=thresholds)
    assert risky_unsure.risk == "high" and risky_unsure.confidence != "high"

    risky_sure = decide("q", _Client(risk=0.88, confidence=0.98), thresholds=thresholds)
    assert risky_sure.risk == "high" and risky_sure.confidence == "high"


def test_changing_the_threshold_changes_the_outcome() -> None:
    """The threshold is a setting; the test proves it actually moves the line."""
    client = _Client(risk=0.6, confidence=0.7)
    strict = decide("q", client, thresholds=RoutingThresholds(risk_at_least=0.5, confidence_at_least=0.8))
    loose = decide("q", client, thresholds=RoutingThresholds(risk_at_least=0.5, confidence_at_least=0.6))
    assert (strict.risk, strict.confidence) == ("high", "medium")
    assert (loose.risk, loose.confidence) == ("high", "high")


def test_thresholds_come_from_the_environment_with_the_calibrated_defaults() -> None:
    default = thresholds_from({})
    assert (default.risk_at_least, default.confidence_at_least) == (0.5, 0.8)
    configured = thresholds_from(
        {
            "AIOPS_GATEWAY_ROUTING_RISK_AT_LEAST": "0.6",
            "AIOPS_GATEWAY_ROUTING_CONFIDENCE_AT_LEAST": "0.9",
        }
    )
    assert (configured.risk_at_least, configured.confidence_at_least) == (0.6, 0.9)


@pytest.mark.parametrize("bad", ["1.5", "-0.1"])
def test_an_out_of_range_threshold_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        thresholds_from({"AIOPS_GATEWAY_ROUTING_RISK_AT_LEAST": bad})


def test_a_non_numeric_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be a number"):
        thresholds_from({"AIOPS_GATEWAY_ROUTING_CONFIDENCE_AT_LEAST": "high"})


# --- Fallback: an unobtainable decision is not a failure ---


@pytest.mark.parametrize(
    "error",
    [
        JevUnavailable("service down"),
        JevInvalidResponse("garbage"),
        JevError("some other client failure"),
    ],
)
def test_an_unobtainable_decision_returns_none_instead_of_raising(error: Exception) -> None:
    """The caller then answers without a routing hint — never with an error."""
    assert classify_with_jev("q", _Client(error=error), thresholds=RoutingThresholds()) is None


def test_no_configured_client_means_no_decision(caplog: pytest.LogCaptureFixture) -> None:
    """An unconfigured deployment is not a failure and must not log one."""
    with caplog.at_level(logging.WARNING):
        assert classify_with_jev("q", None, thresholds=RoutingThresholds()) is None
    assert caplog.records == []


def test_a_successful_decision_is_returned_as_the_classifier_contract() -> None:
    result = classify_with_jev(
        "q", _Client(choice="knowledge", confidence=0.9, risk=0.03), thresholds=RoutingThresholds()
    )
    assert result == {"intent": "knowledge", "confidence": "high", "risk": "low"}


# --- Observability: this must not break silently again ---


def test_a_failure_is_logged_with_a_code(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="aiops.routing"):
        classify_with_jev("q", _Client(error=JevUnavailable("nope")), thresholds=RoutingThresholds())
    assert any(ROUTING_UNAVAILABLE in record.getMessage() for record in caplog.records), caplog.text


def test_the_question_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The log entry describes the failure; the user's words are not ours to keep."""
    secret = "我的订单号是2096164064667852801，扣费不对"
    with caplog.at_level(logging.WARNING, logger="aiops.routing"):
        classify_with_jev(secret, _Client(error=JevUnavailable("boom")), thresholds=RoutingThresholds())
    assert caplog.text
    assert secret not in caplog.text
    assert "2096164064667852801" not in caplog.text


def test_a_contract_violation_is_logged_under_its_own_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed answer and an outage have different remedies, so they differ."""
    with caplog.at_level(logging.WARNING, logger="aiops.routing"):
        classify_with_jev("q", _Client(choice="not-an-intent"), thresholds=RoutingThresholds())
    assert ROUTING_INVALID in caplog.text


def test_a_failure_is_counted_when_metrics_are_wired(tmp_path) -> None:
    """Counted, not just logged: an aggregate is what an operator alerts on."""
    from aiops_diagnostics.metrics_store import MetricsStore

    store = MetricsStore(tmp_path / "metrics.db")
    classify_with_jev(
        "q",
        _Client(error=JevUnavailable("down")),
        thresholds=RoutingThresholds(),
        metrics=store,
        tenant_id="T-1",
    )
    rows = store.list_runs("T-1", route_type="routing")
    assert rows, "a routing failure must leave one counted row"
    assert rows[0]["error_code"] == ROUTING_UNAVAILABLE


def test_metrics_failing_does_not_break_routing(tmp_path) -> None:
    """Recording is best-effort; a metrics fault must never reach the user."""

    class _BrokenMetrics:
        def record(self, **_kwargs: Any) -> str:
            raise RuntimeError("metrics exploded")

    assert (
        classify_with_jev(
            "q",
            _Client(error=JevUnavailable("down")),
            thresholds=RoutingThresholds(),
            metrics=_BrokenMetrics(),  # type: ignore[arg-type]
            tenant_id="T-1",
        )
        is None
    )


def test_an_unexpected_defect_is_not_hidden_behind_the_fallback() -> None:
    """Only this client's own errors mean 'no decision'.

    A programming error elsewhere must surface, not be swallowed — swallowing is
    exactly how the previous classifier stayed broken unnoticed.
    """

    class _Buggy:
        def decide(self, *_args: Any, **_kwargs: Any) -> Decision:
            raise KeyError("a real bug")

    with pytest.raises(KeyError):
        classify_with_jev("q", _Buggy(), thresholds=RoutingThresholds())  # type: ignore[arg-type]


def test_the_failure_log_is_bounded_and_carries_no_credentials(caplog: pytest.LogCaptureFixture) -> None:
    """The reason string is truncated and comes from our own client, not a payload."""
    long_error = JevUnavailable("x" * 500)
    with caplog.at_level(logging.WARNING, logger="aiops.routing"):
        classify_with_jev("q", _Client(error=long_error), thresholds=RoutingThresholds())
    for record in caplog.records:
        assert len(record.getMessage()) < 300, record.getMessage()
    # And a serialized payload embedded in the message would be a leak path.
    assert json.dumps({"x": 1}) not in caplog.text
