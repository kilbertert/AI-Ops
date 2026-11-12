from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.conversation.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway.gateway_api import create_gateway_app
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.identity.capability import CAPABILITY_ASSERTED
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord


class _Caller:
    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        subject = SubjectRecord(b_user_id="c:C-1", c_user_id="C-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
            capability_source=CAPABILITY_ASSERTED,
        )


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("B-1", "C-1", "T-1", "admin"),)

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
            platform_resolver=PlatformIdentityResolver(_Directory()),
            faq_catalog=FAQCatalog.bundled(),
        )
    )


def test_faq_api_returns_recommendations_without_answers(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get(
            "/v1/faq/recommendations",
            headers={"Authorization": "Bearer service", "X-Business-Entry": "consumer"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["platform"] == "consumer"
    assert body["available_platforms"] == ["consumer", "operator"]
    assert len(body["recommendations"]) == 28
    assert "answer" not in body["recommendations"][0]


def test_faq_answer_is_platform_scoped_and_sync(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        answer = client.post(
            "/v1/faq/answer",
            headers={"Authorization": "Bearer service", "X-Business-Entry": "consumer"},
            json={"question_id": "consumer.faq.q001"},
        )
        cross_platform = client.post(
            "/v1/faq/answer",
            headers={"Authorization": "Bearer service", "X-Business-Entry": "consumer"},
            json={"question_id": "operator.faq.q001"},
        )
    assert answer.status_code == 200
    assert answer.json()["format"] == "text"
    assert answer.json()["answer"]
    assert "job_id" not in answer.json()
    assert cross_platform.status_code == 404
    assert cross_platform.json()["error"]["code"] == "FAQ_NOT_FOUND"


def test_faq_api_rejects_frontend_platform_field(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/v1/faq/answer",
            headers={"Authorization": "Bearer service", "X-Business-Entry": "consumer"},
            json={"question_id": "consumer.faq.q001", "platform": "operator"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_faq_api_requires_service_authentication(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/v1/faq/catalog", headers={"X-Business-Entry": "consumer"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ACCESS_TOKEN_REQUIRED"


def test_faq_api_ignores_untrusted_platform_query_parameter(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get(
            "/v1/faq/recommendations?platform=operator",
            headers={"Authorization": "Bearer service", "X-Business-Entry": "consumer"},
        )
    assert response.status_code == 200
    assert response.json()["platform"] == "consumer"


def test_faq_responses_echo_resolved_language(tmp_path: Path) -> None:
    """FAQ read endpoints resolve Accept-Language and echo it (L1/#201)."""
    with _client(tmp_path) as client:
        recommendations = client.get(
            "/v1/faq/recommendations",
            headers={
                "Authorization": "Bearer service",
                "X-Business-Entry": "consumer",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        catalog = client.get(
            "/v1/faq/catalog",
            headers={
                "Authorization": "Bearer service",
                "X-Business-Entry": "consumer",
                "Accept-Language": "fr",
            },
        )
        answer = client.post(
            "/v1/faq/answer",
            headers={"Authorization": "Bearer service", "X-Business-Entry": "consumer"},
            json={"question_id": "consumer.faq.q001"},
        )
    assert recommendations.status_code == 200
    assert recommendations.json()["language"] == "en"
    assert catalog.status_code == 200
    assert catalog.json()["language"] == "fr"
    # A missing Accept-Language header falls back to the default language.
    assert answer.status_code == 200
    assert answer.json()["language"] == "zh"


def test_faq_endpoints_return_localized_content(tmp_path: Path) -> None:
    """FAQ read endpoints return localized titles/answers per Accept-Language (L2/#202)."""
    with _client(tmp_path) as client:
        recommendations = client.get(
            "/v1/faq/recommendations",
            headers={
                "Authorization": "Bearer service",
                "X-Business-Entry": "consumer",
                "Accept-Language": "en",
            },
        )
        assert recommendations.status_code == 200
        assert recommendations.json()["language"] == "en"
        assert recommendations.json()["recommendations"][0]["title"].startswith("Differences between AC")

        answer = client.post(
            "/v1/faq/answer",
            headers={
                "Authorization": "Bearer service",
                "X-Business-Entry": "consumer",
                "Accept-Language": "en",
            },
            json={"question_id": "consumer.faq.q001"},
        )
        assert answer.status_code == 200
        body = answer.json()
        assert body["language"] == "en"
        assert body["question"] == "Differences between AC, DC & HPC Chargers"
        assert body["answer"].startswith("Charging points")

        catalog = client.get(
            "/v1/faq/catalog",
            headers={
                "Authorization": "Bearer service",
                "X-Business-Entry": "consumer",
                "Accept-Language": "pt-BR",
            },
        )
        assert catalog.json()["language"] == "pt"
        assert catalog.json()["entries"][0]["question"] == (
            "Diferença entre carregador rápido, ultra e lento"
        )


def test_faq_endpoints_serve_traditional_for_zh_hant(tmp_path: Path) -> None:
    """`zh-Hant` is its own language key, not a fold onto `zh` (#531).

    The ticket's headline claim is "a `zh-Hant` request really gets Traditional",
    and this is the endpoint where that is observable. Both directions are
    asserted: the operator catalog serves the Traditional question and answer,
    and a plain `zh` request is unchanged — so the tag selects a script rather
    than replacing one.
    """
    with _client(tmp_path) as client:
        catalog = client.get(
            "/v1/faq/catalog",
            headers={
                "Authorization": "Bearer service",
                "X-Business-Entry": "operator",
                "Accept-Language": "zh-Hant",
            },
        )
        assert catalog.status_code == 200, catalog.text
        body = catalog.json()
        assert body["language"] == "zh-Hant"
        entry = body["entries"][0]
        assert entry["question"] == "新建充電站的標準建站流程與核心業務配置是什麼？"

        answer = client.post(
            "/v1/faq/answer",
            headers={
                "Authorization": "Bearer service",
                "X-Business-Entry": "operator",
                "Accept-Language": "zh-Hant",
            },
            json={"question_id": "operator.faq.q001"},
        )
        assert answer.status_code == 200, answer.text
        answered = answer.json()
        assert answered["language"] == "zh-Hant"
        assert answered["question"] == "新建充電站的標準建站流程與核心業務配置是什麼？"
        assert answered["answer"].startswith("在平臺中新建充電場站")

        # The control, on BOTH endpoints: the Simplified authority is unchanged
        # by the new key, so what the `zh-Hant` request got above is a selected
        # script and not a blanket relabelling of every response.
        zh_headers = {
            "Authorization": "Bearer service",
            "X-Business-Entry": "operator",
            "Accept-Language": "zh",
        }
        simplified = client.get("/v1/faq/catalog", headers=zh_headers).json()
        assert simplified["language"] == "zh"
        assert simplified["entries"][0]["question"] == "新建充电站的标准建站流程与核心业务配置是什么？"

        simplified_answer = client.post(
            "/v1/faq/answer", headers=zh_headers, json={"question_id": "operator.faq.q001"}
        ).json()
        assert simplified_answer["question"] == "新建充电站的标准建站流程与核心业务配置是什么？"
        assert simplified_answer["answer"].startswith("在平台中新建充电场站")


# ── 平台的语言值（#712）────────────────────────────────────────────────────
#
# 端到端的那一半：`test_i18n.py` 只证解析函数认这些值，这一组证**HTTP 面**
# 拿它们换回了对应语言的正文 —— 解析对了但接线丢了，是两件事。


@pytest.mark.parametrize(
    ("platform_value", "served"),
    [
        # 公司平台 `sys_language.lan` 的九行，逐字（后台「语言配置」页）。
        ("zh-CN", "zh"),
        ("en", "en"),
        ("portuguese", "pt"),
        ("spanish", "es"),
        ("th", "th"),
        ("vi", "vi"),
        ("mn", "mn"),
        ("km", "km"),
        ("zh-HK", "zh-Hant"),
    ],
)
def test_recommendations_answer_every_platform_language_value(
    tmp_path: Path, platform_value: str, served: str
) -> None:
    """平台九行的每一个值，都要换回它自己的语言正文。

    判据是三条一起：HTTP 200、`language` 等于该语言、正文**确实是**该语言
    （不是回显了请求语言却给了中文 —— 那正是这条缺陷原本的形态）。
    """
    with _client(tmp_path) as client:
        response = client.get(
            "/v1/faq/recommendations",
            headers={
                "Authorization": "Bearer service",
                "X-Business-Entry": "consumer",
                "Accept-Language": platform_value,
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["language"] == served, body
    titles = [item["title"] for item in body["recommendations"]]
    assert titles, body
    han = re.compile(r"[㐀-䶿一-鿿]")
    if served == "zh-Hant":
        # 繁体：正文里必须有繁体专用字，且没有简体专用字。
        joined = "".join(titles)
        assert "體" in joined or "標" in joined or "麼" in joined, titles[0]
        assert "体" not in joined and "标" not in joined, titles[0]
    elif not served.startswith("zh"):
        # 非中文：一个汉字都不能有 —— 少了这条，一份只有中文的目录也能
        # 让「九行各取各的」看起来在跑。
        for title in titles:
            assert not han.search(title), f"{served} 的题面里出现了汉字：{title}"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/faq/recommendations"),
        ("GET", "/v1/faq/catalog"),
        ("POST", "/v1/faq/answer"),
        ("GET", "/v1/assistant/starters"),
        ("GET", "/v1/shortcuts"),
        ("GET", "/v1/banner"),
    ],
)
def test_localized_copy_language_is_logged(tmp_path: Path, caplog, method: str, path: str) -> None:
    """确定性文案面要留下「客户端发了什么头、解析成什么」的一行记录。

    拦截的错误：日后重构丢掉这条 ``_LOGGER.info``、或把**任何一个**端点换回
    不记录的 ``request_language``——这几个面没有持久语言记录（chat 路径有
    run 行），日志是定位「客户端发错 Accept-Language」的唯一线索，静默丢失
    要等下次排障才被发现。六个面逐个断言：行存在、带原始头与解析值。
    只断言日志不断言响应码：starters/shortcuts/banner 在这个最小夹具里可能
    因缺下游件而 4xx/5xx，而依赖先于处理器执行，日志照样落地——本测试的
    契约是「日志」，不是业务成功。
    """
    caplog.set_level(logging.INFO, logger="aiops.gateway")
    headers = {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}
    body = {"question_id": "consumer.faq.q001"} if method == "POST" else None
    with _client(tmp_path) as client:
        client.request(method, path, headers={**headers, "Accept-Language": "en"}, json=body)
        client.request(method, path, headers=headers, json=body)
    records = [
        r.message
        for r in caplog.records
        if r.name == "aiops.gateway" and "localized_copy_language" in r.message
    ]
    assert len(records) == 2
    assert f"path={path}" in records[0]
    assert "accept_language='en'" in records[0] and "resolved=en" in records[0]
    assert "accept_language=None" in records[1] and "resolved=zh" in records[1]
