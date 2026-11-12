"""用户等待时可见真实取证和复核阶段，阶段记录不交付答案。"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from aiops_diagnostics.operator import operator_answer_review, operator_cpms
from operations.test_operator_cpms import HEADERS, node, operator, request, terminal  # noqa: F401


def test_progress_during_review_and_after_completion(operator, monkeypatch):  # noqa: F811
    entered, release = threading.Event(), threading.Event()

    def review(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"verdict": "allow"}

    monkeypatch.setattr(operator_answer_review, "review", review)
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, config, run: node(operator, run))
    qa_id = request(operator["client"]).json()["qa_id"]
    try:
        assert entered.wait(5)
        response = operator["client"].get("/v1/assistant/questions/" + qa_id, headers=HEADERS)
        body = response.json()
        assert body["status"] == "running"
        assert body["result"] is None
        assert body["progress"]["phase"] == "review"
        assert body["progress"]["tool_count"] == 2
        assert body["progress"]["elapsed_ms"] >= 0
    finally:
        release.set()
    assert terminal(operator, qa_id).json()["status"] == "completed"
    run = operator["ctx"].agent_run_store.by_qa(qa_id)
    assert [
        item["tool"] for item in run.trace if item["phase"] == "tool" and item["status"] == "succeeded"
    ] == ["cpms_order_detail", "cpms_charging_samples"]
    assert any(item["phase"] == "review" and item["status"] == "succeeded" for item in run.trace)
    assert run.trace[-1]["phase"] == "completed"
    assert all(
        set(item) <= {"phase", "status", "at", "elapsed_ms", "duration_ms", "tool", "detail"}
        for item in run.trace
    )
    # ADR-0251：派发携带工具数、复核通过也都落机器码（detail 只允许码，不是自由文本）。
    assert any(
        item["phase"] == "dispatch" and str(item.get("detail", "")).startswith("tools=") for item in run.trace
    )
    assert any(
        item["phase"] == "review" and item["status"] == "succeeded" and item.get("detail") == "allowed"
        for item in run.trace
    )


def test_trace_write_failure_does_not_leave_question_running(operator, monkeypatch):  # noqa: F811
    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(operator["ctx"].agent_run_store, "record_trace", locked)
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, config, run: node(operator, run))
    qa = request(operator["client"]).json()["qa_id"]
    assert terminal(operator, qa).json()["status"] == "completed"


def test_trace_is_bounded_and_survives_snapshot_updates(operator, monkeypatch):  # noqa: F811
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, config, run: node(operator, run))
    qa = request(operator["client"]).json()["qa_id"]
    assert terminal(operator, qa).json()["status"] == "completed"
    store = operator["ctx"].agent_run_store
    run = store.by_qa(qa)
    first = run.trace[0]
    for elapsed in range(150):
        store.record_trace(
            run.run_id,
            {"phase": "tool", "status": "succeeded", "at": "2026-11-10T00:00:00Z", "elapsed_ms": elapsed},
        )
    store.absorb(run.run_id, {"evidence": {}})
    saved = store.by_qa(qa)
    assert saved.state == run.state and saved.snapshot == run.snapshot
    assert len(saved.trace) == 128 and saved.trace[0] == first
    assert saved.trace[-1]["elapsed_ms"] == 149


def test_cancel_does_not_wait_for_tool_lock(operator, monkeypatch):  # noqa: F811
    entered, release = threading.Event(), threading.Event()

    def dispatch(_, config, run):
        with operator["ctx"].operator_cpms.tasks[run].lock:
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    qa = request(operator["client"]).json()["qa_id"]
    assert entered.wait(5)
    with ThreadPoolExecutor() as pool:
        stopped = pool.submit(
            operator["client"].post, "/v1/assistant/questions/" + qa + "/cancel", headers=HEADERS
        )
        try:
            assert stopped.result(timeout=1).json()["status"] == "cancelled"
        finally:
            release.set()


def test_failed_readback_is_not_recorded_as_completed(operator, monkeypatch):  # noqa: F811
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, config, run: node(operator, run))

    def withdrawn(*args):
        raise operator_cpms.OperatorRefused("OPERATOR_RESULT_RETRACTED")

    monkeypatch.setattr(operator["ctx"].operator_cpms, "authorize_readback", withdrawn)
    qa = request(operator["client"]).json()["qa_id"]
    assert terminal(operator, qa).json()["status"] == "failed"
    trace = operator["ctx"].agent_run_store.by_qa(qa).trace
    assert trace[-1]["phase"] == "failed"
    assert not any(event["phase"] == "completed" for event in trace)
