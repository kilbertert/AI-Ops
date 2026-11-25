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


def test_qa_events_seven_day_retention_enforced_on_read_and_write(tmp_path: Path) -> None:
    """回归测试 BUG_0007：无需手工调用 expire_now，7 天外事件在回放中立即过滤并在新事件写入时清扫。"""
    import sqlite3
    from contextlib import closing
    from datetime import timedelta

    from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc

    client, store = _client(tmp_path)
    scope_a = _MultiUserCaller().resolve("user-a", required_scope="aiops:diagnoses:write").scope_fingerprint
    with client:
        qa_old = store.create_assistant_question(scope_a, "旧问答", tenant_id="T-A")
        qa_new = store.create_assistant_question(scope_a, "新问答", tenant_id="T-A")
        events = client.app.state.gateway.qa_events
        events.append(qa_old["qa_id"], "message_delta", {"delta": "8天前草稿"})
        eight_days_ago = format_stored_time(now_utc() - timedelta(days=8))
        with closing(sqlite3.connect(tmp_path / "gateway.db")) as conn, conn:
            conn.execute(
                "UPDATE assistant_qa_events SET created_at = ? WHERE qa_id = ?",
                (eight_days_ago, qa_old["qa_id"]),
            )
        # Replay immediately excludes expired rows without calling expire_now().
        assert events.list_after(qa_old["qa_id"], after_seq=0) == []
        # Writing a new run_started triggers automatic sweep of expired rows.
        events.append(qa_new["qa_id"], "run_started", {"qa_id": qa_new["qa_id"]})
        with closing(sqlite3.connect(tmp_path / "gateway.db")) as conn:
            remaining_old = conn.execute(
                "SELECT COUNT(*) FROM assistant_qa_events WHERE qa_id = ?",
                (qa_old["qa_id"],),
            ).fetchone()[0]
        assert remaining_old == 0


def test_sse_live_order_reauthorization_and_nonblocking_event_loop(tmp_path: Path, monkeypatch) -> None:
    """回归测试 SEC_0001 与 BUG_0004：
    1. 运行中/失败态订单问答在订单改归不可见站点后，SSE 立即拒绝并绝不回放草稿正文；
    2. SSE 复核 CPMS 权限阻塞期间，同进程事件循环仍能立即响应其他请求。
    """
    import asyncio
    import threading
    import time

    import httpx

    from aiops_diagnostics.operator import operator_cpms
    from operations.test_operator_cpms import HEADERS, NODE_A_TOKEN, ORDER, environment

    with environment(tmp_path, monkeypatch) as env:
        order_bound = threading.Event()
        release_worker = threading.Event()

        def _dispatch(self, _cfg, run_id):
            # Grant order tool so run.order_no & task.order_site_id are bound, then push a draft delta
            from aiops_diagnostics.operator import operator_tools

            run = env["ctx"].agent_run_store.get(run_id)
            contract = (
                operator_cpms.OBJECT_CONTRACT
                if operator_tools.tool("cpms_order_detail").kind == "object"
                else operator_cpms.TOOL_CONTRACT
            )
            env["ctx"].operator_cpms.grant(
                run_id,
                NODE_A_TOKEN,
                run.expected_agent_version,
                "cpms_order_detail",
                contract,
                {"order_no": ORDER},
            )
            env["ctx"].operator_cpms.stream_chunk(
                run_id,
                NODE_A_TOKEN,
                run.expected_agent_version,
                "绝密订单草稿片段",
                chunk_index=0,
            )
            order_bound.set()
            release_worker.wait(timeout=5)
            raise RuntimeError("stop test worker")

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", _dispatch)

        ask = env["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": f"核对订单 {ORDER}", "order_no": ORDER},
        )
        assert ask.status_code == 202, ask.text
        qa_id = ask.json()["qa_id"]
        assert order_bound.wait(timeout=5)

        # SEC_0001: Move order to an invisible site -> SSE on running QA MUST refuse and leak zero draft!
        original_site = env["state"]["site"]
        env["state"]["site"] = "site-moved-out"
        blocked_running = env["client"].get(f"/v1/assistant/questions/{qa_id}/events", headers=HEADERS)
        assert blocked_running.status_code in {403, 404}
        assert "绝密订单草稿片段" not in blocked_running.text

        # Let worker fail -> SSE on failed QA MUST ALSO refuse and leak zero draft!
        release_worker.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            st = env["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS).json()["status"]
            if st == "failed":
                break
            time.sleep(0.02)
        blocked_failed = env["client"].get(f"/v1/assistant/questions/{qa_id}/events", headers=HEADERS)
        assert blocked_failed.status_code in {403, 404}
        assert "绝密订单草稿片段" not in blocked_failed.text

        # BUG_0004: Slow down CPMS check by 0.35s, prove /health still completes concurrently
        env["state"]["site"] = original_site
        real_cpms_get = operator_cpms.cpms_get
        entered_cpms = threading.Event()

        def _slow_cpms_get(*args, **kwargs):
            entered_cpms.set()
            time.sleep(0.35)
            return real_cpms_get(*args, **kwargs)

        monkeypatch.setattr(operator_cpms, "cpms_get", _slow_cpms_get)

        async def _verify_concurrent() -> float:
            transport = httpx.ASGITransport(app=env["app"])
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                sse_task = asyncio.create_task(
                    ac.get(f"/v1/assistant/questions/{qa_id}/events", headers=HEADERS)
                )
                for _ in range(50):
                    if entered_cpms.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert entered_cpms.is_set(), "expected SSE auth to enter CPMS check"
                t0 = time.monotonic()
                health = await ac.get("/health")
                health_elapsed = time.monotonic() - t0
                assert health.status_code == 200
                sse_resp = await sse_task
                assert sse_resp.status_code == 200
                return health_elapsed

        health_elapsed = asyncio.run(_verify_concurrent())
        assert health_elapsed < 0.25, f"event loop blocked during CPMS check ({health_elapsed:.3f}s)"
