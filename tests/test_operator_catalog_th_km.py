"""Thai and Khmer: readable, not askable (#530).

These are the languages the capability split exists for. They can be RENDERED —
the catalog and the system copy carry them — but they cannot be ROUTED: no word
boundaries means the deterministic matchers degrade to verbatim lookup, and the
high-risk order cues (a hand-written word-boundary regex) cannot be written for
them at all. This ticket makes that boundary a FACT of the code rather than a
sentence in a decision record, and reports the degradation instead of hiding it
behind a 200.
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.faq import FAQCatalog
from aiops_diagnostics.i18n import (
    SUPPORTED_LANGUAGES,
    can_prompt_in,
    chinese_leak,
    free_text_unavailable_message,
    resolve_language,
)

_READ_ONLY = ("th", "km")


@pytest.mark.parametrize("language", _READ_ONLY)
def test_the_catalog_serves_the_language(language: str) -> None:
    """Readable: whole catalog, not a sample."""
    catalog = FAQCatalog.bundled()
    for entry in catalog.catalog("operator"):
        answer = catalog.answer("operator", entry["question_id"], language)
        assert chinese_leak(answer["question"]) == "", entry["question_id"]
        assert chinese_leak(answer["answer"]) == "", entry["question_id"]
    assert catalog.served_language("operator", language) == language


@pytest.mark.parametrize("language", _READ_ONLY)
def test_the_language_is_not_askable(language: str) -> None:
    """Not askable: declared, and consumed by every model-prompting route."""
    assert language in SUPPORTED_LANGUAGES, "读的语言也必须在受支持集里"
    assert can_prompt_in(language) is False


@pytest.mark.parametrize("language", _READ_ONLY)
def test_the_proposal_is_echoed_honestly(language: str) -> None:
    """The user's language is recognised — they are told what we cannot do.

    Falling back to `zh` at the resolver would tell a Thai speaker we do not
    know their language. Recognising it and refusing the free-text path tells
    them the truth: we can show you this in Thai, we cannot answer a Thai
    question.
    """
    assert resolve_language(f"{language}-TH,en;q=0.9") == language


@pytest.mark.parametrize("language", _READ_ONLY)
def test_the_refusal_is_in_their_own_language(language: str) -> None:
    """Not a Chinese sentence explaining that we cannot do Thai.

    The refusal is copy WE write, so it localises like any other: the user reads
    their own language while the model is never asked to write in it.
    """
    message = free_text_unavailable_message(language)
    assert message.strip()
    assert chinese_leak(message) == "", message
    assert "快捷" not in message and "shortcut" not in message.lower()


def test_the_refusal_message_covers_every_language() -> None:
    """A missing entry would hand the default language's sentence to a reader
    who asked for another — the silent-fallback shape this workstream removes."""
    for language in SUPPORTED_LANGUAGES:
        assert free_text_unavailable_message(language).strip()
        for other in SUPPORTED_LANGUAGES:
            if other != language:
                assert free_text_unavailable_message(language) != free_text_unavailable_message(other), (
                    f"{language} 与 {other} 的拒绝文案相同"
                )


def test_the_boundary_is_the_read_only_languages_and_nothing_else() -> None:
    """Every other language must remain askable — the split is a boundary, not
    a general disable."""
    for language in SUPPORTED_LANGUAGES:
        if language in _READ_ONLY:
            continue
        assert can_prompt_in(language) is True, language


def test_the_routing_degradation_is_measured_and_the_boundary_covers_it() -> None:
    """The degradation is worse than "verbatim only", and the boundary is why
    that is survivable.

    MEASURED (not assumed — the first version of this test asserted "verbatim
    only" and was wrong): `_normalize_keywords` swallows a Thai run into
    pseudo-tokens, and the matcher then stops being a matcher in BOTH
    directions — a REWORDED Thai question does not hit its own entry, and an
    UNRELATED Thai sentence hits an entry anyway. On a Chinese catalog the token
    sets are shared punctuation and fragments, so the containment bar does not
    separate them either.

    That is why the free-text boundary is checked BEFORE the FAQ match: letting
    a Thai question through to a matcher that is unsound for its script would
    produce a confidently wrong answer chosen from the catalog. These assertions
    pin the measurement, so a future change that fixes the tokenizer has to come
    back here and update the recorded degradation.
    """
    from aiops_diagnostics.faq import FAQCatalog
    from aiops_diagnostics.gateway_api import _faq_match, _normalize_keywords

    catalog = FAQCatalog.bundled()
    verbatim = catalog.answer("operator", "operator.faq.q001", "th")["question"]

    # Verbatim reproduces the title (the same string yields the same tokens).
    assert _faq_match("operator", catalog, verbatim)[0] == "operator.faq.q001"
    # An UNRELATED Thai sentence hits an entry anyway: the matcher is unsound
    # for this script, in the direction that matters. This is the reproducible
    # half; whether a *reworded* question hits is string-dependent, so it is not
    # asserted either way — claiming it would be the second over-claim here.
    unrelated = "อากาศวันนี้เป็นอย่างไร"
    assert _faq_match("operator", catalog, unrelated)[0] is not None, "假阳性消失，记录需更新"
    # The tokenizer is the cause: no spaces to split on.
    assert len(_normalize_keywords(verbatim)) <= 15
    assert all(" " not in token for token in _normalize_keywords(verbatim))


def test_the_free_text_boundary_runs_before_the_faq_match() -> None:
    """Ordering is the load-bearing part: the matcher is unsound for these
    scripts, so a Thai question must be refused before it can reach it."""
    import inspect

    from aiops_diagnostics.gateway_api import create_gateway_app

    source = inspect.getsource(create_gateway_app)
    boundary = source.index("if not can_prompt_in(language):")
    faq_match = source.index("_faq_match(decision.platform")
    assert boundary < faq_match, "边界检查排在了 FAQ 匹配之后 —— 泰文会先被不可靠的匹配器选中"


def test_every_assistant_route_refuses_a_read_only_language(tmp_path) -> None:
    """Not just the generic path: the routes that START JOBS must refuse too.

    A check placed after Route 1/Route 2 still returns a diagnosis or promo job
    that generates Chinese under a Thai label — the boundary has to be ahead of
    every model-backed route, and "ahead of the one I was looking at" is not the
    same thing. Exercised over the real HTTP surface, and asserting on the STUB's
    own record of what it was asked to do rather than on one call list.
    """
    from tests.test_assistant_api import _client, _headers

    client, runtime = _client(tmp_path)
    for payload in (
        # explicit order -> Route 1
        {"question": "ตรวจสอบคำสั่งซื้อ", "order_no": "2096164064667852801"},
        # embedded order in the text -> Route 1b
        {"question": "ช่วยตรวจสอบคำสั่งซื้อ 2096164064667852801 ด้วย"},
        # no order -> generic QA (Route 3)
        {"question": "อากาศวันนี้เป็นอย่างไร"},
    ):
        for language in _READ_ONLY:
            response = client.post(
                "/v1/assistant/questions",
                json=payload,
                headers={**_headers(), "Accept-Language": language},
            )
            assert response.status_code == 200, (language, payload)
            body = response.json()
            assert body["type"] == "clarification", (language, body.get("type"))
            assert body["language"] == language
            assert body["message"] == free_text_unavailable_message(language)

    # No job of ANY kind: the stub records every entry point, including
    # `start_assistant_qa`, which a single `calls` list would miss.
    assert runtime.calls == [], "边界之后仍然启动了诊断作业"
    assert runtime.qa_calls == [], "边界之后仍然启动了问答作业"


def test_an_unowned_order_is_still_404_not_a_language_clarification(tmp_path) -> None:
    """Authorization answers before the boundary does.

    Ordering the boundary first would turn "you do not own this order" into a
    language clarification — the boundary would become a way to ask whether an
    order exists. The two questions are unrelated and each keeps its own answer.
    """
    from tests.test_assistant_api import _client, _headers

    client, _ = _client(tmp_path, allowed_orders={"some-other-order"})
    response = client.post(
        "/v1/assistant/questions",
        json={"question": "ตรวจสอบคำสั่งซื้อ", "order_no": "2096164064667852801"},
        headers={**_headers(), "Accept-Language": "th"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ORDER_NOT_FOUND"
