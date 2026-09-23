"""Jev decision client (PRD #383, ticket #389).

Jev is not a text model. You send it business facts and **typed questions**
(``choice`` / ``noul`` / ``score``), and it returns **typed decisions with
probabilities** — a label from a list you supplied, a yes/no probability, or a
position on a scale. There is no completion, so there is nothing to parse: the
answer is a value the caller can branch on directly.

That is the whole reason this repository talks to it. The lightweight classifier
used to get its routing decision by asking a model to *generate* JSON and then
parsing that text back, and on 2026-09-22 the transport dropped the opening
characters of one such answer in production (see ``turn_recovery``). Typed
decisions remove that failure mode structurally rather than tolerating it.

Wire shape (measured against the live gateway, 2026-09-23)::

    POST {base_url}/systemone
    Authorization: Bearer <key>
    {"model": "typesafe/jev", "state": "<facts>",
     "questions": {"<id>": {"type": "choice", "instructions": "...",
                            "criteria": {"label": "description"}}}}
    → {"model": "typesafe/jev",
       "answers": {"<id>": {"type": "choice", "choice": "label",
                            "confidence": 0.98, "probabilities": {...}}},
       "usage": {"input_tokens": 278, "output_tokens": 20}}

**A User-Agent is mandatory and is not a stylistic choice.** The gateway's WAF
rejects the default ``Python-urllib/3.x`` agent with ``403 error code: 1010``,
which reads exactly like rate limiting or a rejected key — during this ticket's
own investigation it was misdiagnosed as a 1-request-per-30-seconds quota. It is
neither. Any client here must set an explicit agent, and the default below is
part of the contract rather than a convenience.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from aiops_diagnostics.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    RetryPolicy,
    bearer_auth_header,
    join_url,
    json_body,
    request_json,
)

#: The agent the gateway's WAF accepts. See the module docstring: omitting it is
#: the difference between 200 and a 403 that looks like an auth or quota error.
DEFAULT_USER_AGENT = "aiops-gateway/1.0 (+https://github.com/kilbertert/AI-Ops)"

#: Jev identifiers this client will ask for. Pinned rather than free-form so a
#: typo surfaces here instead of as a confusing upstream rejection.
KNOWN_MODELS = frozenset({"typesafe/jev"})

DEFAULT_MODEL = "typesafe/jev"

#: The three primitives Jev answers with. Anything else is a programming error
#: in the caller, not an upstream failure, so it fails before a request is sent.
CHOICE = "choice"
NOUL = "noul"
SCORE = "score"
_QUESTION_TYPES = frozenset({CHOICE, NOUL, SCORE})


class JevError(RuntimeError):
    """Base for every failure this client raises."""


class JevUnavailable(JevError):
    """The decision service could not be reached or refused the request.

    Retryable by nature, and callers are expected to have a fallback: a routing
    decision that cannot be obtained must degrade to the previous behaviour, not
    fail the user's request.
    """


class JevInvalidResponse(JevError):
    """The service answered, but not with a decision this client can read.

    A defect rather than an outage, in the same spirit as
    ``AgentContractError``: it means one of the two sides changed shape.
    """


@dataclass(frozen=True, slots=True)
class Choice:
    """Pick one label from ``criteria``."""

    instructions: str
    criteria: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Noul:
    """A yes/no question, answered as the probability that the answer is yes."""

    instructions: str


@dataclass(frozen=True, slots=True)
class Score:
    """A position on an ordered scale, lowest level first."""

    instructions: str
    criteria: Sequence[str]


Question = Choice | Noul | Score


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class NoulAnswer:
    noul: float


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    score: float
    confidence: float
    legend: Mapping[str, str] = field(default_factory=dict)
    probabilities: Mapping[str, float] = field(default_factory=dict)


Answer = ChoiceAnswer | NoulAnswer | ScoreAnswer


@dataclass(frozen=True, slots=True)
class Decision:
    """One service response: the model that answered plus one answer per question."""

    model: str
    answers: Mapping[str, Answer]
    usage: Mapping[str, Any] = field(default_factory=dict)


def _primitive_of(question: Question) -> str:
    if isinstance(question, Choice):
        return CHOICE
    if isinstance(question, Noul):
        return NOUL
    if isinstance(question, Score):
        return SCORE
    raise ValueError(f"unsupported question type: {type(question).__name__}")


def _question_payload(question: Question) -> dict[str, Any]:
    if isinstance(question, Choice):
        if not question.criteria:
            raise ValueError("a choice question needs at least one criterion")
        return {"type": CHOICE, "instructions": question.instructions, "criteria": dict(question.criteria)}
    if isinstance(question, Noul):
        return {"type": NOUL, "instructions": question.instructions}
    if isinstance(question, Score):
        # A bare string would iterate per character and ship a scale of single
        # letters, which upstream would answer against without complaint.
        if isinstance(question.criteria, str) or not question.criteria:
            raise ValueError("a score question needs at least one level")
        return {"type": SCORE, "instructions": question.instructions, "criteria": list(question.criteria)}
    raise ValueError(f"unsupported question type: {type(question).__name__}")


def _as_number(value: Any) -> float | None:
    """Coerce a numeric field, rejecting bool and non-finite values.

    ``bool`` is an ``int`` in Python, so ``isinstance(True, (int, float))``
    passes — and a service answering ``true`` where a probability belongs should
    be a contract failure, not the number 1.0.

    Non-finite values are rejected for a sharper reason: ``json.loads`` accepts
    the literals ``NaN`` and ``Infinity``, and a NaN that reaches a caller's
    threshold comparison makes every comparison false in **both** directions.
    A routing rule like ``confidence >= 0.8`` would then reject the value
    without anyone learning the response was malformed. Better to fail here,
    where the cause is still legible.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _as_probability(value: Any) -> float | None:
    """A number that is also a valid probability (0..1)."""
    number = _as_number(value)
    if number is None or not 0.0 <= number <= 1.0:
        return None
    return number


def _as_number_map(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, item in value.items():
        number = _as_number(item)
        if number is not None:
            out[str(key)] = number
    return out


def _as_probability_map(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, item in value.items():
        number = _as_probability(item)
        if number is not None:
            out[str(key)] = number
    return out


def _parse_answer(answer_id: str, raw: Any) -> Answer:
    if not isinstance(raw, Mapping):
        raise JevInvalidResponse(f"answer {answer_id!r} is not an object")
    kind = raw.get("type")
    if kind == CHOICE:
        choice = raw.get("choice")
        confidence = _as_probability(raw.get("confidence"))
        if not isinstance(choice, str) or not choice:
            raise JevInvalidResponse(f"choice answer {answer_id!r} carried no label")
        if confidence is None:
            raise JevInvalidResponse(f"choice answer {answer_id!r} carried no confidence")
        return ChoiceAnswer(
            choice=choice, confidence=confidence, probabilities=_as_probability_map(raw.get("probabilities"))
        )
    if kind == NOUL:
        noul = _as_probability(raw.get("noul"))
        if noul is None:
            raise JevInvalidResponse(f"noul answer {answer_id!r} carried no value")
        if not 0.0 <= noul <= 1.0:
            raise JevInvalidResponse(f"noul answer {answer_id!r} is outside 0..1")
        return NoulAnswer(noul=noul)
    if kind == SCORE:
        score = _as_number(raw.get("score"))
        if score is None:
            raise JevInvalidResponse(f"score answer {answer_id!r} carried no score")
        legend = raw.get("legend")
        confidence = _as_probability(raw.get("confidence"))
        return ScoreAnswer(
            score=score,
            # A score may legitimately carry no confidence; only an invalid one
            # is a contract failure.
            confidence=confidence if confidence is not None else 0.0,
            legend={str(k): str(v) for k, v in legend.items()} if isinstance(legend, Mapping) else {},
            probabilities=_as_probability_map(raw.get("probabilities")),
        )
    raise JevInvalidResponse(f"answer {answer_id!r} has an unsupported type: {kind!r}")


#: Which answer class each question primitive must come back as.
_ANSWER_FOR_QUESTION: dict[str, type] = {CHOICE: ChoiceAnswer, NOUL: NoulAnswer, SCORE: ScoreAnswer}


def parse_decision(
    payload: Any,
    *,
    expected: Mapping[str, Question] | None = None,
    expected_ids: Sequence[str] = (),
) -> Decision:
    """Read a service response into a ``Decision``.

    Kept separate from the transport so the parsing contract is testable without
    a server, and so a caller that already holds a payload can reuse it.

    ``expected`` is the question set that was asked, and passing it is what makes
    the correspondence checkable. Verifying only that each id is present accepts
    a ``noul`` answer to a ``choice`` question: the caller then holds an object
    with no ``choice`` field where its own types promised one, and the mismatch
    surfaces far from its cause. ``expected_ids`` remains for callers that hold
    only id strings and can accept the weaker check.
    """
    if not isinstance(payload, Mapping):
        raise JevInvalidResponse("response is not an object")
    answers_raw = payload.get("answers")
    if not isinstance(answers_raw, Mapping):
        raise JevInvalidResponse("response carried no answers")
    answers = {str(key): _parse_answer(str(key), value) for key, value in answers_raw.items()}
    missing = [item for item in expected_ids if item not in answers]
    if missing:
        raise JevInvalidResponse(f"response omitted requested questions: {', '.join(missing)}")
    for name, question in (expected or {}).items():
        answer = answers.get(name)
        if answer is None:
            raise JevInvalidResponse(f"response omitted requested question: {name}")
        wanted = _ANSWER_FOR_QUESTION.get(_primitive_of(question))
        if wanted is not None and not isinstance(answer, wanted):
            raise JevInvalidResponse(
                f"answer {name!r} is a {type(answer).__name__} but the question asked for a {wanted.__name__}"
            )
        if (
            isinstance(question, Choice)
            and isinstance(answer, ChoiceAnswer)
            and answer.choice not in question.criteria
        ):
            raise JevInvalidResponse(
                f"answer {name!r} chose {answer.choice!r}, which is not one of the "
                f"labels that question offered"
            )
    usage = payload.get("usage")
    return Decision(
        model=str(payload.get("model") or ""),
        answers=answers,
        usage=dict(usage) if isinstance(usage, Mapping) else {},
    )


@dataclass(frozen=True, slots=True)
class JevSettings:
    base_url: str
    api_key: str = field(repr=False, default="")
    model: str = DEFAULT_MODEL
    timeout: float = 20.0
    user_agent: str = DEFAULT_USER_AGENT

    def validate(self) -> None:
        if not self.base_url.strip():
            raise ValueError("Jev base_url is required")
        # The bearer key and the business state both travel in this request, so
        # cleartext transport is a credential disclosure, not just a hygiene
        # issue. Same rule the model providers already follow: https anywhere,
        # http only for a loopback address.
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Jev base_url must be a complete http or https URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Jev base_url must use https unless it is a loopback address")
        if not self.api_key.strip():
            raise ValueError("Jev api_key is required")
        if self.model not in KNOWN_MODELS:
            raise ValueError(f"unknown Jev model: {self.model}")
        # Blank is not "use the default": the WAF then rejects every request with
        # a 403 that reads like an auth failure, which is exactly the confusion
        # the module docstring exists to prevent.
        if not self.user_agent.strip():
            raise ValueError("Jev user_agent is required")


class JevDecisionClient:
    """Ask Jev typed questions about one piece of business state."""

    def __init__(self, settings: JevSettings) -> None:
        settings.validate()
        self.settings = settings

    def _error_mapping(self) -> ErrorMapping:
        def _unavailable(failure: HttpFailure) -> Exception:
            # Deliberately does not echo the upstream body: a rejected request
            # or a bad key would otherwise put service detail into our logs.
            return JevUnavailable("decision service unavailable")

        def _invalid_body(_failure: HttpFailure) -> Exception:
            return JevInvalidResponse("decision service returned a non-JSON body")

        def _invalid_envelope(_failure: HttpFailure) -> Exception:
            return JevInvalidResponse("decision service returned an unexpected envelope")

        return ErrorMapping(
            auth_rejected=_unavailable,
            http_error=_unavailable,
            unavailable=_unavailable,
            invalid_body=_invalid_body,
            invalid_envelope=_invalid_envelope,
        )

    def decide(self, state: str, questions: Mapping[str, Question]) -> Decision:
        """Ask every question against ``state`` in one call.

        Questions are evaluated independently and in parallel upstream, so
        asking more of them together costs one round trip rather than several.
        """
        if not questions:
            raise ValueError("at least one question is required")
        for question_id, question in questions.items():
            if not isinstance(question, (Choice, Noul, Score)):
                raise ValueError(f"unsupported question type for {question_id!r}: {type(question).__name__}")
        payload = {
            "model": self.settings.model,
            "state": state,
            "questions": {name: _question_payload(item) for name, item in questions.items()},
        }
        spec = RequestSpec(
            url=join_url(self.settings.base_url, "/systemone"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": bearer_auth_header(self.settings.api_key),
                # Mandatory: see the module docstring.
                "User-Agent": self.settings.user_agent,
            },
            body=json_body(payload),
            timeout=self.settings.timeout,
        )
        body = request_json(
            spec,
            mapping=self._error_mapping(),
            # Transient transport failures are worth one retry, and this POST is
            # safe to repeat: the question set is a pure function of the request,
            # so a second attempt cannot double-apply anything. That is why POST
            # is opted in here — the skeleton's default exists to protect the
            # calls that *do* have effects. A 4xx is the upstream's deliberate
            # answer and is mapped straight through, never retried.
            retry=RetryPolicy(
                max_retries=1,
                base_delay_seconds=0.5,
                max_delay_seconds=2.0,
                jitter_seconds=0.2,
                methods=frozenset({"POST"}),
            ),
        )
        return parse_decision(body, expected=questions)


def _demo() -> None:
    """Self-check for the parsing contract; no network.

    The wire shapes below are the ones the live gateway returned, trimmed to the
    fields this client reads.
    """
    live = {
        "model": "typesafe/jev",
        "answers": {
            "intent": {
                "type": "choice",
                "choice": "casual",
                "confidence": 1,
                "probabilities": {"casual": 1, "knowledge": 0},
            },
            "risk": {"type": "noul", "noul": 0.02},
            "grade": {"type": "score", "score": 3, "legend": {"0": "low", "3": "high"}},
        },
        "usage": {"input_tokens": 278, "output_tokens": 20},
    }
    decision = parse_decision(live, expected_ids=["intent", "risk", "grade"])
    assert decision.model == "typesafe/jev"
    assert decision.answers["intent"].choice == "casual"
    assert decision.answers["intent"].confidence == 1.0
    assert decision.answers["risk"].noul == 0.02
    assert decision.answers["grade"].score == 3.0

    # Request building covers all three primitives.
    built = _question_payload(Choice(instructions="which?", criteria={"a": "A", "b": "B"}))
    assert built["type"] == "choice" and set(built["criteria"]) == {"a", "b"}
    assert _question_payload(Noul(instructions="urgent?")) == {"type": "noul", "instructions": "urgent?"}
    assert _question_payload(Score(instructions="how sure?", criteria=["low", "high"]))["criteria"] == [
        "low",
        "high",
    ]

    # A boolean is not a probability, and neither is NaN.
    assert _as_probability(True) is None
    assert _as_probability(0) == 0.0
    assert _as_probability(float("nan")) is None
    assert _as_probability(1.5) is None
    assert _as_number(float("inf")) is None

    # Malformed answers are contract failures, not silent defaults.
    for bad in (
        {},
        {"answers": {"q": {"type": "choice"}}},  # no label
        {"answers": {"q": {"type": "noul", "noul": 1.5}}},  # outside 0..1
        {"answers": {"q": {"type": "mystery", "x": 1}}},  # unknown primitive
        {"answers": {}},  # requested id absent
        {"answers": {"q": {"type": "choice", "choice": "a"}}},  # no confidence
    ):
        expected = ["q"]
        try:
            parse_decision(bad, expected_ids=expected)
        except JevInvalidResponse:
            pass
        else:
            raise AssertionError(f"expected JevInvalidResponse for {bad!r}")
    print("jev_decisions self-check ok")


if __name__ == "__main__":
    _demo()
