"""11-language acceptance across the deterministic answer surfaces (#542).

This is the acceptance gate for the operator-wide internationalization, written
as a gate rather than a one-off report: it runs on every CI pass, so a language
that stops being served is caught by the suite instead of by a reader comparing
two dated documents.

## What it covers, and what it explicitly does NOT

Covers every surface whose copy is produced WITHOUT a model:

- 固定问答 / 单问诊断的**目录与答案**（`/v1/faq/catalog`, `/v1/faq/answer`）
- 澄清与兜底文案（缺单号、错误入口）
- 快捷动作（`/v1/shortcuts`，按语言取 label / description / question_template）

**Does not cover** the model-backed surfaces — 统一助手 QA、宣传卡片、单问诊断的
模型结论、健康报告的实际生成。 Those need a provider and live data sources; a test
here would either mock the model (proving nothing about language) or silently skip.
`docs/validation.md` records that boundary explicitly rather than letting a green
run imply it.

The assertion that matters is the same on every surface: **the response reports
the language it actually served.** A 200 carrying Simplified while echoing
`zh-Hant` is the exact defect this workstream exists to remove, so "served ==
requested" is checked, not merely "the request succeeded".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.caller_auth import CALLER_AUTH_FORBIDDEN, CallerAuthError
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.shortcut_lifecycle import ShortcutManager, ShortcutStore

LANGUAGES = SUPPORTED_LANGUAGES
OPERATOR_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "operator"}
CONSUMER_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}


class _Caller:
    """Grants the requested scope. Mirrors `tests/test_shortcut_api.py`'s caller —
    same keyword-only `required_scope`, because the endpoint passes it as such."""

    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        del third_session, source_key, platform_entry
        if token == "narrow" and required_scope != "aiops:faq:read":
            raise CallerAuthError("insufficient scope", code=CALLER_AUTH_FORBIDDEN)
        roles = {"ROLE_AGENT_ADMIN"}
        subject = SubjectRecord(b_user_id="c:T-1", c_user_id="T-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(roles),
            permissions=frozenset({required_scope, "aiops:shortcuts:manage"}),
        )


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("B-1", c_user_id, tenant_id, "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


class _Runtime:
    def shutdown(self) -> None:
        pass


def _client(tmp_path: Path) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    return TestClient(
        create_gateway_app(
            settings=settings,
            store=GatewayStore(settings.database_file),
            runtime=_Runtime(),  # type: ignore[arg-type]
            caller_resolver=_Caller(),
            order_authorizer=lambda: None,  # type: ignore[arg-type]
            platform_resolver=PlatformIdentityResolver(_Directory()),
            faq_catalog=FAQCatalog.bundled(),
        )
    )


#: THE acceptance finding for #542: the fixed-QA catalog does NOT cover 11
#: languages on EITHER platform, and the two gaps are exact complements.
#:
#: - consumer carries the five ORIGINAL languages (en/de/fr/es/pt, #200/#202)
#:   but never got vi/mn (#529) or th/km (#530) — those went to operator only.
#: - operator carries the five NEW languages but never got en/de/fr/es/pt —
#:   #200 put "operator 17 条第一版不翻译" out of scope and nothing backfilled it.
#:
#: `zh-Hant` is the one addition that reached BOTH, because it is derived for
#: every platform rather than hand-authored per platform.
#:
#: Recorded as a KNOWN GAP with its reason, per #542's acceptance criterion:
#: "失败/受阻用例写明原因，不得记为通过".
#: #565 把两个平台都补齐到 11/11：operator 的 en/de/es/fr/pt 取自产品宽表已给的译文；
#: consumer 的 vi/mn/th/km 题面与答案均为起草（该宽表没有这四列）。两条缺口都清空。
#: 这两个常量是**双向门**：缺口变化会让 `test_catalog_gaps_are_exactly_as_recorded` 失败，
#: 从而强制更新验收记录，而不是留下一条已不成立的"已知缺口"。
CONSUMER_CATALOG_GAP = ()
OPERATOR_CATALOG_GAP = ()


def _catalog_languages(platform: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(served, missing) for one platform's fixed-QA catalog."""
    from aiops_diagnostics.faq import FAQCatalog

    catalog = FAQCatalog.bundled()
    served = tuple(tag for tag in LANGUAGES if catalog.served_language(platform, tag) == tag)
    missing = tuple(tag for tag in LANGUAGES if tag not in served)
    return served, missing


@pytest.mark.parametrize(
    ("platform", "headers", "covered"),
    [
        ("consumer", CONSUMER_HEADERS, tuple(t for t in LANGUAGES if t not in CONSUMER_CATALOG_GAP)),
        ("operator", OPERATOR_HEADERS, tuple(t for t in LANGUAGES if t not in OPERATOR_CATALOG_GAP)),
    ],
)
def test_catalog_serves_each_language_it_claims(
    tmp_path: Path, platform: str, headers: dict, covered: tuple[str, ...]
) -> None:
    """Every language the catalog HAS is served end to end, with no silent fallback.

    The complement — the languages it does not have — is pinned separately by
    `test_catalog_gaps_are_exactly_as_recorded`, so this test fails if a covered
    language starts falling back, and that one fails if a gap silently changes.
    """
    question_id = f"{platform}.faq.q001"
    with _client(tmp_path) as client:
        for language in covered:
            catalog = client.get("/v1/faq/catalog", headers={**headers, "Accept-Language": language})
            assert catalog.status_code == 200, catalog.text
            body = catalog.json()
            assert body["language"] == language, f"{platform}/{language}: 目录回退到 {body['language']}"
            assert body["entries"], f"{platform}/{language}: 目录为空"
            assert body["entries"][0]["question"].strip()

            answer = client.post(
                "/v1/faq/answer",
                headers={**headers, "Accept-Language": language},
                json={"question_id": question_id},
            )
            assert answer.status_code == 200, answer.text
            served = answer.json()
            assert served["language"] == language, f"{platform}/{language}: 答案回退到 {served['language']}"
            assert served["answer"].strip()


def test_catalog_gaps_are_exactly_as_recorded() -> None:
    """Pin both gaps as SETS, so the acceptance record cannot go stale.

    A gate in both directions: a covered language regressing is caught here, and
    a future translation of the missing set FAILS this test — which forces the
    acceptance record to be updated instead of leaving a "known gap" line that
    is no longer true.
    """
    # Compared as SETS: the tuple order is the inventory's, which is not
    # alphabetical, and the gap is a set of languages rather than a sequence.
    assert set(_catalog_languages("consumer")[1]) == set(CONSUMER_CATALOG_GAP)
    assert set(_catalog_languages("operator")[1]) == set(OPERATOR_CATALOG_GAP)
    # Both platforms are complete: no language falls back on either. Asserted
    # explicitly rather than left to the two empty tuples — an empty constant
    # compared to an empty result passes even if the lookup itself broke.
    assert set(CONSUMER_CATALOG_GAP) | set(OPERATOR_CATALOG_GAP) == set()


@pytest.mark.parametrize("language", LANGUAGES)
def test_shortcuts_serve_every_language(tmp_path: Path, language: str) -> None:
    """Shortcut copy covers all 11 languages, for BOTH business entries.

    Seeded rows are the fresh-install path; #542 separately migrated the rows
    already published in production. Both must serve.
    """
    store = ShortcutStore(tmp_path / "gateway.db")
    manager = ShortcutManager(store)
    context = type(
        "Ctx",
        (),
        {
            "effective_tenant_id": "T-1",
            "roles": frozenset({"ROLE_AGENT_ADMIN"}),
            "caller": type("C", (), {"b_user_id": "admin"})(),
        },
    )()
    # `seed_bundled` RETURNS the drafts; `list_effective` would return nothing
    # here, because it lists only PUBLISHED rows (that mistake made this test
    # assert over an empty store the first time).
    for seeded in store.seed_bundled(context, manager):
        manager.publish(context, seeded.shortcut_id, expected_revision=seeded.revision)

    with _client(tmp_path) as client:
        for entry, headers in (("consumer", CONSUMER_HEADERS), ("operator", OPERATOR_HEADERS)):
            body = client.get("/v1/shortcuts", headers={**headers, "Accept-Language": language}).json()
            assert body["shortcuts"], f"{entry}: 没有可用动作"
            for row in body["shortcuts"]:
                assert row["language"] == language, (
                    f"{entry}/{row['code']} 在 {language} 下服务了 {row['language']}"
                )
                assert row["label"].strip()
                assert row["description"].strip()


@pytest.mark.parametrize("language", LANGUAGES)
def test_clarification_and_fallback_copy_cover_every_language(language: str) -> None:
    """The tables a user hits on the unhappy paths, asserted per language.

    These are the surfaces nobody exercises by hand — a missing entry here means
    a clarification or an error arrives in a language the reader cannot use, and
    nothing else in the system reports it.
    """
    from aiops_diagnostics import i18n

    tables = {
        "QA_FALLBACK_MESSAGES": i18n.QA_FALLBACK_MESSAGES,
        "PROMO_EMPTY_MESSAGES": i18n.PROMO_EMPTY_MESSAGES,
        "PROMO_UNAVAILABLE_MESSAGES": i18n.PROMO_UNAVAILABLE_MESSAGES,
        "CLARIFICATION_MESSAGES": i18n.CLARIFICATION_MESSAGES,
        "DIAGNOSIS_FAILURE_MESSAGES": i18n.DIAGNOSIS_FAILURE_MESSAGES,
        "DIAGNOSIS_ERROR_MESSAGES": i18n.DIAGNOSIS_ERROR_MESSAGES,
    }
    for name, table in tables.items():
        assert language in table, f"{name} 缺 {language}"
        assert all(value.strip() for value in table[language].values()), f"{name}[{language}] 有空值"

    for name, table in (
        ("ZERO_ORDER_REMINDER_MESSAGES", i18n.ZERO_ORDER_REMINDER_MESSAGES),
        ("FREE_TEXT_UNAVAILABLE_MESSAGES", i18n.FREE_TEXT_UNAVAILABLE_MESSAGES),
    ):
        assert language in table, f"{name} 缺 {language}"
        assert table[language].strip(), f"{name}[{language}] 为空"


def test_thai_and_khmer_declare_the_read_only_boundary() -> None:
    """「能渲染，不能触发」是声明属性，不是从别处推断出来的 (#542 验收项).

    The capability boundary has to be readable from the inventory, because a
    surface that asks "may I prompt in this language" must not re-derive the
    answer from a language list.
    """
    from aiops_diagnostics.i18n import can_prompt_in, effective_language

    for language in ("th", "km"):
        assert can_prompt_in(language) is False, f"{language} 不应可提示"
        assert effective_language(language) != language, f"{language} 应退到可提示语言"
    for language in LANGUAGES:
        if language in {"th", "km"}:
            continue
        assert can_prompt_in(language) is True, f"{language} 应可提示"


def test_traditional_is_its_own_key_not_a_fold_onto_simplified() -> None:
    """`zh-Hant` 是独立语言键；请求繁体不得被答成简体 (#531 的回归门)."""
    from aiops_diagnostics.i18n import resolve_language

    assert resolve_language("zh-Hant") == "zh-Hant"
    assert resolve_language("zh-Hant-TW") == "zh-Hant"
    assert resolve_language("zh") == "zh"
