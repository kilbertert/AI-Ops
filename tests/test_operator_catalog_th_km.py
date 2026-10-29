"""泰/高棉模型提示已开放；旧FAQ分词的反例与拒绝仍保留。"""

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
def test_the_language_is_promptable(language: str) -> None:
    """模型支持语言与旧分词能力分开。"""
    assert language in SUPPORTED_LANGUAGES, "读的语言也必须在受支持集里"
    assert can_prompt_in(language) is True


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


# --------------------------------------------------------------------------
# A self-contained assistant client. The repo's tests never import one another
# (`tests` is not a package under CI), so this builds the minimum surface the
# route-refusal tests need. (That mistake has been made and corrected three
# times on this workstream; this is the correction for the third.)
# --------------------------------------------------------------------------


class _Caller:
    def resolve(self, token, **kwargs):
        from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

        del kwargs
        subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({"aiops:orders:read"}),
        )


class _Authorizer:
    """Authorizes only the orders it was given; everything else is `not owned`."""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed

    def can_access(self, context, order_no: str) -> bool:
        del context
        return order_no in self.allowed


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        from aiops_diagnostics.faq import PlatformRoleRecord

        return (PlatformRoleRecord("B-1", c_user_id, tenant_id, "consumer"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


class _Jobs:
    """Records every entry point that starts a job, and starts none."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.qa_calls: list[str] = []
        self.classified = None

    def classify_lightweight(self, question, *, language="zh", tenant_id=None):
        del question, language, tenant_id
        return self.classified

    def start_standard_diagnosis(
        self,
        context,
        order_no,
        question,
        indicator_code,
        language="zh",
        *,
        conversation_turn=None,
        business_entry: str | None = None,
    ):
        del context, indicator_code, conversation_turn, business_entry
        self.calls.append((order_no, question))
        raise AssertionError("边界之后仍然启动了诊断作业")

    def start_assistant_qa(self, context, question, **kwargs):
        del context, kwargs
        self.qa_calls.append(question)
        raise AssertionError("边界之后仍然启动了问答作业")

    def shutdown(self) -> None:
        pass


def _client(tmp_path, *, allowed_orders: set[str] | None = None):
    from fastapi.testclient import TestClient

    from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver
    from aiops_diagnostics.gateway_api import create_gateway_app
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway_store import GatewayStore

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    runtime = _Jobs()
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=_Caller(),
        order_authorizer=_Authorizer(allowed_orders or {"2096164064667852801"}),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app), runtime


_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}
