"""The diagnosis surface for the newly added languages (#541).

Two different answers ship here, and the difference is the point:

- **Vietnamese and Mongolian** can be PROMPTED — Latin and Cyrillic script with
  word boundaries, so the matchers and the rule layer can back a claim to answer
  in them. They work end to end.
- **Thai and Khmer** cannot. No word boundaries means the deterministic matchers
  degrade to verbatim lookup and the word-boundary regexes cannot be written for
  them at all. A request in one of them is ROUTED as unsupported rather than
  half-served, so the stored language never claims text it does not contain.
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.i18n import (
    SUPPORTED_LANGUAGES,
    can_prompt_in,
    language_name,
    resolve_language,
)

_PROMPTABLE = ("vi", "mn")
_NOT_PROMPTABLE = ("th", "km")


def test_the_four_new_languages_are_supported() -> None:
    for language in _PROMPTABLE + _NOT_PROMPTABLE:
        assert language in SUPPORTED_LANGUAGES, language


@pytest.mark.parametrize("language", _PROMPTABLE)
def test_vietnamese_and_mongolian_can_be_prompted(language: str) -> None:
    """They must carry a prompt-usable display name, or the prompt says nothing."""
    assert can_prompt_in(language) is True
    assert language_name(language).strip()
    assert language_name(language) != language_name("zh")


@pytest.mark.parametrize("language", _NOT_PROMPTABLE)
def test_thai_and_khmer_cannot_be_prompted(language: str) -> None:
    """Declared, not merely unimplemented.

    `#534`'s capability split says these are read-only languages. Nothing
    enforced it: the diagnosis prompt would have said "write in Thai" while the
    routing layer cannot inspect Thai at all. This is the assertion that makes
    the declaration real.
    """
    assert can_prompt_in(language) is False


def test_an_unknown_language_cannot_be_prompted() -> None:
    assert can_prompt_in("ja") is False
    assert can_prompt_in("") is False


@pytest.mark.parametrize("language", _NOT_PROMPTABLE)
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
    assert "language = language if can_prompt_in(language) else DEFAULT_LANGUAGE" in source
    # And it is applied before the row is created.
    assert source.index("can_prompt_in(language)") < source.index("create_standard_diagnosis")
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


def test_traditional_chinese_is_out_of_scope_for_this_ticket() -> None:
    """`zh-Hant` is not in the inventory: adding it is #531's job, and until it
    is added the tag folds to `zh` — so the diagnosis surface cannot serve it,
    and this ticket does not pretend otherwise."""
    assert "zh-Hant" not in SUPPORTED_LANGUAGES
    assert resolve_language("zh-Hant") == "zh"


def test_every_model_prompting_route_normalises_a_non_promptable_language() -> None:
    """`prompting=False` must hold on EVERY route that reaches a model — not
    just the one that happened to consume the flag first.

    The diagnosis face was the flag's first consumer; the assistant QA route and
    the routing classifier reach a model too. A declaration enforced on one of
    three consumers is not a boundary, it is a comment.
    """
    import inspect

    from aiops_diagnostics.gateway_runtime import GatewayRuntime

    normalise = "language = language if can_prompt_in(language) else DEFAULT_LANGUAGE"
    for method in (
        GatewayRuntime.start_standard_diagnosis,
        GatewayRuntime.start_assistant_qa,
        GatewayRuntime.classify_lightweight_model,
    ):
        source = inspect.getsource(method)
        assert normalise in source, f"{method.__name__} 未归一化非可提示语言"
