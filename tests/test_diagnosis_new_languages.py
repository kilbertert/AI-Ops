"""已登记语言都能进入模型；未知语言仍归一化。"""

from __future__ import annotations

import pytest

from aiops_diagnostics.i18n import (
    SUPPORTED_LANGUAGES,
    can_prompt_in,
    language_name,
    resolve_language,
)

_PROMPTABLE = ("vi", "mn")
_FORMERLY_BLOCKED = ("th", "km")


def test_the_four_new_languages_are_supported() -> None:
    for language in _PROMPTABLE + _FORMERLY_BLOCKED:
        assert language in SUPPORTED_LANGUAGES, language


@pytest.mark.parametrize("language", _PROMPTABLE)
def test_vietnamese_and_mongolian_can_be_prompted(language: str) -> None:
    """They must carry a prompt-usable display name, or the prompt says nothing."""
    assert can_prompt_in(language) is True
    assert language_name(language).strip()
    assert language_name(language) != language_name("zh")


@pytest.mark.parametrize("language", _FORMERLY_BLOCKED)
def test_thai_and_khmer_can_be_prompted(language: str) -> None:
    """旧分词限制不能变成模型语言禁令。"""
    assert can_prompt_in(language) is True


def test_an_unknown_language_cannot_be_prompted() -> None:
    assert can_prompt_in("ja") is False
    assert can_prompt_in("") is False


@pytest.mark.parametrize("language", _FORMERLY_BLOCKED)
def test_a_non_promptable_request_is_normalised_at_job_creation(language: str) -> None:
    """The STORED language must be the one the answer is actually written in.

    Normalising at render time would be too late: the stored value is what the
    response reports for the result's prose (#549), so a row saying `th` while
    the model wrote Chinese describes text it does not contain — and every later
    poll of that row repeats the claim.
    """
    import inspect

    from aiops_diagnostics.gateway_runtime import GatewayRuntime

    source = inspect.getsource(GatewayRuntime.start_standard_diagnosis)
    # The normalisation is one line with one consumer; asserted verbatim so that
    # moving it to render time (which would break the stored value) fails here.
    assert "language = effective_language(language)" in source
    # And it is applied before the row is created.
    assert source.index("effective_language(language)") < source.index("create_standard_diagnosis")
    # The destination it falls back to is a language the surface can prompt.
    from aiops_diagnostics.i18n import DEFAULT_LANGUAGE

    assert can_prompt_in(DEFAULT_LANGUAGE) is True


@pytest.mark.parametrize("language", _PROMPTABLE)
def test_the_prompt_names_the_new_language(language: str) -> None:
    """Through the real prompt builders, not the lookup beside them.

    The wiring is what breaks: `_developer_instructions` and the coordinator's
    initial prompt both interpolate `language_name`, and a missing entry there
    would silently fall back to the default language.
    """
    from aiops_diagnostics.codex_runtime import _developer_instructions
    from aiops_diagnostics.i18n import DEFAULT_LANGUAGE

    text = _developer_instructions(language)
    assert f"field in {language_name(language)}:" in text
    assert f"field in {language_name(DEFAULT_LANGUAGE)}:" not in text


def test_the_retry_prompt_restates_the_new_language() -> None:
    """#536's fix must cover the new languages too, not only the original six."""
    import inspect

    from aiops_diagnostics.agent_engine import AgentCoordinator

    source = inspect.getsource(AgentCoordinator._request_contract_repair)
    assert "Write the corrected response in {language_name(self.language)}" in source


@pytest.mark.parametrize("language", _PROMPTABLE)
def test_the_guard_covers_the_new_languages(language: str) -> None:
    """A non-Chinese answer in a new language is still scanned for Chinese."""
    from aiops_diagnostics.answer_language import answer_chinese_leak

    assert answer_chinese_leak("余额耗尽停止订单", language) != ""
    assert answer_chinese_leak("Balance exhausted, order stopped", language) == ""


def test_traditional_chinese_arrived_with_its_own_ticket() -> None:
    """`zh-Hant` was out of scope for #541 and is IN scope now that #531 landed.

    The earlier version of this test asserted it was absent and that the tag
    folded to `zh`. Both are false now — that is what #531 changed — so the
    assertion records the handover rather than being deleted.
    """
    assert "zh-Hant" in SUPPORTED_LANGUAGES
    assert resolve_language("zh-Hant") == "zh-Hant"
    assert can_prompt_in("zh-Hant") is True


def test_every_model_prompting_route_normalises_a_non_promptable_language() -> None:
    """Unknown languages must be normalized on every route reaching a model — not
    just the one that happened to consume the flag first.

    The diagnosis face was the flag's first consumer; the assistant QA route and
    the routing classifier reach a model too. A declaration enforced on one of
    three consumers is not a boundary, it is a comment.
    """
    import inspect

    from aiops_diagnostics.gateway_runtime import GatewayRuntime

    normalise = "language = effective_language(language)"
    for method in (
        GatewayRuntime.start_standard_diagnosis,
        GatewayRuntime.start_assistant_qa,
        GatewayRuntime.classify_lightweight_model,
    ):
        source = inspect.getsource(method)
        assert normalise in source, f"{method.__name__} 未归一化非可提示语言"


def test_the_qa_response_boundary_reports_the_effective_language() -> None:
    """新请求保留语言；运行记录提供语言时以记录为准。"""
    from aiops_diagnostics.gateway_api import _assistant_question_response

    row = {"qa_id": "qa_1", "question": "q", "status": "completed", "result": {"text": "答案"}}
    for language in _FORMERLY_BLOCKED:
        assert _assistant_question_response(dict(row), language)["language"] == language, language
    for language in _PROMPTABLE:
        assert _assistant_question_response(dict(row), language)["language"] == language
