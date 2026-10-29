from __future__ import annotations

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

    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
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
        # Every entry point that STARTS a job. A boundary that leaked through
        # the QA route would be invisible to a test watching only `calls`.
        self.qa_calls: list[str] = []
        self._qa = {}  # qa_id -> record
        self.skip_retrieval = False
        self.classify_calls = 0

    def shutdown(self) -> None:
        pass

    def start_standard_diagnosis(
        self,
        context: ScopeContext,
        order_no: str,
        question: str,
        indicator_code,
        language="zh",
        *,
        conversation_turn=None,
        business_entry: str | None = None,
    ):
        del context, indicator_code, conversation_turn, business_entry
        self.calls.append((order_no, question))
        # The stub must STORE the language it was given, like the real runtime —
        # which normalises it before storing. A stub that dropped it made every
        # creation response report the request language whatever the row held,
        # so the surface could not see the two disagree (#541 review).
        from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, can_prompt_in

        stored_language = language if can_prompt_in(language) else DEFAULT_LANGUAGE
        self.stored_language = stored_language
        return {
            "diagnosis_id": "dx_test000000000000000000000000000001",
            "order_no": order_no,
            "question": question,
            "indicator_code": None,
            "status": "queued",
            "language": stored_language,
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
        self.classify_calls += 1
        return self.classified

    def start_assistant_qa(
        self,
        context: ScopeContext,
        question: str,
        *,
        conversation=None,
        conversation_turn_no=None,
        language="zh",
        promo_target=None,
        promo_intent=None,
        skip_retrieval=False,
        business_entry="",
    ):
        del conversation, conversation_turn_no, promo_target, promo_intent, business_entry
        self.qa_calls.append(question)
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


def test_assistant_qa_poll_unknown_is_404(tmp_path: Path) -> None:
    """Polling a qa_id outside the caller scope is refused."""
    client, _ = _client(tmp_path)
    poll = client.get("/v1/assistant/questions/qa_nonexistent000000000000000000000001", headers=_headers())
    assert poll.status_code == 404
    assert poll.json()["error"]["code"] == "QA_NOT_FOUND"


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
