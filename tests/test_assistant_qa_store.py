from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiops_diagnostics.gateway_store import GatewayStore


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
