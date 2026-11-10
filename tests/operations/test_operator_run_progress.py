"""用户等待时可见真实取证和复核阶段，阶段记录不交付答案。"""

import threading

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
        set(item) <= {"phase", "status", "at", "elapsed_ms", "duration_ms", "tool"} for item in run.trace
    )
