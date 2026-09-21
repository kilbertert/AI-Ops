from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiops_diagnostics.gateway_store import (
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
