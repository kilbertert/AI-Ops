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


class _Authorizer:
    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed

    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        return order_no in self.allowed


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def shutdown(self) -> None:
        pass

    def start_standard_diagnosis(self, context: ScopeContext, order_no: str, question: str, indicator_code):
        del context, indicator_code
        self.calls.append((order_no, question))
        return {
            "diagnosis_id": "dx_test000000000000000000000000000001",
            "order_no": order_no,
            "question": question,
            "indicator_code": None,
            "status": "queued",
            "result": None,
            "error_code": None,
            "error_message": None,
            "created_at": "2026-09-07T00:00:00+00:00",
            "updated_at": "2026-09-07T00:00:00+00:00",
            "completed_at": None,
        }


def _client(tmp_path: Path, *, allowed_orders: set[str] | None = None) -> tuple[TestClient, _Runtime]:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    runtime = _Runtime()
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


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}


def test_assistant_faq_shortcircuit_returns_sync_answer(tmp_path: Path) -> None:
    """FAQ-keyword hit returns a deterministic, zero-order sync answer."""
    client, _ = _client(tmp_path)
    # "无法拔枪" should keyword-hit a FAQ title (q010).
    resp = client.post("/v1/assistant/questions", json={"question": "无法拔枪怎么办"}, headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "faq"
    assert body["question_id"].startswith("consumer.faq.")
    assert body["answer"]


def test_assistant_explicit_order_routes_to_diagnosis(tmp_path: Path) -> None:
    """Explicit order_no is honored as a diagnosis (not reclassified)."""
    client, runtime = _client(tmp_path)
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "你好", "order_no": "2096164064667852801"},
        headers=_headers(),
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["type"] == "diagnosis"
    assert body["diagnosis_id"] == "dx_test000000000000000000000000000001"
    assert runtime.calls == [("2096164064667852801", "你好")]


def test_assistant_explicit_order_unowned_returns_404(tmp_path: Path) -> None:
    """Order outside the caller's scope is refused (no reclassification, no leak)."""
    client, runtime = _client(tmp_path, allowed_orders={"other-only"})
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "你好", "order_no": "2096164064667852801"},
        headers=_headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert runtime.calls == []


def test_assistant_generic_returns_queued_placeholder(tmp_path: Path) -> None:
    """No order_no and no FAQ hit -> queued generic answer (T3 wires the job)."""
    client, runtime = _client(tmp_path)
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "为什么我的车充满电之后续航里程总是比官方标注少这么多"},
        headers=_headers(),
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["type"] == "qa"
    assert body["status"] == "queued"
    assert body["retry_after_ms"] == 1000
    assert body["result"] is None
    assert runtime.calls == []


def test_assistant_invalid_body_is_422(tmp_path: Path) -> None:
    """Extra/unknown fields or empty question are rejected (extra=forbid)."""
    client, _ = _client(tmp_path)
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "", "order_no": "abc"},
        headers=_headers(),
    )
    assert resp.status_code == 422
    resp2 = client.post(
        "/v1/assistant/questions",
        json={"question": "hi", "platform": "consumer"},
        headers=_headers(),
    )
    assert resp2.status_code == 422


def test_assistant_missing_auth_is_401(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.post("/v1/assistant/questions", json={"question": "hi"}, headers={})
    assert resp.status_code == 401
