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
    should_ask_for_context,
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


# --- Review findings: each of these was a real defect before the fix ---


def test_an_unconfigured_deployment_still_classifies(tmp_path) -> None:
    """No Jev settings must not mean no classification.

    Found in review: the first version returned "no decision" whenever Jev was
    unconfigured. Every deployment without the new credentials would then have
    lost casual handling, the promotional intents and the high-risk
    clarification rule — a regression wearing the costume of a default.
    """
    from aiops_diagnostics.gateway_runtime import GatewayRuntime

    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.jev_client = None
    runtime.routing_thresholds = RoutingThresholds()
    runtime.metrics_store = None
    called: dict[str, Any] = {}

    def _model(question: str, *, language: str = "zh") -> dict[str, Any]:
        called["question"] = question
        return {"intent": "casual", "confidence": "high", "risk": "low"}

    runtime.classify_lightweight_model = _model  # type: ignore[method-assign]
    result = runtime.classify_lightweight("你好")
    assert called.get("question") == "你好", "the previous classifier must still be reached"
    assert result == {"intent": "casual", "confidence": "high", "risk": "low"}


def test_a_nonpositive_timeout_is_rejected() -> None:
    """A bad timeout otherwise fails every call as a transport error."""
    from aiops_diagnostics.jev_decisions import JevSettings

    for bad in (-1, 0, 0.0, 500):
        with pytest.raises(ValueError, match="timeout"):
            JevSettings(base_url="https://x.example", api_key="k", timeout=bad).validate()


def test_a_nonpositive_gateway_timeout_is_rejected() -> None:
    from pathlib import Path

    from aiops_diagnostics.gateway_config import GatewayServerSettings

    settings = GatewayServerSettings(
        data_home=Path("/tmp"),
        database_file=Path("/tmp/gateway.db"),
        jev_timeout_seconds=0,
    )
    with pytest.raises(ValueError, match="JEV_TIMEOUT"):
        settings.validate()


def test_routing_health_does_not_inflate_user_run_totals(tmp_path) -> None:
    """One question that lost its hint and then answered normally is one run.

    Found in review: the routing row entered the summary totals as a failed run,
    so a successfully answered request counted twice — once failed, once
    completed. The row stays visible in `by_route`; it leaves the totals.
    """
    from aiops_diagnostics.metrics_store import MetricsStore

    store = MetricsStore(tmp_path / "metrics.db")
    store.record(tenant_id="T-1", route_type="qa", outcome="completed")
    store.record(tenant_id="T-1", route_type="routing", outcome="failed", error_code="ROUTING_UNAVAILABLE")

    summary = store.summary("T-1")
    assert summary["totals"]["runs"] == 1, summary["totals"]
    assert summary["totals"]["failed"] == 0, summary["totals"]
    # ...but the health signal is not hidden.
    routes = {row["route_type"] for row in summary["by_route"]}
    assert "routing" in routes, summary["by_route"]


# --- Plan A: high risk asks regardless of confidence (#401) ---
# The rule this replaces fired zero times in 86 real questions, because Jev
# recognises money questions and is confident about recognising them. A rule
# that is always correct and never fires is not a rule.


def test_high_risk_asks_even_when_the_decision_is_confident() -> None:
    """The whole point of plan A: confidence no longer suppresses the question."""
    decided = {"intent": "order_issue", "risk": "high", "confidence": "high"}
    assert should_ask_for_context(decided) is True


def test_the_real_traffic_case_now_asks() -> None:
    """The measured billing complaint, with the values Jev actually returned.

    `帮我看看我的订单扣费对不对,感觉多扣了钱` came back risk=high (0.85) and
    confidence=high (1.00). Under the old rule it did not ask; that is the case
    this change exists for.
    """
    assert should_ask_for_context({"intent": "order_issue", "risk": "high", "confidence": "high"}) is True


def test_low_risk_never_asks() -> None:
    """Low-risk questions proceed as before — the rule stays asymmetric."""
    assert should_ask_for_context({"intent": "casual", "risk": "low", "confidence": "low"}) is False
    assert should_ask_for_context({"intent": "knowledge", "risk": "low", "confidence": "high"}) is False


def test_no_decision_means_no_question() -> None:
    """An unobtainable decision must not turn into an interruption."""
    assert should_ask_for_context(None) is False
    assert should_ask_for_context({}) is False


def test_the_previous_form_is_still_reachable_for_comparison() -> None:
    """`risk_always_asks=False` restores the old rule, so the two can be compared.

    Kept as a switch rather than deleted logic: the change is a product decision
    about behaviour, and being able to put the previous behaviour back — on one
    host, without a code change — is what makes that decision cheap to revisit.
    """
    old = RoutingThresholds(risk_always_asks=False)
    assert should_ask_for_context({"risk": "high", "confidence": "high"}, thresholds=old) is False
    assert should_ask_for_context({"risk": "high", "confidence": "medium"}, thresholds=old) is True
    # ...and the new behaviour is the default.
    assert RoutingThresholds().risk_always_asks is True


def test_the_switch_is_configurable_by_environment() -> None:
    """Changing the behaviour must not require a code change."""
    assert thresholds_from({}).risk_always_asks is True
    assert thresholds_from({"AIOPS_GATEWAY_ROUTING_RISK_ALWAYS_ASKS": "false"}).risk_always_asks is False
    assert thresholds_from({"AIOPS_GATEWAY_ROUTING_RISK_ALWAYS_ASKS": "true"}).risk_always_asks is True
    with pytest.raises(ValueError, match="must be a boolean"):
        thresholds_from({"AIOPS_GATEWAY_ROUTING_RISK_ALWAYS_ASKS": "maybe"})


def test_every_routing_setting_is_reachable_from_the_environment(monkeypatch) -> None:
    """A setting nobody reads is not a setting.

    Found in review: `routing_risk_always_asks` was declared, documented in
    `.env.example`, and used as the documented rollback — but `from_env()` never
    read it, so `AIOPS_GATEWAY_ROUTING_RISK_ALWAYS_ASKS=false` did nothing. The
    rollback path was written down in three places and worked in none of them.

    This test drives the real reader rather than the dataclass default, so a
    future setting that is declared but not wired fails here.
    """
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    monkeypatch.setenv("AIOPS_GATEWAY_DATA_HOME", "/tmp/aiops-routing-probe")
    monkeypatch.setenv("AIOPS_GATEWAY_DATABASE_FILE", "/tmp/aiops-routing-probe/gateway.db")
    monkeypatch.setenv("AIOPS_GATEWAY_ROUTING_RISK_AT_LEAST", "0.6")
    monkeypatch.setenv("AIOPS_GATEWAY_ROUTING_CONFIDENCE_AT_LEAST", "0.9")
    monkeypatch.setenv("AIOPS_GATEWAY_ROUTING_RISK_ALWAYS_ASKS", "false")
    settings = GatewayServerSettings.from_env()
    assert settings.routing_risk_at_least == 0.6
    assert settings.routing_confidence_at_least == 0.9
    assert settings.routing_risk_always_asks is False, (
        "the documented rollback must actually reach the gateway settings"
    )
