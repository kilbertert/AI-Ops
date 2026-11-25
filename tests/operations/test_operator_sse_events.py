"""#1709 行为契约测试：节点路径白名单脱敏投影、SSE 实时流、Last-Event-ID 续传、取消与复核替换。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from aiops_diagnostics.conversation.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway.gateway_api import create_gateway_app
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.identity.capability import CAPABILITY_ASSERTED
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.operator.qa_events import project_steps_from_trace, sanitize_step_summary

_OP_A = {"Authorization": "Bearer user-a", "X-Business-Entry": "operator"}
_OP_B = {"Authorization": "Bearer user-b", "X-Business-Entry": "operator"}


class _MultiUserCaller:
    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        del third_session, platform_entry, source_key
        uid = "U-B" if token == "user-b" else "U-A"
        tenant = "T-B" if token == "user-b" else "T-A"
        subject = SubjectRecord(b_user_id=uid, c_user_id=uid, tenant_id=tenant)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=tenant,
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
            capability_source=CAPABILITY_ASSERTED,
        )


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord(c_user_id, c_user_id, tenant_id, "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


class _Runtime:
    def __init__(self, store: GatewayStore) -> None:
        self.store = store

    def shutdown(self) -> None:
        pass

    def get_assistant_qa(self, context: ScopeContext, qa_id: str) -> dict[str, Any] | None:
        return self.store.get_assistant_question(qa_id, context.scope_fingerprint)

    def cancel_assistant_qa(self, context: ScopeContext, qa_id: str) -> dict[str, Any] | None:
        qa = self.store.get_assistant_question(qa_id, context.scope_fingerprint)
        if qa is None:
            return None
        self.store.update_assistant_question(qa_id, status="cancelled")
        return self.store.get_assistant_question(qa_id, context.scope_fingerprint)


def _parse_sse(raw: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line:
            if current:
                frames.append(current)
                current = {}
            continue
        if line.startswith("id:"):
            current["id"] = int(line.split(":", 1)[1].strip())
        elif line.startswith("event:"):
            current["event"] = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            current["data"] = json.loads(line.split(":", 1)[1].strip())
    if current:
        frames.append(current)
    return frames


def _client(tmp_path: Path) -> tuple[TestClient, GatewayStore]:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=_Runtime(store),  # type: ignore[arg-type]
        caller_resolver=_MultiUserCaller(),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app), store


def test_step_projection_redacts_secrets_urls_and_sql() -> None:
    cleaned = sanitize_step_summary(
        "Bearer secret-tok https://internal.local/api SELECT * FROM ch_order_info WHERE id=1 {raw: 1}"
    )
    assert cleaned is not None
    assert "secret-tok" not in cleaned
    assert "internal.local" not in cleaned
    assert "ch_order_info" not in cleaned
    assert "{" not in cleaned

    steps = project_steps_from_trace(
        [
            {"phase": "authorize", "status": "running", "elapsed_ms": 10},
            {"phase": "authorize", "status": "succeeded", "elapsed_ms": 40, "duration_ms": 30},
            {"phase": "tool", "tool": "cpms_order_detail", "status": "running", "elapsed_ms": 50},
            {
                "phase": "tool",
                "tool": "cpms_order_detail",
                "status": "succeeded",
                "elapsed_ms": 200,
                "duration_ms": 150,
            },
            {"phase": "review", "status": "running", "elapsed_ms": 210},
            {"phase": "review", "status": "succeeded", "elapsed_ms": 260, "duration_ms": 50},
        ],
        language="zh",
    )
    assert [s["node_code"] for s in steps] == ["authorize_scope", "cpms_order_detail", "answer_review"]
    assert all(s["status"] == "succeeded" for s in steps)
    assert steps[1]["title"] == "订单详情"
    assert steps[1]["duration_ms"] == 150


def test_sse_stream_replay_resume_replace_cancel_and_isolation(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    scope_a = _MultiUserCaller().resolve("user-a", required_scope="aiops:diagnoses:write").scope_fingerprint
    with client:
        qa = store.create_assistant_question(scope_a, "排查异常订单", tenant_id="T-A")
        qa_id = qa["qa_id"]
        events = client.app.state.gateway.qa_events
        events.append(qa_id, "run_started", {"qa_id": qa_id, "attempt_no": 1})
        events.append(
            qa_id,
            "step_started",
            {"step_id": "st_1", "node_type": "tool", "node_code": "cpms_order_detail", "title": "订单详情"},
        )
        events.append(
            qa_id,
            "step_finished",
            {"step_id": "st_1", "node_type": "tool", "node_code": "cpms_order_detail", "status": "succeeded"},
        )
        events.append(qa_id, "message_delta", {"delta": "草稿段落1", "elapsed_ms": 300})
        events.append(
            qa_id,
            "message_replace",
            {
                "delivery_mode": "evidence_only",
                "blocks": [{"kind": "text", "text": "终局复核仅交付只读证据。"}],
                "notice": "已切换为仅交付只读证据。",
            },
        )
        events.append(
            qa_id,
            "message_done",
            {
                "status": "completed",
                "elapsed_ms": 420,
                "delivery_mode": "evidence_only",
                "result": {"blocks": [{"kind": "text", "text": "终局复核仅交付只读证据。"}]},
            },
        )
        store.update_assistant_question(
            qa_id,
            status="completed",
            result={"blocks": [{"kind": "text", "text": "终局复核仅交付只读证据。"}]},
        )

        # Full stream replay from seq 0
        resp = client.get(f"/v1/assistant/questions/{qa_id}/events", headers=_OP_A)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["x-accel-buffering"] == "no"
        assert resp.headers["cache-control"] == "no-store"
        frames = _parse_sse(resp.text)
        assert [f["event"] for f in frames] == [
            "run_started",
            "step_started",
            "step_finished",
            "message_delta",
            "message_replace",
            "message_done",
        ]
        assert [f["id"] for f in frames] == [1, 2, 3, 4, 5, 6]

        # Resume via Last-Event-ID=3 -> no duplicate, no missing frames
        resumed = client.get(
            f"/v1/assistant/questions/{qa_id}/events",
            headers={**_OP_A, "Last-Event-ID": "3"},
        )
        assert resumed.status_code == 200
        resumed_frames = _parse_sse(resumed.text)
        assert [f["id"] for f in resumed_frames] == [4, 5, 6]
        assert [f["event"] for f in resumed_frames] == [
            "message_delta",
            "message_replace",
            "message_done",
        ]

        # Resume via after_seq query param
        resumed_q = client.get(
            f"/v1/assistant/questions/{qa_id}/events",
            params={"after_seq": 5},
            headers=_OP_A,
        )
        assert [f["id"] for f in _parse_sse(resumed_q.text)] == [6]

        # Invalid Last-Event-ID -> 422
        bad_id = client.get(
            f"/v1/assistant/questions/{qa_id}/events",
            headers={**_OP_A, "Last-Event-ID": "not-an-int"},
        )
        assert bad_id.status_code == 422

        # Cross-tenant isolation -> 404
        assert client.get(f"/v1/assistant/questions/{qa_id}/events", headers=_OP_B).status_code == 404

        # Cancel emits run_cancelled and closes SSE stream
        qa2 = store.create_assistant_question(scope_a, "运行中的提问", tenant_id="T-A")
        cancel = client.post(f"/v1/assistant/questions/{qa2['qa_id']}/cancel", headers=_OP_A)
        assert cancel.status_code == 200
        assert cancel.json()["stream_url"] == f"/v1/assistant/questions/{qa2['qa_id']}/events"
        cancelled_stream = client.get(f"/v1/assistant/questions/{qa2['qa_id']}/events", headers=_OP_A)
        cancelled_frames = _parse_sse(cancelled_stream.text)
        assert cancelled_frames[-1]["event"] == "run_cancelled"

        # Internal stream-chunks callback rejects unauthenticated requests
        unauth = client.post(
            "/v1/internal/operator-runs/run_missing/stream-chunks",
            json={"delta": "hi"},
        )
        assert unauth.status_code == 403
