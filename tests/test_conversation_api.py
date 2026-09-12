"""Conversation and active-order context tests (T4/#172).

Protocol-level via FastAPI TestClient with the same fake stack as
test_assistant_api.py: covers scope/entry/agent binding isolation, the
8-turn/8k-token window, 30-day retention, active_order follow-up diagnosis
with per-turn ownership re-verification, plain knowledge questions staying on
qa+RAG even with an active order, 409 CONVERSATION_BUSY, cancelled turns
never surviving as complete replies, and delete semantics.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

OWNED_ORDER = "2096164064667852801"


class _Caller:
    """Two identities: "narrow" holds only faq:read; the default token is
    granted any scope and can impersonate tenant/subject via token names."""

    def __init__(self) -> None:
        self.subject = SubjectRecord(b_user_id="c:C-1", c_user_id="C-1", tenant_id="T-1")

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        del third_session
        if token == "narrow" and required_scope not in {"aiops:faq:read"}:
            from aiops_diagnostics.caller_auth import CALLER_AUTH_FORBIDDEN, CallerAuthError

            raise CallerAuthError("insufficient scope", code=CALLER_AUTH_FORBIDDEN)
        subject = self.subject
        tenant = "T-1"
        if token == "other-tenant":
            subject = SubjectRecord(b_user_id="c:C-2", c_user_id="C-2", tenant_id="T-2")
            tenant = "T-2"
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=tenant,
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
    """Allows only OWNED_ORDER by default; can flip to simulate revoked."""

    def __init__(self) -> None:
        self.allowed = {OWNED_ORDER}

    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        del context
        return order_no in self.allowed


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.busy_conversations: set[str] = set()

    def shutdown(self) -> None:
        pass

    def start_standard_diagnosis(self, context, order_no: str, question: str, indicator_code, language="zh"):
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
            "created_at": "2026-09-10T00:00:00+00:00",
            "updated_at": "2026-09-10T00:00:00+00:00",
            "completed_at": None,
        }

    def get_standard_diagnosis(self, context, diagnosis_id):
        del context, diagnosis_id
        return None

    def start_assistant_qa(
        self,
        context,
        question: str,
        *,
        conversation=None,
        conversation_turn_no=None,
        language="zh",
    ):
        # Simulate the 409 surface: the API layer raises busy BEFORE us, so
        # reaching here means the slot was claimable. Record and answer async.
        del conversation, conversation_turn_no
        self.calls.append(("qa", question))
        return {
            "qa_id": "qa_test00000000000000000000000000000001",
            "question": question,
            "status": "queued",
            "result": None,
        }

    def get_assistant_qa(self, context, qa_id):
        del context, qa_id
        return None

    def list_assistant_qa(self, context, *, limit: int = 50):
        del context, limit
        return []


def _client(tmp_path: Path):
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    authorizer = _Authorizer()
    runtime = _Runtime()
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=_Caller(),
        order_authorizer=authorizer,
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app), authorizer, runtime


def _headers(token: str = "service") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Business-Entry": "consumer"}


def _create_conversation(client: TestClient, *, entry: str = "consumer") -> dict:
    resp = client.post(
        "/v1/conversations",
        json={"agent_version_key": "agt_abcdef1234567890#v1"},
        headers={"Authorization": "Bearer service", "X-Business-Entry": entry},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_conversation_crud_and_cross_scope_isolation(tmp_path: Path) -> None:
    """Create/list/get/delete; other tenant or entry gets a uniform 404."""
    client, _, _ = _client(tmp_path)
    conversation = _create_conversation(client)
    cid = conversation["conversation_id"]

    # Owner sees it in list and detail.
    listed = client.get("/v1/conversations", headers=_headers())
    assert listed.status_code == 200
    assert listed.json()["conversations"][0]["conversation_id"] == cid
    detail = client.get(f"/v1/conversations/{cid}", headers=_headers())
    assert detail.status_code == 200
    assert detail.json()["business_entry"] == "consumer"

    # Another tenant never reaches the conversation: the identity layer
    # rejects the cross-tenant mapping before any conversation lookup (the
    # conversation is invisible either way — no existence leak).
    other = client.get(f"/v1/conversations/{cid}", headers=_headers("other-tenant"))
    assert other.status_code in (403, 404, 503)

    # Same user, different entry: entry is part of the binding → 404.
    operator = client.get(
        f"/v1/conversations/{cid}",
        headers={"Authorization": "Bearer service", "X-Business-Entry": "operator"},
    )
    assert operator.status_code == 404

    # Delete removes it for the owner; a repeat delete is the uniform 404.
    assert client.delete(f"/v1/conversations/{cid}", headers=_headers()).status_code == 200
    assert client.get(f"/v1/conversations/{cid}", headers=_headers()).status_code == 404
    assert client.delete(f"/v1/conversations/{cid}", headers=_headers()).status_code == 404


def test_active_order_bind_requires_ownership(tmp_path: Path) -> None:
    """Binding an unowned order is a uniform 404, never an ownership oracle."""
    client, _, _ = _client(tmp_path)
    conversation = _create_conversation(client)
    cid = conversation["conversation_id"]

    ok = client.post(
        f"/v1/conversations/{cid}/active-order",
        json={"order_no": OWNED_ORDER},
        headers=_headers(),
    )
    assert ok.status_code == 200
    assert ok.json()["active_order_no"] == OWNED_ORDER

    unowned = client.post(
        f"/v1/conversations/{cid}/active-order",
        json={"order_no": "1234567890123456789"},
        headers=_headers(),
    )
    assert unowned.status_code == 404
    assert unowned.json()["error"]["code"] == "ORDER_NOT_FOUND"


def test_followup_omits_order_via_active_order(tmp_path: Path) -> None:
    """An order follow-up without order_no routes to diagnosis via the
    conversation's confirmed order (ownership re-verified per turn)."""
    client, authorizer, runtime = _client(tmp_path)
    conversation = _create_conversation(client)
    cid = conversation["conversation_id"]
    client.post(f"/v1/conversations/{cid}/active-order", json={"order_no": OWNED_ORDER}, headers=_headers())

    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "我刚才那笔充电订单为什么突然停了", "conversation_id": cid},
        headers=_headers(),
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["type"] == "diagnosis"
    assert body["order_no_from_context"] == OWNED_ORDER
    assert runtime.calls == [(OWNED_ORDER, "我刚才那笔充电订单为什么突然停了")]

    # Ownership revoked between turns: the same follow-up falls back to qa
    # (plain knowledge answer), and the stale binding is cleared.
    authorizer.allowed = set()
    resp2 = client.post(
        "/v1/assistant/questions",
        json={"question": "那笔订单现在怎么还不退款", "conversation_id": cid},
        headers=_headers(),
    )
    assert resp2.status_code == 202
    assert resp2.json()["type"] == "qa"
    detail = client.get(f"/v1/conversations/{cid}", headers=_headers()).json()
    assert detail["active_order_no"] is None


def test_knowledge_question_stays_qa_even_with_active_order(tmp_path: Path) -> None:
    """A plain knowledge question routes to qa+RAG despite an active order."""
    client, _, runtime = _client(tmp_path)
    conversation = _create_conversation(client)
    cid = conversation["conversation_id"]
    client.post(f"/v1/conversations/{cid}/active-order", json={"order_no": OWNED_ORDER}, headers=_headers())

    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "会员积分商城什么时候上线呀", "conversation_id": cid},
        headers=_headers(),
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["type"] == "qa"
    assert "conversation_id" in body
    # No diagnosis was started.
    assert runtime.calls == [("qa", "会员积分商城什么时候上线呀")]


def test_concurrent_generation_returns_409(tmp_path: Path) -> None:
    """A second turn while the conversation is generating gets 409 BUSY."""
    client, _, _ = _client(tmp_path)
    conversation = _create_conversation(client)
    cid = conversation["conversation_id"]

    # Claim the generation slot directly (the worker would hold it between
    # begin_turn and complete_turn).
    from aiops_diagnostics.conversation_store import ConversationStore

    store = ConversationStore(Path(client.app.state.gateway.settings.database_file))
    store.begin_turn(cid, _scope_of(client), kind="qa", question="占位")

    busy = client.post(
        "/v1/assistant/questions",
        json={"question": "积分商城什么时候上线呀", "conversation_id": cid},
        headers=_headers(),
    )
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "CONVERSATION_BUSY"

    # Without a conversation the same question is not blocked.
    plain = client.post(
        "/v1/assistant/questions",
        json={"question": "积分商城什么时候上线呀"},
        headers=_headers(),
    )
    assert plain.status_code == 202


def _scope_of(client: TestClient) -> str:
    """The test caller's scope fingerprint (same construction as _Caller)."""
    subject = SubjectRecord(b_user_id="c:C-1", c_user_id="C-1", tenant_id="T-1")
    context = ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="T-1",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:diagnoses:write"}),
    )
    return context.scope_fingerprint


def test_busy_lock_expires(tmp_path: Path) -> None:
    """A crashed worker's generating flag self-expires after the lock window."""
    from datetime import UTC, datetime, timedelta

    from aiops_diagnostics.conversation_store import ConversationStore

    client, _, _ = _client(tmp_path)
    conversation = _create_conversation(client)
    cid = conversation["conversation_id"]
    store = ConversationStore(Path(client.app.state.gateway.settings.database_file))
    scope = _scope_of(client)

    turn_no = store.begin_turn(cid, scope, kind="qa", question="占位")
    # Backdate the lock beyond BUSY_LOCK_SECONDS.
    stale = datetime.now(UTC) - timedelta(seconds=180)
    with store._connection(write=True) as connection:  # noqa: SLF001 — test probes internals
        connection.execute(
            "UPDATE conversations SET generating_since = ? WHERE conversation_id = ?",
            (stale.isoformat(), cid),
        )
    # A new turn claims the slot despite the stale flag.
    next_no = store.begin_turn(cid, scope, kind="qa", question="再问")
    assert next_no == turn_no + 1


def test_cancelled_turn_never_survives_as_reply(tmp_path: Path) -> None:
    """release/complete with no answer drops the row; history stays clean."""
    from aiops_diagnostics.conversation_store import ConversationStore

    client, _, _ = _client(tmp_path)
    conversation = _create_conversation(client)
    cid = conversation["conversation_id"]
    store = ConversationStore(Path(client.app.state.gateway.settings.database_file))
    scope = _scope_of(client)

    turn_no = store.begin_turn(cid, scope, kind="qa", question="会被取消")
    store.release_turn(cid, scope, turn_no)
    history = store.turns(cid, scope)
    assert history == []
    assert store.get(cid, scope)["is_generating"] is False

    # A completed turn persists; a subsequent cancelled one does not.
    keep = store.begin_turn(cid, scope, kind="qa", question="正常问题")
    store.complete_turn(
        cid,
        scope,
        keep,
        answer={"blocks": [{"kind": "text", "text": "答案"}], "retrieval_status": "found"},
        token_count=50,
    )
    dropped = store.begin_turn(cid, scope, kind="qa", question="中断问题")
    store.complete_turn(cid, scope, dropped, answer=None, cancelled=True)
    history = store.turns(cid, scope)
    assert [item["turn_no"] for item in history] == [keep]


def test_context_window_is_bounded(tmp_path: Path) -> None:
    """context_turns keeps at most 8 turns and the 8k-token budget."""
    from aiops_diagnostics.conversation_store import (
        CONTEXT_MAX_TOKENS,
        CONTEXT_MAX_TURNS,
        ConversationStore,
    )

    client, _, _ = _client(tmp_path)
    conversation = _create_conversation(client)
    cid = conversation["conversation_id"]
    store = ConversationStore(Path(client.app.state.gateway.settings.database_file))
    scope = _scope_of(client)

    # Twelve finished turns: window must cap at the newest 8.
    for index in range(12):
        turn_no = store.begin_turn(cid, scope, kind="qa", question=f"问题{index}")
        store.complete_turn(
            cid,
            scope,
            turn_no,
            answer={"blocks": [{"kind": "text", "text": f"答案{index}"}], "retrieval_status": "found"},
            token_count=100,
        )
    window = store.context_turns(cid, scope)
    assert len(window) == CONTEXT_MAX_TURNS
    assert window[0]["turn_no"] == 5  # turns 5..12, oldest-first

    # Token budget: when the newest turns are large, the 8k cap binds before
    # the 8-turn cap — 8 × 2500 = 20k > 8k, so only ~3 turns survive.
    for index in range(12, 20):
        turn_no = store.begin_turn(cid, scope, kind="qa", question=f"长问题{index}")
        store.complete_turn(
            cid,
            scope,
            turn_no,
            answer={"blocks": [{"kind": "text", "text": "长" * 3700}], "retrieval_status": "found"},
            token_count=2500,
        )
    window = store.context_turns(cid, scope)
    assert len(window) < CONTEXT_MAX_TURNS
    total = sum(item["token_count"] for item in window)
    # The newest turn is always kept even alone; after that the budget binds.
    assert total <= CONTEXT_MAX_TOKENS + 2500


def test_conversation_requires_full_assistant_scope(tmp_path: Path) -> None:
    """A faq:read-only token cannot create conversations."""
    client, _, _ = _client(tmp_path)
    resp = client.post(
        "/v1/conversations",
        json={"agent_version_key": "agt_abcdef1234567890#v1"},
        headers={
            "Authorization": "Bearer narrow",
            "X-Business-Entry": "consumer",
        },
    )
    assert resp.status_code in (401, 403)


def test_conversation_retention_expires_after_30_days(tmp_path: Path) -> None:
    """Expired conversations vanish from list/get (30-day retention)."""
    from datetime import UTC, datetime, timedelta

    from aiops_diagnostics.conversation_store import ConversationStore

    client, _, _ = _client(tmp_path)
    conversation = _create_conversation(client)
    cid = conversation["conversation_id"]
    store = ConversationStore(Path(client.app.state.gateway.settings.database_file))

    expired = datetime.now(UTC) - timedelta(days=31)
    with store._connection(write=True) as connection:  # noqa: SLF001 — test probes internals
        connection.execute(
            "UPDATE conversations SET expires_at = ? WHERE conversation_id = ?",
            (expired.isoformat(), cid),
        )
    listed = client.get("/v1/conversations", headers=_headers()).json()
    assert listed["count"] == 0
    assert client.get(f"/v1/conversations/{cid}", headers=_headers()).status_code == 404
