from __future__ import annotations

from typing import Any

import pytest

from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, resolve_language


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        # Missing, blank, or unparseable headers fall back to the default.
        (None, "zh"),
        ("", "zh"),
        ("   ", "zh"),
        (";", "zh"),
        (";;,", "zh"),
        # Region/script subtags fold onto supported base tags; case folds.
        ("en-US", "en"),
        ("zh-Hans-CN", "zh"),
        ("pt-BR", "pt"),
        ("DE", "de"),
        # Highest q wins regardless of list order.
        ("fr;q=0.9, en;q=0.5", "fr"),
        ("en;q=0.5, fr;q=0.9", "fr"),
        ("de;q=0.1, fr;q=0.2, en", "en"),
        ("zh;q=0.2, en;q=0.3", "en"),
        # A bare tag defaults to q=1 and beats any lower weight.
        ("pt-BR;q=0.8, en", "en"),
        ("fr;q=0.8, es;q=0.9", "es"),
        ("en-US,en;q=0.9", "en"),
        # Equal q keeps the first occurrence.
        ("fr, de", "fr"),
        ("fr;q=0.5, de;q=0.5", "fr"),
        # q=0 means "not acceptable" (RFC 7231) and is excluded.
        ("fr;q=0, en", "en"),
        ("fr;q=0.0", "zh"),
        # Wildcards, unsupported languages, and malformed weights are ignored.
        ("*", "zh"),
        ("ja, ko", "zh"),
        ("*;q=0.9, ja;q=0.8", "zh"),
        ("en;q=abc", "zh"),
        ("en;q=1.5", "zh"),
        ("en;q=-1", "zh"),
        ("en;q=abc, fr", "fr"),
    ],
)
def test_resolve_language_matrix(header: Any, expected: str) -> None:
    assert resolve_language(header) == expected


def test_supported_languages_and_default_match_the_i18n_catalog_plan() -> None:
    assert DEFAULT_LANGUAGE == "zh"
    assert SUPPORTED_LANGUAGES == ("zh", "en", "de", "fr", "es", "pt")
    assert len(set(SUPPORTED_LANGUAGES)) == len(SUPPORTED_LANGUAGES)
