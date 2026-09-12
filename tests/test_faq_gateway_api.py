from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


class _Caller:
    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        subject = SubjectRecord(b_user_id="c:C-1", c_user_id="C-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
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
