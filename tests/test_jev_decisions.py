"""The Jev decision client: request building, parsing, and failure mapping (#389).

The client exists so a routing decision stops being *generated text*. These tests
pin the two things that follows from: the request must be a well-formed typed
question set, and every way the service can fail must arrive as one of this
client's own errors rather than a bare urllib exception — because its caller has
to degrade to a fallback, and it cannot do that if it is catching the standard
library's vocabulary.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from typing import Any

import pytest

from aiops_diagnostics.bounded_http import RequestSpec
from aiops_diagnostics.jev_decisions import (
    DEFAULT_USER_AGENT,
    Choice,
    ChoiceAnswer,
    JevDecisionClient,
    JevInvalidResponse,
    JevSettings,
    JevUnavailable,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    parse_decision,
)

BASE = "https://decision.example"


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self, *_args: Any) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@dataclass
class _Recorder:
    """Captures the outgoing request so the wire contract can be asserted."""

    body: dict[str, Any] | None = None
    spec: RequestSpec | None = None


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    from aiops_diagnostics import bounded_http

    captured = _Recorder()

    def fake_open_response(spec: RequestSpec) -> Any:
        captured.spec = spec
        captured.body = json.loads((spec.body or b"{}").decode())
        # Answer whatever was asked, so a test can assert on the request without
        # the stub silently failing the response contract.
        answers: dict[str, Any] = {}
        for name, question in (captured.body.get("questions") or {}).items():
            if question.get("type") == "choice":
                options = list(question.get("criteria") or {})
                answers[name] = {"type": "choice", "choice": options[0], "confidence": 0.5}
            elif question.get("type") == "score":
                answers[name] = {"type": "score", "score": 1, "confidence": 0.5}
            else:
                answers[name] = {"type": "noul", "noul": 0.5}
        return _Response(json.dumps({"model": "typesafe/jev", "answers": answers}).encode())

    monkeypatch.setattr(bounded_http, "open_response", fake_open_response)
    return captured


def _client(**overrides: Any) -> JevDecisionClient:
    settings = JevSettings(base_url=BASE, api_key="test-key", **overrides)
    return JevDecisionClient(settings)


# --- The request contract ---


def test_all_three_primitives_go_out_in_one_typed_request(recorder: _Recorder) -> None:
    """Questions are batched, and each carries the shape its primitive needs."""
    _client().decide(
        "Payments failed for three days.",
        {
            "intent": Choice(instructions="which kind?", criteria={"a": "first", "b": "second"}),
            "urgent": Noul(instructions="needs urgency?"),
            "grade": Score(instructions="how bad?", criteria=["mild", "severe"]),
        },
    )
    sent = recorder.body
    assert sent is not None
    assert sent["model"] == "typesafe/jev"
    assert sent["state"] == "Payments failed for three days."
    assert sent["questions"]["intent"] == {
        "type": "choice",
        "instructions": "which kind?",
        "criteria": {"a": "first", "b": "second"},
    }
    assert sent["questions"]["urgent"] == {"type": "noul", "instructions": "needs urgency?"}
    assert sent["questions"]["grade"]["criteria"] == ["mild", "severe"]


def test_the_request_carries_an_explicit_user_agent(recorder: _Recorder) -> None:
    """The WAF rejects urllib's default agent, and the rejection looks like auth.

    Measured 2026-09-23: the same request returns 200 with any ordinary agent and
    ``403 error code: 1010`` with ``Python-urllib/3.x``. That is the whole reason
    this is asserted rather than left to urllib's default.
    """
    _client().decide("state", {"q": Noul(instructions="x?")})
    assert recorder.spec is not None
    assert recorder.spec.headers["User-Agent"] == DEFAULT_USER_AGENT


def test_the_api_key_never_leaks_through_the_settings_repr() -> None:
    """A key in a traceback or a log line is a credential disclosure."""
    settings = JevSettings(base_url=BASE, api_key="super-secret-key")
    assert "super-secret-key" not in repr(settings)


def test_only_the_pinned_model_is_accepted() -> None:
    with pytest.raises(ValueError):
        JevSettings(base_url=BASE, api_key="k", model="some/other-model").validate()


@pytest.mark.parametrize(
    "question",
    [Choice(instructions="x", criteria={}), Score(instructions="x", criteria=[])],
)
def test_a_question_with_no_options_is_rejected_before_sending(question: Any) -> None:
    """An empty criteria set is a caller bug, and upstream would only blur it."""
    with pytest.raises(ValueError):
        _client().decide("state", {"q": question})


def test_an_empty_question_set_is_rejected() -> None:
    with pytest.raises(ValueError):
        _client().decide("state", {})


# --- Parsing ---


def test_a_live_shaped_response_parses_to_typed_answers() -> None:
    """The shape the gateway actually returns (captured 2026-09-23)."""
    decision = parse_decision(
        {
            "model": "typesafe/jev",
            "answers": {
                "intent": {
                    "type": "choice",
                    "choice": "casual",
                    "confidence": 1,
                    "probabilities": {"casual": 1, "knowledge": 0},
                },
                "risk": {"type": "noul", "noul": 0.02},
            },
            "usage": {"input_tokens": 278, "output_tokens": 20},
        },
        expected_ids=["intent", "risk"],
    )
    assert decision.model == "typesafe/jev"
    intent = decision.answers["intent"]
    assert isinstance(intent, ChoiceAnswer)
    assert intent.choice == "casual"
    assert intent.confidence == 1.0
    assert intent.probabilities["knowledge"] == 0.0
    risk = decision.answers["risk"]
    assert isinstance(risk, NoulAnswer)
    assert risk.noul == 0.02


def test_a_score_answer_keeps_its_legend() -> None:
    decision = parse_decision(
        {"answers": {"g": {"type": "score", "score": 3, "confidence": 0.9, "legend": {"3": "high"}}}},
        expected_ids=["g"],
    )
    answer = decision.answers["g"]
    assert isinstance(answer, ScoreAnswer)
    assert answer.score == 3.0
    assert answer.legend == {"3": "high"}


def test_a_requested_question_missing_from_the_response_is_a_contract_failure() -> None:
    """Silently defaulting a missing decision would route on an invented answer."""
    with pytest.raises(JevInvalidResponse):
        parse_decision({"answers": {"other": {"type": "noul", "noul": 0.5}}}, expected_ids=["intent"])


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"answers": []},
        {"answers": {"q": "not-an-object"}},
        {"answers": {"q": {"type": "choice"}}},
        {"answers": {"q": {"type": "choice", "choice": ""}}},
        {"answers": {"q": {"type": "choice", "choice": "a"}}},
        {"answers": {"q": {"type": "noul"}}},
        {"answers": {"q": {"type": "noul", "noul": 1.5}}},
        {"answers": {"q": {"type": "noul", "noul": -0.1}}},
        {"answers": {"q": {"type": "unknown", "v": 1}}},
    ],
)
def test_a_malformed_response_is_a_contract_failure(payload: Any) -> None:
    with pytest.raises(JevInvalidResponse):
        parse_decision(payload, expected_ids=["q"])


def test_a_boolean_is_not_accepted_where_a_probability_belongs() -> None:
    """``True`` is an ``int`` in Python; accepting it would invent confidence 1.0."""
    with pytest.raises(JevInvalidResponse):
        parse_decision({"answers": {"q": {"type": "noul", "noul": True}}}, expected_ids=["q"])


def test_boundary_probabilities_are_accepted() -> None:
    for value in (0, 1, 0.0, 1.0):
        decision = parse_decision({"answers": {"q": {"type": "noul", "noul": value}}}, expected_ids=["q"])
        assert decision.answers["q"].noul == float(value)


# --- Failure mapping: every upstream failure arrives as this client's own error ---


def test_a_rejected_request_maps_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx must not surface as a bare HTTPError — the caller catches ours."""
    from aiops_diagnostics import bounded_http

    def reject(_spec: RequestSpec) -> Any:
        raise urllib.error.HTTPError(BASE, 403, "Forbidden", {}, None)

    monkeypatch.setattr(bounded_http, "open_response", reject)
    with pytest.raises(JevUnavailable):
        _client().decide("state", {"q": Noul(instructions="x?")})


def test_a_transport_failure_maps_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiops_diagnostics import bounded_http

    def boom(_spec: RequestSpec) -> Any:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(bounded_http, "open_response", boom)
    with pytest.raises(JevUnavailable):
        _client().decide("state", {"q": Noul(instructions="x?")})


def test_a_non_json_body_maps_to_invalid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiops_diagnostics import bounded_http

    monkeypatch.setattr(bounded_http, "open_response", lambda _spec: _Response(b"<html>not json</html>"))
    with pytest.raises(JevInvalidResponse):
        _client().decide("state", {"q": Noul(instructions="x?")})


def test_a_transient_failure_is_retried_before_giving_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """One transient failure should not cost the caller its decision.

    The POST is safe to repeat — the question set is a pure function of the
    request — so a single retry is opted in explicitly.
    """
    from aiops_diagnostics import bounded_http

    attempts = {"n": 0}

    def flaky(spec: RequestSpec) -> Any:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.URLError("transient")
        return _Response(json.dumps({"answers": {"q": {"type": "noul", "noul": 0.4}}}).encode())

    monkeypatch.setattr(bounded_http, "open_response", flaky)
    monkeypatch.setattr(bounded_http.time, "sleep", lambda _seconds: None)
    decision = _client().decide("state", {"q": Noul(instructions="x?")})
    assert attempts["n"] == 2
    assert decision.answers["q"].noul == 0.4


def test_a_rejected_request_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx is the upstream's deliberate answer; retrying it wastes the caller's time."""
    from aiops_diagnostics import bounded_http

    attempts = {"n": 0}

    def reject(_spec: RequestSpec) -> Any:
        attempts["n"] += 1
        raise urllib.error.HTTPError(BASE, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(bounded_http, "open_response", reject)
    with pytest.raises(JevUnavailable):
        _client().decide("state", {"q": Noul(instructions="x?")})
    assert attempts["n"] == 1
