"""语义事实一致性只影响交付，不允许改变权限或恢复被取消的任务。"""

from types import SimpleNamespace

import pytest

from aiops_diagnostics.operator import operator_answer_review, operator_cpms
from operations.test_operator_agent import call_tool
from operations.test_operator_cpms import HEADERS, ORDER, environment, terminal


@pytest.mark.parametrize("verdict", ["reject", "uncertain", "unavailable"])
def test_unverified_reply_is_not_delivered(tmp_path, monkeypatch, verdict):
    with environment(tmp_path, monkeypatch) as f:
        monkeypatch.setattr(operator_answer_review, "review", lambda *args: {"verdict": verdict})

        def dispatch(_, config, run):
            _, head, _ = call_tool(f, run, "cpms_order_detail")
            response = f["client"].post(
                f"/v1/internal/operator-runs/{run}/completions",
                headers=head,
                json={
                    "status": "completed",
                    "answer": "整单收费都正确",
                },
            )
            assert (
                response.status_code == 403
                and response.json()["error"]["code"] == "OPERATOR_ANSWER_NOT_GROUNDED"
            )

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        response = f["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "查询费用", "order_no": ORDER}
        )
        result = terminal(f, response.json()["qa_id"]).json()
        assert result["status"] == "failed" and result["result"] is None


@pytest.mark.parametrize("change", ["cancel", "revoke"])
def test_review_cannot_resurrect_cancelled_or_revoked_run(tmp_path, monkeypatch, change):
    with environment(tmp_path, monkeypatch) as f:

        def dispatch(_, config, run):
            _, head, _ = call_tool(f, run, "cpms_order_detail")

            def review(*args):
                if change == "cancel":
                    qa = f["ctx"].agent_run_store.get(run).qa_id
                    f["client"].post("/v1/assistant/questions/" + qa + "/cancel", headers=HEADERS)
                else:
                    from operations.test_operator_cpms import scope

                    f["resolver"].current = scope("OTHER")
                return {"verdict": "allow"}

            monkeypatch.setattr(operator_answer_review, "review", review)
            response = f["client"].post(
                f"/v1/internal/operator-runs/{run}/completions",
                headers=head,
                json={
                    "status": "completed",
                    "answer": "根据记录核对。",
                },
            )
            assert response.status_code == 403

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        response = f["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "查询费用", "order_no": ORDER}
        )
        qa = response.json()["qa_id"]
        future = f["ctx"].runtime._futures.get(qa)
        if future:
            future.result(timeout=5)
        assert f["ctx"].agent_run_store.by_qa(qa).state != "succeeded"


@pytest.mark.parametrize(
    "response,verdict",
    [
        ({"verdict": "allow"}, "allow"),
        ({"verdict": "reject"}, "reject"),
        ({"verdict": "allow", "extra": 1}, "unavailable"),
    ],
)
def test_review_uses_evidence_and_actual_reply_without_credentials(monkeypatch, response, verdict):
    seen = []
    monkeypatch.setattr(operator_answer_review, "resolve_provider_api_key", lambda *args, **kwargs: "key")
    monkeypatch.setattr(
        operator_answer_review, "structured_request", lambda **kwargs: seen.append(kwargs) or response
    )
    settings = SimpleNamespace(select_provider=lambda _: SimpleNamespace(model="test"))
    result = operator_answer_review.review("这笔需要复核", {"tool": "evidence"}, [], "为什么", "zh", settings)
    assert result["verdict"] == verdict
    assert "这笔需要复核" in seen[0]["messages"][1]["content"]
    assert "key" not in seen[0]["messages"][1]["content"]
