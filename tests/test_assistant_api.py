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
            roles=frozenset({"ROLE_AGENT_ADMIN"}),
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

    def start_standard_diagnosis(
        self, context: ScopeContext, order_no: str, question: str, indicator_code, language="zh"
    ):
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
        language="zh",
    ):
        del conversation, conversation_turn_no
        qa_id = "qa_test00000000000000000000000000000001"
        self._qa[qa_id] = {"qa_id": qa_id, "question": question, "status": "queued", "result": None}
        return self._qa[qa_id]

    def get_assistant_qa(self, context: ScopeContext, qa_id: str):
        del context
        return self._qa.get(qa_id)

    def fail_assistant_qa(self, qa_id: str, error_code: str = "QA_FAILED", error_message: str = ""):
        self._qa[qa_id]["status"] = "failed"
        self._qa[qa_id]["error_code"] = error_code
        self._qa[qa_id]["error_message"] = error_message

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


def test_assistant_high_risk_without_order_returns_clarification(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "是不是扣错钱了"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "clarification"
    assert body["missing_fields"] == ["order_no"]
    assert "qa_id" not in body
    assert runtime._qa == {}


def test_order_bound_shortcut_requires_order_context(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    created = client.post(
        "/v1/shortcuts",
        headers=_headers(),
        json={
            "business_entry": "consumer",
            "code": "smart_diagnosis",
            "intent": "order_issue",
            "requires_order": True,
            "labels": {"zh": "智能检测"},
        },
    )
    assert created.status_code == 201, created.text
    shortcut = created.json()
    published = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/publish",
        headers=_headers(),
        json={"expected_revision": shortcut["revision"]},
    )
    assert published.status_code == 200, published.text
    resp = client.post(
        "/v1/assistant/questions",
        json={
            "question": "帮我检测这个订单的充电异常",
            "shortcut_code": "smart_diagnosis",
        },
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "clarification"
    assert body["missing_fields"] == ["order_no"]
    assert runtime._qa == {}


def test_order_bound_shortcut_with_embedded_order_reaches_diagnosis(tmp_path: Path) -> None:
    """Frontend regression (2026-09-16, live H5): the order picker result
    arrives INSIDE the question text, not as the order_no field. The
    requires_order guard only looked at payload.order_no, shadowed the
    embedded-order route (Route 1b), and rejected a perfectly diagnosable
    request with '请先选择需要检测的订单'. The guard must stay silent when
    the text already carries an order id."""
    client, runtime = _client(tmp_path)  # fixture allows 2096164064667852801
    created = client.post(
        "/v1/shortcuts",
        headers=_headers(),
        json={
            "business_entry": "consumer",
            "code": "smart_diagnosis",
            "intent": "order_issue",
            "requires_order": True,
            "labels": {"zh": "智能检测"},
        },
    )
    assert created.status_code == 201, created.text
    shortcut = created.json()
    published = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/publish",
        headers=_headers(),
        json={"expected_revision": shortcut["revision"]},
    )
    assert published.status_code == 200, published.text

    question = "帮我检测（2096164064667852801）这个订单的充电异常"
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": question, "shortcut_code": "smart_diagnosis"},
        headers=_headers(),
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["type"] == "diagnosis"
    assert body["order_no_extracted"] == "2096164064667852801"
    assert runtime.calls == [("2096164064667852801", question)]

    # Unowned embedded order still falls through (no hard 404, no leak).
    runtime.calls.clear()
    unowned = client.post(
        "/v1/assistant/questions",
        json={
            "question": "帮我检测（2099999999999999999）这个订单的充电异常",
            "shortcut_code": "smart_diagnosis",
        },
        headers=_headers(),
    )
    assert unowned.status_code != 404
    assert runtime.calls == []


def test_assistant_qa_poll_failed_exposes_error(tmp_path: Path) -> None:
    """A failed QA job returns the {code, message, retryable} error contract,
    matching the diagnosis poll line — not a null error the frontend must
    guess at (bare '未找到')."""
    client, runtime = _client(tmp_path)
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "新加坡电动巴士"},
        headers=_headers(),
    )
    qa_id = resp.json()["qa_id"]
    runtime.fail_assistant_qa(qa_id, error_message="model provider quota exceeded")
    poll = client.get(f"/v1/assistant/questions/{qa_id}", headers=_headers())
    assert poll.status_code == 200
    body = poll.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "QA_FAILED"
    assert body["error"]["message"] == "model provider quota exceeded"
    assert body["error"]["retryable"] is True
    assert body["result"] is None


def test_assistant_qa_poll_running_keeps_null_error(tmp_path: Path) -> None:
    """Non-terminal statuses keep error: null — the field only appears on failures."""
    client, _ = _client(tmp_path)
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "电动车续航为什么下降"},
        headers=_headers(),
    )
    poll = client.get(f"/v1/assistant/questions/{resp.json()['qa_id']}", headers=_headers())
    assert poll.json()["error"] is None


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


def test_assistant_responses_echo_resolved_language(tmp_path: Path) -> None:
    """Every assistant entry echoes the Accept-Language resolution (L1/#201)."""
    client, _ = _client(tmp_path)

    faq = client.post(
        "/v1/assistant/questions",
        json={"question": "无法拔枪怎么办"},
        headers={**_headers(), "Accept-Language": "en-US,en;q=0.9"},
    )
    assert faq.status_code == 200
    assert faq.json()["type"] == "faq"
    assert faq.json()["language"] == "en"

    qa = client.post(
        "/v1/assistant/questions",
        json={"question": "为什么我的车充满电之后续航里程总是比官方标注少这么多"},
        headers={**_headers(), "Accept-Language": "pt-BR"},
    )
    assert qa.status_code == 202
    assert qa.json()["type"] == "qa"
    assert qa.json()["language"] == "pt"

    poll = client.get(
        f"/v1/assistant/questions/{qa.json()['qa_id']}",
        headers={**_headers(), "Accept-Language": "fr"},
    )
    assert poll.status_code == 200
    assert poll.json()["language"] == "fr"

    listed = client.get("/v1/assistant/questions", headers=_headers())
    assert listed.status_code == 200
    assert listed.json()["type"] == "qa_list"
    assert listed.json()["language"] == "zh"

    diagnosis = client.post(
        "/v1/assistant/questions",
        json={"question": "为什么充电突然停了", "order_no": "2096164064667852801"},
        headers={**_headers(), "Accept-Language": "de"},
    )
    assert diagnosis.status_code == 202
    assert diagnosis.json()["type"] == "diagnosis"
    assert diagnosis.json()["language"] == "de"


def test_assistant_faq_branch_returns_localized_answer(tmp_path: Path) -> None:
    """A zh question with Accept-Language=en still hits FAQ and gets the en entry."""
    client, _ = _client(tmp_path)
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "无法拔枪怎么办"},
        headers={**_headers(), "Accept-Language": "en"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "faq"
    assert body["language"] == "en"
    assert body["question_id"] == "consumer.faq.q010"
    assert body["question"] == "Connector Stuck? Emergency Cable Release Guide"
    assert body["answer"].startswith("Do NOT yank it")


def test_assistant_faq_shortcircuit_matches_multilingual_questions(tmp_path: Path) -> None:
    """Questions in any supported language hit the same entry as zh (L3/#203)."""
    catalog = FAQCatalog.bundled()
    zh_q011 = catalog.answer("consumer", "consumer.faq.q011")["question"]
    cases = {
        "Why did charging stop unexpectedly?": "consumer.faq.q011",
        "Warum hat der Ladevorgang unerwartet gestoppt?": "consumer.faq.q011",
        "Pourquoi la charge s'est-elle arrêtée inopinément ?": "consumer.faq.q011",
        "¿Por qué se detuvo la recarga inesperadamente?": "consumer.faq.q011",
        "Por que o carregamento parou inesperadamente?": "consumer.faq.q011",
        # zh full question still hits via the authoritative title.
        zh_q011: "consumer.faq.q011",
    }
    for question, expected_qid in cases.items():
        client, runtime = _client(tmp_path)
        resp = client.post("/v1/assistant/questions", json={"question": question}, headers=_headers())
        assert resp.status_code == 200, question
        body = resp.json()
        assert body["type"] == "faq", question
        assert body["question_id"] == expected_qid, question
        assert runtime.calls == []
        client.close()


def test_assistant_faq_shortcircuit_ignores_generic_single_tokens(tmp_path: Path) -> None:
    """A lone generic Latin token must not trigger a FAQ hit (L3/#203)."""
    client, runtime = _client(tmp_path)
    for question in ("charging", "Charger", "refund"):
        resp = client.post("/v1/assistant/questions", json={"question": question}, headers=_headers())
        assert resp.status_code == 202, question
        assert resp.json()["type"] == "qa", question
    assert runtime.calls == []


def _publish_shortcut(client: TestClient, body: dict) -> dict:
    created = client.post("/v1/shortcuts", headers=_headers(), json=body)
    assert created.status_code == 201, created.text
    shortcut = created.json()
    published = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/publish",
        headers=_headers(),
        json={"expected_revision": shortcut["revision"]},
    )
    assert published.status_code == 200, published.text
    return shortcut


def test_jump_shortcut_at_entry_returns_clarification_and_creates_no_job(tmp_path: Path) -> None:
    """A jump action belongs to the client, not the assistant entry. If a
    client sends one here anyway (it was supposed to navigate), the answer
    must be plain and must NOT spawn a QA or diagnosis job — otherwise a
    button click silently buys a model run."""
    client, runtime = _client(tmp_path)
    _publish_shortcut(
        client,
        {
            "business_entry": "consumer",
            "code": "report_fault",
            "intent": "report_fault",
            "requires_order": False,
            "labels": {"zh": "故障上报", "en": "Report a Fault"},
            "jump_path": "/charge/pages/faultReport/faultReportList",
        },
    )

    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "我要上报一个故障", "shortcut_code": "report_fault"},
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "clarification"
    assert body["missing_fields"] == []
    # No job of either kind was created.
    assert runtime._qa == {}
    assert runtime.calls == []


def test_jump_shortcut_creates_no_job_even_with_an_owned_order(tmp_path: Path) -> None:
    """The jump guard must precede the order routes: a jump action must not
    start a diagnosis just because an order happened to be attached."""
    client, runtime = _client(tmp_path)  # fixture allows 2096164064667852801
    _publish_shortcut(
        client,
        {
            "business_entry": "consumer",
            "code": "report_fault",
            "intent": "report_fault",
            "requires_order": False,
            "labels": {"zh": "故障上报"},
            "jump_path": "/charge/pages/faultReport/faultReportList",
        },
    )

    resp = client.post(
        "/v1/assistant/questions",
        json={
            "question": "帮我检测（2096164064667852801）这个订单的充电异常",
            "shortcut_code": "report_fault",
        },
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["type"] == "clarification"
    assert runtime.calls == []
    assert runtime._qa == {}


def test_prompt_shortcuts_are_unaffected_by_the_jump_guard(tmp_path: Path) -> None:
    """Regression: the two prompt actions keep their exact behavior."""
    client, runtime = _client(tmp_path)
    _publish_shortcut(
        client,
        {
            "business_entry": "consumer",
            "code": "smart_diagnosis",
            "intent": "order_issue",
            "requires_order": True,
            "labels": {"zh": "智能检测"},
        },
    )

    # Order-bound prompt action without an order: unchanged clarification.
    missed = client.post(
        "/v1/assistant/questions",
        json={"question": "帮我检测这个订单的充电异常", "shortcut_code": "smart_diagnosis"},
        headers=_headers(),
    )
    assert missed.status_code == 200
    assert missed.json()["type"] == "clarification"
    assert missed.json()["missing_fields"] == ["order_no"]
    assert runtime._qa == {}

    # Order-bound prompt action WITH the order: still reaches diagnosis.
    reached = client.post(
        "/v1/assistant/questions",
        json={
            "question": "帮我检测（2096164064667852801）这个订单的充电异常",
            "shortcut_code": "smart_diagnosis",
        },
        headers=_headers(),
    )
    assert reached.status_code == 202, reached.text
    assert reached.json()["type"] == "diagnosis"


def test_unknown_shortcut_code_still_falls_through_to_qa(tmp_path: Path) -> None:
    """An unpublished or unknown code keeps the existing safe fallback: it is
    ignored and the question is handled as an ordinary one."""
    client, runtime = _client(tmp_path)
    question = "为什么我的车充满电之后续航里程总是比官方标注少这么多"
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": question, "shortcut_code": "not_published_yet"},
        headers=_headers(),
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["type"] == "qa"
