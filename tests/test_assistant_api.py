from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.caller_auth import CALLER_AUTH_FORBIDDEN, CallerAuthError
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import _extract_order_no, create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


class _Caller:
    """Mimics an introspection resolver: a token named "narrow" may only hold
    aiops:faq:read, everything else is granted the requested scope. Requests
    for aiops:diagnoses:write under "narrow" raise CALLER_AUTH_FORBIDDEN, like
    a real introspection server. This lets the auth-downgrade regression prove
    that a faq:read-only caller CANNOT use the diagnosis branch of the
    assistant endpoint."""

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        if token == "narrow" and required_scope not in {"aiops:faq:read"}:
            raise CallerAuthError("insufficient scope", code=CALLER_AUTH_FORBIDDEN)
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
        self._qa = {}  # qa_id -> record

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

    def get_standard_diagnosis(self, context: ScopeContext, diagnosis_id: str):
        del context, diagnosis_id
        return None

    def start_assistant_qa(
        self,
        context: ScopeContext,
        question: str,
        *,
        conversation=None,
        conversation_turn_no=None,
    ):
        del conversation, conversation_turn_no
        qa_id = "qa_test00000000000000000000000000000001"
        self._qa[qa_id] = {"qa_id": qa_id, "question": question, "status": "queued", "result": None}
        return self._qa[qa_id]

    def get_assistant_qa(self, context: ScopeContext, qa_id: str):
        del context
        return self._qa.get(qa_id)

    def list_assistant_qa(self, context: ScopeContext, *, limit: int = 50):
        del context
        items = [
            {"qa_id": k, "question": v["question"], "status": v["status"], "created_at": "t"}
            for k, v in self._qa.items()
        ]
        return items[:limit]


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
    # "无法拔枪" must keyword-hit q010 (拔不出充电枪), NOT q007 or another
    # title merely because they share the generic tail "怎么办".
    resp = client.post("/v1/assistant/questions", json={"question": "无法拔枪怎么办"}, headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "faq"
    assert body["question_id"] == "consumer.faq.q010"
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


def test_assistant_generic_returns_queued_job(tmp_path: Path) -> None:
    """No order_no and no FAQ hit -> real QA job created (202) then pollable."""
    client, runtime = _client(tmp_path)
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "为什么我的车充满电之后续航里程总是比官方标注少这么多"},
        headers=_headers(),
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["type"] == "qa"
    assert body["qa_id"].startswith("qa_")
    assert body["status"] == "queued"
    assert body["retry_after_ms"] == 1000
    assert body["result"] is None
    # poll returns the same job (still queued in stub)
    poll = client.get(f"/v1/assistant/questions/{body['qa_id']}", headers=_headers())
    assert poll.status_code == 200
    assert poll.json()["qa_id"] == body["qa_id"]
    assert poll.json()["status"] == "queued"


def test_assistant_qa_poll_unknown_is_404(tmp_path: Path) -> None:
    """Polling a qa_id outside the caller scope is refused."""
    client, _ = _client(tmp_path)
    poll = client.get("/v1/assistant/questions/qa_nonexistent000000000000000000000001", headers=_headers())
    assert poll.status_code == 404
    assert poll.json()["error"]["code"] == "QA_NOT_FOUND"


def test_diagnosis_poll_with_qa_id_gets_pointed_hint(tmp_path: Path) -> None:
    """Polling the order-diagnosis endpoint with a qa_ id (the frontend mistake
    that produced bare 'diagnosis not found') returns a 404 whose message
    names the correct poll path for the general-question job."""
    client, _ = _client(tmp_path)
    resp = client.get(
        "/v1/standard/diagnoses/qa_9a5d1f49ea0743fba1234567890abcd",
        headers=_headers(),
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "DIAGNOSIS_NOT_FOUND"
    assert "/v1/assistant/questions/qa_9a5d1f49ea0743fba1234567890abcd" in body["error"]["message"]


def test_diagnosis_poll_with_unknown_dx_id_keeps_plain_404(tmp_path: Path) -> None:
    """An unknown dx_ id keeps the plain 'diagnosis not found' 404."""
    client, _ = _client(tmp_path)
    resp = client.get(
        "/v1/standard/diagnoses/dx_nonexistent000000000000000000000001",
        headers=_headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "diagnosis not found"


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


def test_assistant_faq_read_only_token_cannot_use_diagnosis_branch(tmp_path: Path) -> None:
    """Auth-downgrade regression: a caller holding only aiops:faq:read (which
    WOULD pass the pure-FAQ faq_identity) must NOT be able to start an order
    diagnosis through the assistant endpoint. The assistant entry point
    requires aiops:diagnoses:write, like /v1/standard/diagnoses."""
    client, runtime = _client(tmp_path)
    headers = {"Authorization": "Bearer narrow", "X-Business-Entry": "consumer"}
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "你好", "order_no": "2096164064667852801"},
        headers=headers,
    )
    assert resp.status_code in (401, 403)
    assert runtime.calls == []


def test_assistant_history_list_separate_from_diagnoses(tmp_path: Path) -> None:
    """After asking a general question, the QA list shows it (typed qa_list),
    distinct from the order-diagnosis history endpoint."""
    client, runtime = _client(tmp_path)
    client.post(
        "/v1/assistant/questions",
        json={"question": "为什么我的车充满电之后续航里程总是比官方标注少这么多"},
        headers=_headers(),
    )
    lst = client.get("/v1/assistant/questions", headers=_headers())
    assert lst.status_code == 200
    body = lst.json()
    assert body["type"] == "qa_list"
    assert body["count"] == 1
    assert body["questions"][0]["question"].startswith("为什么我的车")


def test_extract_order_no_recognizes_owned_19digit(tmp_path: Path) -> None:
    """A 19-digit standalone token is extracted (order id pattern)."""
    assert _extract_order_no("查到啦，订单 2096164064667852801 怎么还没退押金") == "2096164064667852801"


def test_extract_order_no_ignores_short_digits_and_phone(tmp_path: Path) -> None:
    """Pure-digit runs shorter than 15 chars are NOT treated as orders."""
    assert _extract_order_no("我的手机号是13800001111，帮我查一下") is None
    assert _extract_order_no("价格是50元") is None


def test_assistant_text_embedded_owned_order_routes_to_diagnosis(tmp_path: Path) -> None:
    """A question containing the caller's own order id routes to diagnosis."""
    client, runtime = _client(tmp_path)  # fixture allows 2096164064667852801
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "订单 2096164064667852801 为什么充电突然停了"},
        headers=_headers(),
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["type"] == "diagnosis"
    assert body["order_no"] == "2096164064667852801"
    assert body["order_no_extracted"] == "2096164064667852801"
    assert runtime.calls == [("2096164064667852801", "订单 2096164064667852801 为什么充电突然停了")]


def test_assistant_text_embedded_unowned_order_falls_through(tmp_path: Path) -> None:
    """A question with an order id the caller does NOT own falls through to the
    general answer (NOT a hard 404), because no ownership was asserted."""
    client, runtime = _client(tmp_path, allowed_orders={"other-only"})
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "订单 2096164064667852801 怎么还没退款"},
        headers=_headers(),
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["type"] == "qa"
    assert runtime.calls == []  # no diagnosis started
