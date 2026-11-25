"""回答点赞/点踩反馈与会话最后一轮原地重新生成测试（#1712）。"""

from __future__ import annotations

import time
from typing import Any

from aiops_diagnostics.operator import operator_cpms
from operations.test_operator_cpms import HEADERS, ORDER, environment, node


def _wait_completed(client, qa_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        res = client.get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        if body["status"] in {"completed", "failed", "cancelled"}:
            return body
        time.sleep(0.02)
    raise AssertionError("qa did not complete in time")


def test_feedback_upsert_cancel_isolation_and_readback(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as env:
        monkeypatch.setattr(
            operator_cpms.SnailDispatcher,
            "dispatch",
            lambda self, _cfg, run_id: node(env, run_id, tools=("cpms_order_detail",)),
        )
        ask = env["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": f"核对订单 {ORDER} 状态", "order_no": ORDER},
        )
        assert ask.status_code == 202, ask.text
        qa_id = ask.json()["qa_id"]
        conv_id = ask.json()["conversation_id"]
        done = _wait_completed(env["client"], qa_id)
        assert done["status"] == "completed"
        assert done["feedback"] is None

        # Submit thumbs up
        r_up = env["client"].post(
            f"/v1/assistant/questions/{qa_id}/feedback",
            headers=HEADERS,
            json={"rating": "up", "reasons": ["结论准确"], "comment": "排查非常清楚"},
        )
        assert r_up.status_code == 200, r_up.text
        fb = r_up.json()
        assert fb["qa_id"] == qa_id
        assert fb["rating"] == "up"
        assert fb["reasons"] == ["结论准确"]
        assert fb["comment"] == "排查非常清楚"

        # Hydrated on GET /v1/assistant/questions/{qa_id} and GET /v1/conversations/{conversation_id}
        q_read = env["client"].get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS).json()
        assert q_read["feedback"]["rating"] == "up"
        conv_read = env["client"].get(f"/v1/conversations/{conv_id}", headers=HEADERS).json()
        assert conv_read["turns"][0]["feedback"]["rating"] == "up"

        # Idempotent switch to thumbs down with multiple reasons
        r_down = env["client"].post(
            f"/v1/assistant/questions/{qa_id}/feedback",
            headers=HEADERS,
            json={"rating": "down", "reasons": ["缺少计费明细", "缺少计费明细"], "comment": "需要补查"},
        )
        assert r_down.status_code == 200
        assert r_down.json()["rating"] == "down"
        assert r_down.json()["reasons"] == ["缺少计费明细"]

        # Cancel rating by passing rating=None
        r_clear = env["client"].post(
            f"/v1/assistant/questions/{qa_id}/feedback",
            headers=HEADERS,
            json={"rating": None},
        )
        assert r_clear.status_code == 200
        assert r_clear.json()["rating"] is None
        assert r_clear.json()["reasons"] == []
        assert r_clear.json()["comment"] is None

        # Non-completed QA rejects feedback with 409 FEEDBACK_NOT_ALLOWED
        store = env["app"].state.gateway.store
        queued = store.create_assistant_question(
            scope_fingerprint=env["resolver"].current.scope_fingerprint,
            question="尚未完成的问答",
        )
        r_not_ready = env["client"].post(
            f"/v1/assistant/questions/{queued['qa_id']}/feedback",
            headers=HEADERS,
            json={"rating": "up"},
        )
        assert r_not_ready.status_code == 409
        assert r_not_ready.json()["error"]["code"] == "FEEDBACK_NOT_ALLOWED"

        # Non-existent / cross-tenant QA -> 404 QA_NOT_FOUND
        r_missing = env["client"].post(
            "/v1/assistant/questions/qa_00000000000000000000000000000000/feedback",
            headers=HEADERS,
            json={"rating": "up"},
        )
        assert r_missing.status_code == 404
        assert r_missing.json()["error"]["code"] == "QA_NOT_FOUND"


def test_regenerate_latest_turn_in_place_excludes_old_answer_and_rejects_non_latest(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as env:
        captured_messages: list[list[dict[str, Any]]] = []
        answers = iter(
            [
                "第一轮结果：已完成初步查看。",
                "第二轮第一次结果：旧版本结论。",
                "第二轮重新生成结果：全新复查结论。",
            ]
        )

        def _fake_dispatch(self, _cfg, run_id):
            task = env["ctx"].operator_cpms.tasks[run_id]
            captured_messages.append(list(task.messages))
            node(env, run_id, tools=("cpms_order_detail",), answer=next(answers))

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", _fake_dispatch)

        # Turn 1
        t1 = env["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": f"先查一下订单 {ORDER}", "order_no": ORDER},
        )
        assert t1.status_code == 202, t1.text
        qa1 = t1.json()["qa_id"]
        conv_id = t1.json()["conversation_id"]
        assert _wait_completed(env["client"], qa1)["status"] == "completed"

        # Turn 2 in same conversation
        t2 = env["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": "请再分析一次停充原因", "conversation_id": conv_id},
        )
        assert t2.status_code == 202, t2.text
        qa2 = t2.json()["qa_id"]
        assert t2.json()["turn_no"] == 2
        assert _wait_completed(env["client"], qa2)["status"] == "completed"

        # Regenerating Turn 1 (non-latest) MUST fail with 409 REGENERATE_NOT_ALLOWED
        bad_regen = env["client"].post(f"/v1/assistant/questions/{qa1}/regenerate", headers=HEADERS)
        assert bad_regen.status_code == 409
        assert bad_regen.json()["error"]["code"] == "REGENERATE_NOT_ALLOWED"

        # Regenerating Turn 2 (latest) succeeds in-place with attempt_no=2 & parent_qa_id=qa2
        regen = env["client"].post(f"/v1/assistant/questions/{qa2}/regenerate", headers=HEADERS)
        assert regen.status_code == 202, regen.text
        body = regen.json()
        qa2_v2 = body["qa_id"]
        assert qa2_v2 != qa2
        assert body["conversation_id"] == conv_id
        assert body["turn_no"] == 2
        assert body["attempt_no"] == 2
        assert body["parent_qa_id"] == qa2
        assert body["stream_url"] == f"/v1/assistant/questions/{qa2_v2}/events"

        done_v2 = _wait_completed(env["client"], qa2_v2)
        assert done_v2["status"] == "completed"
        assert done_v2["attempt_no"] == 2
        assert done_v2["parent_qa_id"] == qa2
        assert "全新复查结论" in done_v2["result"]["blocks"][0]["text"]

        # Context sent to model for regenerated Turn 2 MUST include Turn 1 and EXCLUDE old Turn 2 answer!
        assert len(captured_messages) == 3
        regen_msgs_text = "\n".join(str(m.get("content") or "") for m in captured_messages[2])
        assert "第一轮结果" in regen_msgs_text
        assert "旧版本结论" not in regen_msgs_text

        # Conversation detail still has exactly 2 turns (in-place update, no duplicate turn 3!)
        conv = env["client"].get(f"/v1/conversations/{conv_id}", headers=HEADERS).json()
        assert conv["turn_count"] == 2
        assert len(conv["turns"]) == 2
        assert conv["turns"][1]["turn_no"] == 2
        assert conv["turns"][1]["qa_id"] == qa2_v2
        assert conv["turns"][1]["attempt_no"] == 2
        assert conv["turns"][1]["parent_qa_id"] == qa2
        assert "全新复查结论" in conv["turns"][1]["answer"]["blocks"][0]["text"]

        # Consumer entry cannot call regenerate (403)
        r_consumer = env["client"].post(
            f"/v1/assistant/questions/{qa2_v2}/regenerate",
            headers={**HEADERS, "X-Business-Entry": "consumer"},
        )
        assert r_consumer.status_code == 403


def test_regenerate_failure_preserves_existing_turn_and_releases_busy_lock(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as env:
        calls = {"n": 0}

        def _dispatch(self, _cfg, run_id):
            calls["n"] += 1
            if calls["n"] == 1:
                node(env, run_id, tools=("cpms_order_detail",), answer="首版稳定结论。")
                return
            raise RuntimeError("simulated dispatch failure")

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", _dispatch)
        ask = env["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": f"排查订单 {ORDER}", "order_no": ORDER},
        )
        assert ask.status_code == 202
        qa1 = ask.json()["qa_id"]
        conv_id = ask.json()["conversation_id"]
        assert _wait_completed(env["client"], qa1)["status"] == "completed"

        regen = env["client"].post(f"/v1/assistant/questions/{qa1}/regenerate", headers=HEADERS)
        assert regen.status_code == 202
        qa2 = regen.json()["qa_id"]
        assert _wait_completed(env["client"], qa2)["status"] == "failed"

        # Original completed turn MUST remain intact and conversation lock MUST be released!
        conv = env["client"].get(f"/v1/conversations/{conv_id}", headers=HEADERS).json()
        assert conv["is_generating"] is False
        assert conv["turn_count"] == 1
        assert conv["turns"][0]["qa_id"] == qa1
        assert "首版稳定结论" in conv["turns"][0]["answer"]["blocks"][0]["text"]


def test_stale_cancelled_regenerate_cannot_unlock_or_overwrite_newer_regenerate(tmp_path, monkeypatch):
    """回归测试 BUG_0005：取消重生成 A 并立刻启动重生成 B 后，A 延迟收尾不得解锁 B 或覆写答复。"""
    import threading

    with environment(tmp_path, monkeypatch) as env:
        release_a = threading.Event()
        release_b = threading.Event()
        a_entered = threading.Event()
        b_entered = threading.Event()
        call_no = {"n": 0}

        def _dispatch(self, _cfg, run_id):
            call_no["n"] += 1
            n = call_no["n"]
            if n == 1:
                node(env, run_id, tools=("cpms_order_detail",), answer="首版结论。")
                return
            if n == 2:
                a_entered.set()
                release_a.wait(timeout=5)
                raise RuntimeError("stale regen A delayed failure")
            b_entered.set()
            release_b.wait(timeout=5)
            node(env, run_id, tools=("cpms_order_detail",), answer="新重生成 B 结论。")

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", _dispatch)

        # Turn 1 initial completion
        t1 = env["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": f"排查订单 {ORDER}", "order_no": ORDER},
        )
        assert t1.status_code == 202
        qa1 = t1.json()["qa_id"]
        conv_id = t1.json()["conversation_id"]
        assert _wait_completed(env["client"], qa1)["status"] == "completed"

        # Start Regen A, cancel it while still running in worker
        r_a = env["client"].post(f"/v1/assistant/questions/{qa1}/regenerate", headers=HEADERS)
        assert r_a.status_code == 202
        qa_a = r_a.json()["qa_id"]
        assert a_entered.wait(timeout=5)
        cancel_a = env["client"].post(f"/v1/assistant/questions/{qa_a}/cancel", headers=HEADERS)
        assert cancel_a.status_code == 200

        # Immediately start Regen B (reuses turn_no=1 with a new claim_id)
        r_b = env["client"].post(f"/v1/assistant/questions/{qa1}/regenerate", headers=HEADERS)
        assert r_b.status_code == 202
        qa_b = r_b.json()["qa_id"]
        assert b_entered.wait(timeout=5)

        # Let stale worker A finish & fail -> MUST NOT unlock B's conversation slot!
        release_a.set()
        time.sleep(0.15)
        r_c = env["client"].post(f"/v1/assistant/questions/{qa1}/regenerate", headers=HEADERS)
        assert r_c.status_code == 409
        assert r_c.json()["error"]["code"] == "CONVERSATION_BUSY"

        # Let B finish cleanly -> B persists and unlocks cleanly
        release_b.set()
        done_b = _wait_completed(env["client"], qa_b)
        assert done_b["status"] == "completed"
        conv = env["client"].get(f"/v1/conversations/{conv_id}", headers=HEADERS).json()
        assert conv["is_generating"] is False
        assert conv["turns"][0]["qa_id"] == qa_b
        assert "新重生成 B 结论" in conv["turns"][0]["answer"]["blocks"][0]["text"]
