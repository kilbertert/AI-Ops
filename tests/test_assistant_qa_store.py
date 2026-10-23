from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiops_diagnostics.gateway_store import (
    ASSISTANT_QUESTION_RESTART_ERROR_CODE,
    DIAGNOSIS_COMPLETED_RETENTION,
    DIAGNOSIS_FAILED_RETENTION,
    GatewayStore,
)


def _store(tmp_path: Path) -> GatewayStore:
    return GatewayStore(tmp_path / "gateway.db")


def test_create_get_assistant_question_no_order(tmp_path: Path) -> None:
    """A general-question job is created without any order identity and is
    retrievable by its scope only (T3/#153)."""
    store = _store(tmp_path)
    qa = store.create_assistant_question("scope-1", "什么是分时电价")
    assert qa["qa_id"].startswith("qa_")
    assert qa["status"] == "queued"
    assert "order_no" not in qa
    got = store.get_assistant_question(qa["qa_id"], "scope-1")
    assert got is not None
    assert got["question"] == "什么是分时电价"
    # other scope cannot see it
    assert store.get_assistant_question(qa["qa_id"], "scope-other") is None


def test_complete_assistant_question_no_order_field(tmp_path: Path) -> None:
    """A completed general answer carries plain result (no order_no)."""
    store = _store(tmp_path)
    qa = store.create_assistant_question("scope-1", "我的车续航为什么偏短")
    assert store.update_assistant_question(
        qa["qa_id"],
        status="completed",
        result={"text": "这与车况和工况有关。", "reminder": True},
    )
    got = store.get_assistant_question(qa["qa_id"], "scope-1")
    assert got["status"] == "completed"
    assert got["result"] == {"text": "这与车况和工况有关。", "reminder": True}
    assert "order_no" not in got


def test_late_completion_rejected_and_expired(tmp_path: Path) -> None:
    """A slow general-answer job expires past the deadline; late completion is
    refused (same guard as diagnoses)."""
    store = _store(tmp_path)
    qa = store.create_assistant_question("scope-1", "ok")
    store.update_assistant_question(qa["qa_id"], status="running")
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE assistant_questions SET deadline_at = ? WHERE qa_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), qa["qa_id"]),
        )
    assert not store.update_assistant_question(
        qa["qa_id"],
        status="completed",
        result={"text": "迟到", "reminder": False},
    )
    assert store.get_assistant_question(qa["qa_id"], "scope-1")["status"] == "expired"


def test_list_assistant_questions_scoped(tmp_path: Path) -> None:
    """History is per-scope: other callers do not see a question (T5 boundary)."""
    store = _store(tmp_path)
    store.create_assistant_question("scope-1", "问题A")
    store.create_assistant_question("scope-1", "问题B")
    store.create_assistant_question("scope-2", "问题C")
    s1 = store.list_assistant_questions("scope-1", limit=10)
    assert sorted(q["question"] for q in s1) == ["问题A", "问题B"]
    s2 = store.list_assistant_questions("scope-2", limit=10)
    assert [q["question"] for q in s2] == ["问题C"]
    assert len(s1) == 2
    assert len(s2) == 1


def test_cancelled_question_is_terminal_and_kept_for_the_refresh_window(tmp_path: Path) -> None:
    """`cancelled` is a terminal status retained on the short (failed) tier.

    The client holds the answer surface after a cancel, so the row only has to
    outlive the refresh window: neither "never expires" (unbounded rows) nor
    "expires now" (the user 404s while 「已停止」 is still on screen).
    """
    store = _store(tmp_path)
    qa = store.create_assistant_question("scope-1", "问题")
    assert store.update_assistant_question(qa["qa_id"], status="running")
    assert store.update_assistant_question(qa["qa_id"], status="cancelled")
    stored = store.get_assistant_question(qa["qa_id"], "scope-1")
    assert stored["status"] == "cancelled"
    assert stored["completed_at"] is not None
    expires = datetime.fromisoformat(stored["expires_at"])
    assert expires > datetime.now(UTC)
    assert expires <= datetime.now(UTC) + DIAGNOSIS_FAILED_RETENTION
    assert expires < datetime.now(UTC) + DIAGNOSIS_COMPLETED_RETENTION


def test_a_completed_answer_survives_its_retention_window(tmp_path: Path) -> None:
    """交付了答案的作业，过了保留期**仍然是 completed**，答案可读。

    41 上实测（2026-10-23）：诊断完成后 15 分钟，同一条行被 sweep 成 `expired`，
    而 `/v1/standard/diagnoses/{id}` 对 `expired` 返回 `result: null` —— 用户看到的
    是「没找到」，而库里那条答案完好无损。用户的原话是「同一个问题，有时候能回，
    有时候显示没找到，我一个字都没换」。

    这里钉住的是**保留期不得改写已交付的结果**。保留期原本的用途是「一条行留多久」，
    而 `sweep` 做的事是把它标成 `expired`；`expired` 在 API 层等于「没有结果」。
    于是设置保留期的唯一可见后果，是把一个正确答案变成一次失败。

    `expired` 仍然属于终态集合（客户端契约见 `docs/agents/testing-api-reference.md`
    §2.2），只是不再由「交付后过了保留期」产生 —— 那是 `claim` 的职责，只作用于
    仍在跑的作业。
    """
    store = _store(tmp_path)
    qa = store.create_assistant_question("scope-1", "订单状态是什么")
    store.update_assistant_question(
        qa["qa_id"],
        status="completed",
        result={"text": "订单已完成", "reminder": False},
    )
    # 让这条行"刚过保留期"：不动 status，只把 expires_at 拨到过去。
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE assistant_questions SET expires_at = ? WHERE qa_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), qa["qa_id"]),
        )

    got = store.get_assistant_question(qa["qa_id"], "scope-1")

    assert got["status"] == "completed", got["status"]
    assert got["result"] == {"text": "订单已完成", "reminder": False}


def test_cancelled_question_expires_after_its_retention_window(tmp_path: Path) -> None:
    """The retention window is enforced: a cancelled row is swept to `expired`
    once it passes, so `cancelled` is not an unbounded status."""
    store = _store(tmp_path)
    qa = store.create_assistant_question("scope-1", "问题")
    store.update_assistant_question(qa["qa_id"], status="cancelled")
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE assistant_questions SET expires_at = ? WHERE qa_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), qa["qa_id"]),
        )
    assert store.get_assistant_question(qa["qa_id"], "scope-1")["status"] == "expired"


def test_late_worker_cannot_overwrite_a_cancelled_question(tmp_path: Path) -> None:
    """The claim-guard already refuses late writes against any terminal status:
    a worker finishing after the cancel keeps the row `cancelled`, and
    re-cancelling is a no-op instead of an error. This is the storage-layer
    basis for cancel idempotency (PRD #346)."""
    store = _store(tmp_path)
    qa = store.create_assistant_question("scope-1", "问题")
    store.update_assistant_question(qa["qa_id"], status="running")
    assert store.update_assistant_question(qa["qa_id"], status="cancelled")
    assert not store.update_assistant_question(
        qa["qa_id"],
        status="completed",
        result={"text": "迟到的回答", "reminder": False},
    )
    assert not store.update_assistant_question(qa["qa_id"], status="cancelled")
    stored = store.get_assistant_question(qa["qa_id"], "scope-1")
    assert stored["status"] == "cancelled"
    assert stored["result"] is None
    assert stored["error_code"] is None


def test_running_question_is_converged_to_failed_on_restart(tmp_path: Path) -> None:
    """A question still in flight when the gateway restarts ends immediately.

    The only other way out was the 15-minute deadline, so a deploy left the
    caller staring at a spinner for a job whose worker no longer exists. The
    recovery converges it to `failed` — the status says the answer is gone, and
    the error code says why, so an operator can tell a restart from a timeout
    (`expired`) and from a user stop (`cancelled`).
    """
    database = tmp_path / "gateway.db"
    store = GatewayStore(database)
    qa = store.create_assistant_question("scope-1", "什么是分时电价")
    store.update_assistant_question(qa["qa_id"], status="running")

    restarted = GatewayStore(database)
    restarted.recover_assistant_questions()

    stored = restarted.get_assistant_question(qa["qa_id"], "scope-1")
    assert stored["status"] == "failed"
    assert stored["error_code"] == ASSISTANT_QUESTION_RESTART_ERROR_CODE
    assert stored["result"] is None
    assert stored["completed_at"] is not None


def test_restart_recovery_is_a_noop_for_terminal_questions(tmp_path: Path) -> None:
    """Recovery only converges in-flight work; a finished row keeps its result.

    Every status outside `queued`/`running` is left exactly as it was —
    including a row an earlier recovery already converged — so a restart (or a
    second recovery) can never rewrite an answer or a user's own stop.
    """
    database = tmp_path / "gateway.db"
    store = GatewayStore(database)
    completed = store.create_assistant_question("scope-1", "已回答")
    store.update_assistant_question(
        completed["qa_id"], status="completed", result={"text": "答案", "reminder": False}
    )
    cancelled = store.create_assistant_question("scope-1", "已取消")
    store.update_assistant_question(cancelled["qa_id"], status="cancelled")

    restarted = GatewayStore(database)
    restarted.recover_assistant_questions()
    restarted.recover_assistant_questions()

    answered = restarted.get_assistant_question(completed["qa_id"], "scope-1")
    assert answered["status"] == "completed"
    assert answered["result"] == {"text": "答案", "reminder": False}
    assert answered["error_code"] is None
    stopped = restarted.get_assistant_question(cancelled["qa_id"], "scope-1")
    assert stopped["status"] == "cancelled"
    assert stopped["error_code"] is None
