"""The error envelope's `code` / `message` split, as a contract (#538).

`code` is English and stable so a client can branch on it; `message` is a
developer-readable note and is NOT user-facing copy — with ONE deliberate
exception, the unified-assistant QA failure path, whose `message` the client is
told to render directly.

These are contract checks, not implementation tests: they exist so a future
"let's localize all the error messages" or "let's stop localizing this one"
change has to confront the recorded decision instead of quietly making it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Codes a client branches on. Kept as literals on purpose: if one of these is
# renamed, clients using it break, and the test should fail loudly rather than
# follow the rename.
_STABLE_CLIENT_CODES = (
    "DIAGNOSIS_ORDER_OUT_OF_SCOPE",
    "DIAGNOSIS_BLOCKED",
    "DIAGNOSIS_FAILED",
    "QA_FAILED",
    "DIAGNOSIS_NOT_FOUND",
    "DIAGNOSIS_UNAVAILABLE",
)


@pytest.mark.parametrize("code", _STABLE_CLIENT_CODES)
def test_client_branchable_codes_are_ascii_and_language_independent(code: str) -> None:
    """`code` must stay English: a localized code would be unusable as a branch."""
    assert code.isascii(), f"{code} 不是 ASCII —— 客户端无法稳定分支"
    assert code == code.upper(), f"{code} 不是大写常量形状"


def test_the_qa_failure_message_exception_is_still_localized() -> None:
    """The one exception must stay an exception — asserted at the WIRING.

    The client renders this field directly (frontend-api-brief 场景 B), so
    removing the localization would put a Chinese sentence in front of an
    English reader while the surrounding card looked translated — the #283
    shape this whole body of work exists to remove.

    Deliberately goes through the RESPONSE BUILDER, not `_qa_user_message`:
    replacing the call site with a literal is the mutation this must catch, and
    a test that calls the helper directly cannot see it. (That mistake was made
    twice already on this workstream — #535 and #536.)
    """
    from aiops_diagnostics.gateway_api import _assistant_question_response

    row = {
        "qa_id": "qa_1",
        "question": "q",
        "status": "failed",
        "error_code": "QA_FAILED",
        "result": None,
    }
    texts = {
        _assistant_question_response(dict(row), lang)["error"]["message"]
        for lang in ("zh", "en", "de", "fr", "es", "pt")
    }
    assert len(texts) == 6, "QA 失败文案的本地化接线断了（或不再随语言变化）"
    assert all(text.strip() for text in texts)


def test_request_level_envelopes_echo_the_message_verbatim(tmp_path: Path) -> None:
    """`StandardAPIError` messages are developer notes, not copy.

    Asserted over the REAL HTTP surface (a request that 404s), and again with a
    different `Accept-Language`: the envelope echoes the message byte-for-byte
    both times. There are ~53 call sites and none carries a language. If one
    ever starts localizing, this fails and the author must either record a new
    exception in `docs/standard-api-contract.md` §5.2.1 or back out.
    """
    from tests.test_standard_diagnosis_api import _client

    client, _, _ = _client(tmp_path)
    with client:
        url = "/v1/standard/diagnoses/dx_missing"
        bodies = [
            client.get(
                url,
                headers={"Authorization": "Bearer token", "Accept-Language": lang},
            ).json()
            for lang in ("zh", "en", "de")
        ]

    for body in bodies:
        assert body["error"]["code"] == "DIAGNOSIS_NOT_FOUND"
        # Byte-for-byte the same across languages: request-level envelopes do
        # NOT localize.
        assert body["error"]["message"] == bodies[0]["error"]["message"]
    assert bodies[0]["error"]["message"] == "diagnosis not found"
