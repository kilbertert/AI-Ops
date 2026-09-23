"""Routing decisions from Jev's typed answers (PRD #383, ticket #392).

The classifier used to get `intent` / `risk` / `confidence` by asking a model to
generate JSON. This module replaces that source with Jev, which returns typed
decisions — no generated text, so no parsing step to lose characters in.

Three things here are deliberate, and each is a requirement rather than a
preference:

**The six `intent` strings are byte-identical to the ones the model produced.**
Everything downstream compares them with `==` and passes them into
`_promo_route(forced_intent=...)`. Jev returning the labels it was given makes
that free; a mapping layer here would be a second place to keep in sync.

**A decision that cannot be obtained is not a failure.** It is the absence of a
decision. The caller falls back to the previous behaviour and answers the user
anyway. A routing hint is an optimisation; letting it fail the request it was
meant to improve would be a strictly worse product.

**A routing failure must be visible.** This component once broke completely and
silently — the exception was caught by a broad `except`, the message was
redacted, and nothing reached the log, so "never worked in production" stayed
unknown for a long time. Failures now carry a code and are counted and logged.
The user's text is never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.jev_decisions import (
    Choice,
    ChoiceAnswer,
    JevDecisionClient,
    JevError,
    JevInvalidResponse,
    JevUnavailable,
    Noul,
)
from aiops_diagnostics.metrics_store import MetricsStore

_LOGGER = logging.getLogger("aiops.routing")

#: The six intents, verbatim. These are the strings the model used and the
#: strings every consumer already branches on.
INTENTS: tuple[str, ...] = (
    "knowledge",
    "casual",
    "order_issue",
    "report_fault",
    "case_exploration",
    "solution_discovery",
)

#: What each label means, as Jev's `criteria`. This is the only place the
#: semantics of a label are written down for the decision service.
INTENT_CRITERIA: dict[str, str] = {
    "knowledge": "询问充电/新能源业务知识（政策、操作、产品用法）",
    "casual": "寒暄、闲聊或问候，不需要业务知识",
    "order_issue": "涉及某个具体订单的异常（金额、电量、故障）",
    "report_fault": "用户要上报一个故障或投诉",
    "case_exploration": "用户想看客户案例",
    "solution_discovery": "用户想看行业方案或解决方案",
}

_INTENT_INSTRUCTIONS = "用户这句话属于哪一类意图？只能选一个。"
_RISK_INSTRUCTIONS = "这句话是否涉及高风险、需要人工确认的扣费、资金或投诉问题？"

#: Failure codes. Stable strings, because they are counted and alerted on.
#: Two codes rather than one because the remedies differ: an unreachable service
#: is waited out, a contract violation means one of the two sides changed shape.
ROUTING_UNAVAILABLE = "ROUTING_UNAVAILABLE"
ROUTING_INVALID = "ROUTING_INVALID"


class RoutingContractError(JevError):
    """The service answered, but not with a decision this contract can use.

    Distinct from ``JevUnavailable`` on purpose: it is a defect rather than an
    outage, and the two must not be counted together.
    """


@dataclass(frozen=True, slots=True)
class RoutingThresholds:
    """Where Jev's continuous values become the three buckets the rule needs.

    Calibrated in #390 against real questions; the numbers are a starting point
    and are expected to be re-measured against real traffic. They are settings
    rather than constants so that re-measuring does not mean a code change.

    ``confidence_at_least`` is deliberately far from the values observed in
    calibration, because Jev's confidence drifts slightly run to run (0.71 once,
    0.67 the next): a threshold near an observed value makes the same question
    behave differently on different days.
    """

    risk_at_least: float = 0.5
    confidence_at_least: float = 0.8

    def validate(self) -> None:
        for name, value in (
            ("risk_at_least", self.risk_at_least),
            ("confidence_at_least", self.confidence_at_least),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")

    def confidence_bucket(self, confidence: float) -> str:
        """Map a probability onto the high/medium/low the rule speaks in.

        ``high`` means "at or above the threshold"; the shipped rule asks for
        context only when confidence is *not* high, so the boundary belongs to
        the confident side — the same direction as the original three-way label.
        """
        if confidence >= self.confidence_at_least:
            return "high"
        if confidence >= self.confidence_at_least / 2:
            return "medium"
        return "low"


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """What the classifier contract has always returned, from a typed source."""

    intent: str
    confidence: str
    risk: str
    raw_risk: float
    raw_confidence: float

    def as_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "confidence": self.confidence, "risk": self.risk}


def decide(
    question: str,
    client: JevDecisionClient,
    *,
    thresholds: RoutingThresholds,
) -> RoutingDecision:
    """Ask Jev for the routing decision. Raises ``JevError`` if it cannot answer."""
    decision = client.decide(
        question,
        {
            "intent": Choice(instructions=_INTENT_INSTRUCTIONS, criteria=INTENT_CRITERIA),
            "risk": Noul(instructions=_RISK_INSTRUCTIONS),
        },
    )
    intent_answer = decision.answers.get("intent")
    risk_answer = decision.answers.get("risk")
    if not isinstance(intent_answer, ChoiceAnswer) or risk_answer is None:
        raise RoutingContractError("routing decision is missing an answer")
    if intent_answer.choice not in INTENTS:
        # The client already checks the label came from the criteria we sent, so
        # this is a canary for the two lists drifting apart, not a live path.
        raise RoutingContractError(f"routing decision used an unknown intent: {intent_answer.choice}")
    raw_risk = float(getattr(risk_answer, "noul", 0.0))
    return RoutingDecision(
        intent=intent_answer.choice,
        confidence=thresholds.confidence_bucket(intent_answer.confidence),
        risk="high" if raw_risk >= thresholds.risk_at_least else "low",
        raw_risk=raw_risk,
        raw_confidence=intent_answer.confidence,
    )


def classify_with_jev(
    question: str,
    client: JevDecisionClient | None,
    *,
    thresholds: RoutingThresholds,
    metrics: MetricsStore | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """The classifier contract, sourced from Jev, or ``None`` if unavailable.

    ``None`` means "no decision was obtained" and the caller proceeds without
    one. That is the whole fallback: the routing hint is an optimisation, so
    losing it must cost the user nothing.

    Deliberately narrow: only this client'"'"'s own error vocabulary is converted.
    A ``KeyError`` from a bug elsewhere must surface as a bug — catching broadly
    is precisely how the previous classifier stayed broken without anyone
    knowing.

    Every failure is logged with a stable code and counted. The question text is
    not logged — the code is what an operator alerts on, and the text is the
    user's.
    """
    if client is None:
        return None
    try:
        decision = decide(question, client, thresholds=thresholds)
    except JevUnavailable as exc:
        _record_routing_failure(ROUTING_UNAVAILABLE, exc, metrics=metrics, tenant_id=tenant_id)
        return None
    except (RoutingContractError, JevInvalidResponse) as exc:
        _record_routing_failure(ROUTING_INVALID, exc, metrics=metrics, tenant_id=tenant_id)
        return None
    except JevError as exc:
        # A Jev error that is neither: still this client's own vocabulary, so it
        # is "no decision" rather than a crash — but it must not be counted as an
        # outage, which is why it has its own code.
        _record_routing_failure(ROUTING_INVALID, exc, metrics=metrics, tenant_id=tenant_id)
        return None
    return decision.as_dict()


def _record_routing_failure(
    code: str,
    exc: BaseException,
    *,
    metrics: MetricsStore | None,
    tenant_id: str | None,
) -> None:
    """Make a broken routing path visible. Never logs the user's text.

    The message is the exception's class and its own text, which this client
    authors and which carries no user data — the question is not part of it.
    """
    _LOGGER.warning("routing decision unavailable: code=%s error=%s", code, _safe_reason(exc))
    if metrics is None or not tenant_id:
        return
    try:
        metrics.record(
            tenant_id=tenant_id,
            route_type="routing",
            outcome="failed",
            error_code=code,
        )
    except Exception as exc:  # noqa: BLE001 - metrics must never fail a request
        _LOGGER.warning("routing metric could not be recorded: %s", _safe_reason(exc))


def _safe_reason(exc: BaseException) -> str:
    """A bounded, credential-free description of a failure."""
    text = str(exc)
    return f"{type(exc).__name__}: {text[:120]}" if text else type(exc).__name__


def thresholds_from(values: Mapping[str, Any], *, prefix: str = "AIOPS_GATEWAY_") -> RoutingThresholds:
    """Build thresholds from the environment, falling back to the calibrated pair."""
    thresholds = RoutingThresholds(
        risk_at_least=_float_env(values, f"{prefix}ROUTING_RISK_AT_LEAST", 0.5),
        confidence_at_least=_float_env(values, f"{prefix}ROUTING_CONFIDENCE_AT_LEAST", 0.8),
    )
    thresholds.validate()
    return thresholds


def _float_env(values: Mapping[str, Any], name: str, default: float) -> float:
    raw = values.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
