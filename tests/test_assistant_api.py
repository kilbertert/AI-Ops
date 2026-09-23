from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.caller_auth import CALLER_AUTH_FORBIDDEN, CallerAuthError
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import (
    STANDARD_DIAGNOSIS_SCOPE,
    _extract_order_no,
    create_gateway_app,
)
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import ASSISTANT_QUESTION_RESTART_ERROR_CODE, GatewayStore
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
        self.skip_retrieval = False

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

    #: What the lightweight classifier returns. Unset means "no classification",
    #: which is the routing every test that is not about the classifier wants.
    classified = None

    def classify_lightweight(self, question: str, *, language: str = "zh", tenant_id: str | None = None):
        del question, language, tenant_id
        return self.classified

    def start_assistant_qa(
        self,
        context: ScopeContext,
        question: str,
        *,
        conversation=None,
        conversation_turn_no=None,
        language="zh",
        skip_retrieval=False,
    ):
        del conversation, conversation_turn_no
        self.skip_retrieval = skip_retrieval
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


def _headers(token: str = "service") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Business-Entry": "consumer"}


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


def test_high_risk_clarification_holds_in_every_supported_language(tmp_path: Path) -> None:
    """A billing dispute is not a Chinese-only event.

    The cue list was Chinese-only, so an English "was I overcharged" fell
    through this guard, and the guard's own reply string was a Chinese
    literal regardless of Accept-Language — the same class of defect as the
    extraction bug above, on the branch beside it."""
    cases = [
        ("是不是扣错钱了", "zh", "请先选择需要检测的订单"),
        ("Was I overcharged for this charging session?", "en", "Please select the order"),
        ("My bill amount is wrong", "en", "Please select the order"),
        ("I was charged but never got power", "en", "Please select the order"),
        ("Wurde ich für diese Ladesitzung überladen?", "de", "Bitte wählen Sie zuerst"),
        ("Meine Rechnung ist falsch", "de", "Bitte wählen Sie zuerst"),
        ("Ai-je été surfacturé pour cette session ?", "fr", "Veuillez d'abord sélectionner"),
        ("¿Me han cobrado de más por esta carga?", "es", "Seleccione primero el pedido"),
        ("Fui cobrado a mais nesta recarga?", "pt", "Selecione primeiro o pedido"),
    ]
    for question, lang, expected in cases:
        client, runtime = _client(tmp_path)
        resp = client.post(
            "/v1/assistant/questions",
            json={"question": question},
            headers={**_headers(), "Accept-Language": lang},
        )
        assert resp.status_code == 200, (question, resp.text)
        body = resp.json()
        assert body["type"] == "clarification", (question, body)
        assert body["missing_fields"] == ["order_no"], question
        assert body["language"] == lang, question
        assert expected in body["message"], (question, body["message"])
        assert "qa_id" not in body
        assert runtime._qa == {}
        client.close()


def test_general_charging_questions_are_not_high_risk(tmp_path: Path) -> None:
    """The guard must not swallow ordinary knowledge questions.

    "Why did charging stop" is a FAQ entry (q011), not a billing dispute. A
    cue list broad enough to catch every order word would turn this branch
    into a catch-all and strip the FAQ short-circuit of its traffic."""
    client, runtime = _client(tmp_path)
    for question in (
        "Why did charging stop unexpectedly?",
        "充电过程中突然自动停止是什么原因",
        "How do I stop charging early?",
    ):
        resp = client.post("/v1/assistant/questions", json={"question": question}, headers=_headers())
        assert resp.status_code == 200, question
        assert resp.json()["type"] != "clarification", (question, resp.json())
    assert runtime.calls == []
    client.close()


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
    guess at (bare '未找到').

    The message is customer-facing copy, not the job record's internal reason.
    The two audiences need different sentences and only this boundary can tell
    them apart: a Chinese-speaking user was shown the harness's own English
    "customer QA turn returned invalid JSON". The record keeps the reason (the
    store assertion below), because a contract violation is a defect an engineer
    must be able to see — replacing it there once hid a real bug behind
    "service temporarily unavailable" (2026-09-17)."""
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
    # The machine-actionable half of the contract is unchanged: the frontend
    # still branches on `code`, and `error` is still present rather than null.
    assert body["error"]["code"] == "QA_FAILED"
    assert body["error"]["retryable"] is True
    assert body["result"] is None
    # The human half is localized copy, not the internal reason.
    assert body["error"]["message"] != "model provider quota exceeded"
    assert "quota" not in body["error"]["message"]
    # ...and the reason is not lost, only moved to where an engineer looks.
    assert runtime._qa[qa_id]["error_message"] == "model provider quota exceeded"


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


def test_extract_order_no_never_returns_a_latin_word(tmp_path: Path) -> None:
    """A candidate with no digit is a word, not an order number.

    41 live (2026-09-20): the token pattern was [!A-Za-z0-9] runs of 6-64
    safe chars with no digit requirement, so an ENGLISH question returned its
    own first word — `Please check charging anomalies for order (2098…)`
    extracted `Please`. Chinese questions were unaffected only because CJK
    characters are outside the token class, so the scan skipped straight to
    the bare order number. The same shape broke every Latin-script language
    and silently degraded Smart Diagnosis into zero-order customer service."""
    cases = {
        "Please check charging anomalies for order (2096164064667852801)": "2096164064667852801",
        "Please check charging anomalies for order 2096164064667852801.": "2096164064667852801",
        "Diagnose the charging issue of this order 2096164064667852801": "2096164064667852801",
        "check this order for charging problems 2096164064667852801": "2096164064667852801",
        # Purely textual questions have no order to find — and must NOT
        # manufacture one out of an ordinary word.
        "Show me industry solutions": None,
        "I'd like to see customer cases": None,
        "Show me a customer case": None,
    }
    for question, expected in cases.items():
        assert _extract_order_no(question) == expected, question


def test_assistant_english_embedded_order_reaches_diagnosis(tmp_path: Path) -> None:
    """The English form of the frontend's order-in-text submission.

    This is the reported defect: the English prefill reaches the assistant
    with the order inside the sentence, exactly like the Chinese one that has
    always worked. Only the digits-carrying candidate may be chosen."""
    client, runtime = _client(tmp_path)  # fixture allows 2096164064667852801
    question = "Please check charging anomalies for order (2096164064667852801)"
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": question},
        headers=_headers(),
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["type"] == "diagnosis"
    assert body["order_no_extracted"] == "2096164064667852801"
    assert runtime.calls == [("2096164064667852801", question)]


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


def test_clarification_message_follows_accept_language(tmp_path: Path) -> None:
    """41 live (2026-09-18): the clarification `message` was hardcoded Chinese
    while the response still echoed `language: en` — the one user-visible
    surface that ignored the request language. The client renders this string
    verbatim (frontend brief D.3), so it must be localized like everything
    else."""
    client, _ = _client(tmp_path)
    _publish_shortcut(
        client,
        {
            "business_entry": "consumer",
            "code": "smart_diagnosis",
            "intent": "order_issue",
            "requires_order": True,
            "labels": {"zh": "智能检测", "en": "Smart Diagnosis"},
        },
    )

    # Every supported language now carries its OWN copy, so this asserts real
    # translation rather than a zh fallback. Only a genuinely unsupported tag
    # falls back — covered separately below.
    # (requested tag, resolved language, expected fragment)
    cases = [
        ("zh", "zh", "请先选择需要检测的订单"),
        ("en", "en", "Please select the order"),
        ("de", "de", "Bitte wählen Sie zuerst den zu prüfenden Auftrag"),
        ("fr", "fr", "Veuillez d'abord sélectionner la commande"),
        ("es", "es", "Seleccione primero el pedido"),
        ("pt", "pt", "Selecione primeiro o pedido"),
        ("en-US", "en", "Please select the order"),  # region subtag folds
        # An UNSUPPORTED language resolves to zh, so both the echo and the copy
        # fall back together — the documented rule.
        ("ja", "zh", "请先选择需要检测的订单"),
    ]
    for sent, resolved, expected in cases:
        resp = client.post(
            "/v1/assistant/questions",
            json={"question": "帮我检测这个订单", "shortcut_code": "smart_diagnosis"},
            headers={**_headers(), "Accept-Language": sent},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["type"] == "clarification"
        assert body["language"] == resolved, (sent, body["language"])
        assert expected in body["message"], (sent, body["message"])
        # Never empty: the client renders this verbatim.
        assert body["message"].strip()


def test_jump_action_clarification_is_localized(tmp_path: Path) -> None:
    """The jump-action guard's reply (added in #262) was hardcoded too."""
    client, _ = _client(tmp_path)
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
    for lang, expected in (("zh", "请点击页面上的快捷按钮"), ("en", "shortcut button on the page")):
        resp = client.post(
            "/v1/assistant/questions",
            json={"question": "我要上报一个故障", "shortcut_code": "report_fault"},
            headers={**_headers(), "Accept-Language": lang},
        )
        body = resp.json()
        assert body["type"] == "clarification"
        assert body["language"] == lang
        assert expected in body["message"], (lang, body["message"])


class _StoreBackedRuntime(_Runtime):
    """A runtime whose read path is the real store, as ``GatewayRuntime`` is.

    ``get_assistant_qa`` there is a one-line delegation to the store, so a
    restart — whose whole effect is durable state written before the app can
    serve — has to be observed through the store, not through the in-memory
    ``_qa`` dict the rest of this file uses.
    """

    def __init__(self, store: GatewayStore) -> None:
        super().__init__()
        self._store = store

    def get_assistant_qa(self, context: ScopeContext, qa_id: str):
        return self._store.get_assistant_question(qa_id, context.scope_fingerprint)


def test_restarted_gateway_converges_an_in_flight_question(tmp_path: Path) -> None:
    """A question still `running` across a restart is over before the new
    gateway can answer a single request.

    The poll is the waiting-state contract: once the job is terminal the
    client stops polling and unlocks its input box, so `retry_after_ms` must go
    null and the error must name the restart rather than a timeout or a stop.
    """
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    scope = _Caller().resolve("service", required_scope=STANDARD_DIAGNOSIS_SCOPE).scope_fingerprint
    qa = store.create_assistant_question(scope, "什么是分时电价")
    store.update_assistant_question(qa["qa_id"], status="running")

    # The gateway restarts on the same database: building the app is the boot.
    restarted = GatewayStore(settings.database_file)
    app = create_gateway_app(
        settings=settings,
        store=restarted,
        runtime=_StoreBackedRuntime(restarted),  # type: ignore[arg-type]
        caller_resolver=_Caller(),
        order_authorizer=_Authorizer({"2096164064667852801"}),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    with TestClient(app) as client:
        poll = client.get(f"/v1/assistant/questions/{qa['qa_id']}", headers=_headers())

    assert poll.status_code == 200, poll.text
    body = poll.json()
    assert body["status"] == "failed"
    assert body["retry_after_ms"] is None
    assert body["result"] is None
    assert body["error"]["code"] == ASSISTANT_QUESTION_RESTART_ERROR_CODE


# ----------------------------------------------------------------------
# Stopping a question (PRD #346 / #357)
# ----------------------------------------------------------------------

#: A plain knowledge question: reaches the generic QA route (202) without
#: matching the FAQ short-circuit, naming an order, or reading as a dispute.
STOP_QUESTION = "电动车的电池保养怎么做"
FOLLOW_UP_QUESTION = "动力电池的质保政策一般是多少年"


class _Turn:
    """Stands in for the SDK turn handle the worker runs on.

    ``interrupt()`` is the RPC the turn timeout already uses; recording the
    call is what proves the stop request reached the turn that was burning
    tokens.
    """

    def __init__(self) -> None:
        self.interrupted = False

    def interrupt(self) -> None:
        self.interrupted = True


class _TwoTenantCaller(_Caller):
    """The same resolver with a second tenant available by token name.

    A stop request from another tenant must read exactly like a job that does
    not exist — otherwise cancelling becomes a way to probe which qa_ids are
    real.
    """

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        del third_session
        subject = (
            SubjectRecord(b_user_id="c:C-2", c_user_id="C-2", tenant_id="T-2")
            if token == "other-tenant"
            else SubjectRecord(b_user_id="c:C-1", c_user_id="C-1", tenant_id="T-1")
        )
        if token == "narrow" and required_scope not in {"aiops:faq:read"}:
            raise CallerAuthError("insufficient scope", code=CALLER_AUTH_FORBIDDEN)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=subject.tenant_id,
            data_scope=DataScope(type="self"),
            roles=frozenset({"ROLE_AGENT_ADMIN"}),
            permissions=frozenset({required_scope}),
        )


def _real_runtime_client(
    tmp_path: Path,
    monkeypatch,
    *,
    gates: dict | None = None,
    turn: _Turn | None = None,
    caller_resolver=None,
):
    """A TestClient over the REAL runtime.

    Stopping a question is observable only where it happens: the terminal
    write, the interrupt of the live model turn, and the conversation slot that
    is freed. A stub runtime would prove the stub, so these tests run the real
    worker — parked inside the (patched) model call when they need to catch it
    mid-answer, which is exactly the state a stop request arrives in.
    """
    from aiops_diagnostics import gateway_runtime
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    os.chmod(settings.server_config_file, 0o600)  # the config is a private file
    # The classifier is a model call of its own: routing is not what a stop test
    # is about, and its result must not depend on whether a provider key happens
    # to be configured in the environment.
    monkeypatch.setattr(GatewayRuntime, "classify_lightweight", lambda *a, **k: None)
    if gates is not None:

        def _answer(_question, _settings, *, turn_registrar=None, **_kwargs):
            gates["at_model_call"].set()
            gates["before_turn"].wait(timeout=30)
            if turn_registrar is not None:
                turn_registrar(turn)
            gates["turn_started"].set()
            gates["release"].wait(timeout=30)
            return {"text": "这是一个完整的答案", "reminder": False}

        monkeypatch.setattr(gateway_runtime, "run_zero_order_answer", _answer)

    runtime = GatewayRuntime(GatewayStore(settings.database_file), settings, Settings())
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,
        caller_resolver=caller_resolver or _Caller(),
        order_authorizer=_Authorizer({"2096164064667852801"}),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app), runtime


def _gates() -> dict:
    return {
        "at_model_call": threading.Event(),
        "before_turn": threading.Event(),
        "turn_started": threading.Event(),
        "release": threading.Event(),
    }


def test_stopping_a_question_returns_its_terminal_state_and_unlocks_the_conversation(
    tmp_path: Path, monkeypatch
) -> None:
    """The stop returns the job's own terminal state — no second poll needed —
    frees the generation slot at once, and interrupts the running turn."""
    turn = _Turn()
    gates = _gates()
    gates["before_turn"].set()  # let the turn start as soon as the model runs
    client, runtime = _real_runtime_client(tmp_path, monkeypatch, gates=gates, turn=turn)
    try:
        conversation = client.post(
            "/v1/conversations",
            json={"agent_version_key": "agt_abcdef1234567890#v1"},
            headers=_headers(),
        ).json()
        cid = conversation["conversation_id"]

        asked = client.post(
            "/v1/assistant/questions",
            json={"question": STOP_QUESTION, "conversation_id": cid},
            headers=_headers(),
        )
        assert asked.status_code == 202
        qa_id = asked.json()["qa_id"]
        assert gates["turn_started"].wait(10), "the worker never reached the model turn"

        stopped = client.post(f"/v1/assistant/questions/{qa_id}/cancel", headers=_headers())

        assert stopped.status_code == 200
        body = stopped.json()
        assert body["type"] == "qa"
        assert body["qa_id"] == qa_id
        assert body["status"] == "cancelled"
        assert body["retry_after_ms"] is None
        assert body["result"] is None
        assert body["error"] is None
        assert turn.interrupted, "the stop request never reached the live turn"

        # The generation slot is free the moment the stop lands: the next
        # question in the same conversation is accepted, not 409.
        detail = client.get(f"/v1/conversations/{cid}", headers=_headers())
        assert detail.json()["is_generating"] is False
        again = client.post(
            "/v1/assistant/questions",
            json={"question": FOLLOW_UP_QUESTION, "conversation_id": cid},
            headers=_headers(),
        )
        assert again.status_code == 202, again.text

        # Only the follow-up survives as a turn: the stopped question leaves no
        # row, so it never becomes context for a later answer.
        gates["release"].set()
        assert [item["question"] for item in _answered_turns(client, cid)] == [FOLLOW_UP_QUESTION]
    finally:
        gates["release"].set()
        runtime.shutdown()
        client.close()


def _answered_turns(client: TestClient, conversation_id: str) -> list[dict]:
    """Wait for the follow-up's worker to write its answer, then read history."""
    for _ in range(250):
        turns = client.get(f"/v1/conversations/{conversation_id}", headers=_headers()).json()["turns"]
        if turns and turns[-1]["answer"] is not None:
            return turns
        threading.Event().wait(0.02)
    raise AssertionError("the follow-up answer never reached the conversation")


def test_stopping_a_question_twice_and_after_it_finished_is_not_an_error(tmp_path: Path, monkeypatch) -> None:
    """A double tap, and a tap that lands just after the answer did, both answer
    with the job's own state — the stop button is not allowed to invent
    failures the user then has to explain."""
    client, runtime = _real_runtime_client(tmp_path, monkeypatch)
    try:
        scope = _Caller().resolve("service", required_scope=STANDARD_DIAGNOSIS_SCOPE).scope_fingerprint
        in_flight = runtime.store.create_assistant_question(scope, STOP_QUESTION)
        finished = runtime.store.create_assistant_question(scope, STOP_QUESTION)
        runtime.store.update_assistant_question(
            finished["qa_id"], status="completed", result={"text": "答案"}
        )

        first = client.post(f"/v1/assistant/questions/{in_flight['qa_id']}/cancel", headers=_headers())
        assert first.status_code == 200
        assert first.json()["status"] == "cancelled"

        # Second tap on the same job: still that job's state, not an error.
        second = client.post(f"/v1/assistant/questions/{in_flight['qa_id']}/cancel", headers=_headers())
        assert second.status_code == 200
        assert second.json()["status"] == "cancelled"

        # A job that completed while the user was reaching for the button keeps
        # its answer: the stop is a no-op, never a rewrite of the result.
        late = client.post(f"/v1/assistant/questions/{finished['qa_id']}/cancel", headers=_headers())
        assert late.status_code == 200
        assert late.json()["status"] == "completed"
        assert late.json()["result"] == {"text": "答案"}
    finally:
        runtime.shutdown()
        client.close()


def test_stopping_an_unknown_or_foreign_question_is_404(tmp_path: Path, monkeypatch) -> None:
    """Missing and out-of-scope are the same answer, and neither stops the job:
    cancelling must not become a way to probe which qa_ids are real."""
    client, runtime = _real_runtime_client(tmp_path, monkeypatch, caller_resolver=_TwoTenantCaller())
    try:
        scope = (
            _TwoTenantCaller().resolve("service", required_scope=STANDARD_DIAGNOSIS_SCOPE).scope_fingerprint
        )
        qa = runtime.store.create_assistant_question(scope, STOP_QUESTION)

        missing = client.post(
            "/v1/assistant/questions/qa_nonexistent000000000000000000000001/cancel",
            headers=_headers(),
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "QA_NOT_FOUND"

        foreign = client.post(
            f"/v1/assistant/questions/{qa['qa_id']}/cancel", headers=_headers("other-tenant")
        )
        # Refused by the identity layer before any job lookup, exactly like
        # every other cross-tenant read: the caller learns nothing either way.
        assert foreign.status_code in (403, 404, 503)
        assert runtime.store.get_assistant_question(qa["qa_id"], scope)["status"] == "queued"
    finally:
        runtime.shutdown()
        client.close()


def test_casual_question_returns_a_job_rather_than_an_inline_answer(tmp_path: Path) -> None:
    """Chit-chat is answered by the zero-order QA job, not by the classifier (#391).

    The classifier no longer writes the reply. Two reasons, and the second is why
    this is required rather than tidier: the answer travels the one path that has
    the language guard and the localized failure copy, and typed-decision routing
    (#383) cannot produce text at all — an answer that lived in the classifier
    would vanish when routing moved off generated text.
    """
    client, runtime = _client(tmp_path)
    runtime.classified = {"intent": "casual", "confidence": "high", "risk": "low", "answer": "你好呀"}
    resp = client.post("/v1/assistant/questions", json={"question": "你好，你好，你好。"}, headers=_headers())
    # A job, not a synchronous answer.
    assert resp.status_code == 202
    body = resp.json()
    assert body["type"] == "qa"
    assert body["status"] == "queued"
    assert body["qa_id"]
    # The classifier's own sentence is not what the caller receives.
    assert "answer" not in body.get("result", {}) if body.get("result") else True


def test_a_casual_question_skips_knowledge_retrieval(tmp_path: Path) -> None:
    """A greeting must not trigger a library lookup just because search is wired."""
    client, runtime = _client(tmp_path)
    runtime.classified = {"intent": "casual", "confidence": "high", "risk": "low", "answer": "你好呀"}
    client.post("/v1/assistant/questions", json={"question": "你好"}, headers=_headers())
    assert runtime.skip_retrieval is True


def test_a_non_casual_question_still_searches(tmp_path: Path) -> None:
    """The skip is scoped to chit-chat; a business question keeps its retrieval."""
    client, runtime = _client(tmp_path)
    runtime.classified = {"intent": "knowledge", "confidence": "high", "risk": "low"}
    client.post("/v1/assistant/questions", json={"question": "充电桩怎么拔枪"}, headers=_headers())
    assert runtime.skip_retrieval is False


def test_a_high_risk_low_confidence_question_still_asks_for_context(tmp_path: Path) -> None:
    """#391 must not weaken the asymmetric threshold #346 relies on.

    `risk` high with confidence short of high means "ask once more rather than
    act on a thin judgement" — that rule is unchanged, and a chit-chat answer
    moving out of the classifier does not touch it.
    """
    client, runtime = _client(tmp_path)
    runtime.classified = {"intent": "order_issue", "confidence": "medium", "risk": "high"}
    resp = client.post("/v1/assistant/questions", json={"question": "这单扣费不对"}, headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["type"] == "clarification"


def test_a_confident_high_risk_question_still_asks_for_context(tmp_path: Path) -> None:
    """Plan A (#401): high risk asks regardless of confidence.

    Measured on 86 real questions, the previous "high risk AND unsure" form
    asked **zero** times: Jev recognises money questions and is confident about
    recognising them, so the second condition never held. The real billing
    complaint (`帮我看看我的订单扣费对不对,感觉多扣了钱`) came back
    risk=high/confidence=high and was answered without ever asking.

    Measured on the real corpus, this rule adds questions the earlier
    **keyword** guard (`_HIGH_RISK_ORDER_CUES`) does not catch — e.g. `refund`
    and `Please check charging anomalies for order ...`, whose wording carries
    no cue from that list. The question below is deliberately one of those, so
    the clarification can only come from the risk rule.
    """
    client, runtime = _client(tmp_path)
    runtime.classified = {"intent": "order_issue", "confidence": "high", "risk": "high"}
    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "refund"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "clarification"
    assert body["missing_fields"] == ["context"]


def test_a_low_risk_question_is_not_interrupted(tmp_path: Path) -> None:
    """The rule stays asymmetric — only risk gates it now, not confidence."""
    client, runtime = _client(tmp_path)
    runtime.classified = {"intent": "casual", "confidence": "low", "risk": "low"}
    resp = client.post("/v1/assistant/questions", json={"question": "你好"}, headers=_headers())
    assert resp.status_code == 202
    assert resp.json()["type"] == "qa"
