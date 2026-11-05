"""语义事实一致性只影响交付，不允许改变权限或恢复被取消的任务。"""

from types import SimpleNamespace

import pytest

from aiops_diagnostics.operator import operator_answer_review, operator_cpms
from operations.test_operator_agent import call_tool
from operations.test_operator_cpms import HEADERS, ORDER, environment, terminal


@pytest.mark.parametrize("verdict", ["reject", "uncertain", "unavailable"])
def test_unverified_reply_is_not_delivered(tmp_path, monkeypatch, verdict):
    with environment(tmp_path, monkeypatch) as f:
        monkeypatch.setattr(operator_answer_review, "review", lambda *args, **kwargs: {"verdict": verdict})

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
        ({"verdict": "allow", "currencies": [], "station_names": []}, "allow"),
        ({"verdict": "reject", "currencies": [], "station_names": []}, "reject"),
        ({"verdict": "allow", "extra": 1}, "unavailable"),
        ({"verdict": "allow", "currencies": "元", "station_names": []}, "unavailable"),
        ({"verdict": "allow", "currencies": [None], "station_names": []}, "unavailable"),
        ({"verdict": "allow", "currencies": [], "station_names": ["x"] * 33}, "unavailable"),
        ({"verdict": "unknown", "currencies": [], "station_names": []}, "unavailable"),
        ({"verdict": "allow", "currencies": ["元"], "station_names": []}, "reject"),
        ({"verdict": "allow", "currencies": [], "station_names": ["猜测站"]}, "reject"),
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
    # 复核输入必须携带本轮被拒工具清单——「查无此单」与「编造」靠它区分。
    assert '"tool_refusals"' in seen[0]["messages"][1]["content"]


@pytest.mark.parametrize("name,expected", [("Original Station", "allow"), ("猜测译名", "reject")])
def test_site_names_must_come_from_delivered_current_or_authorized_history(monkeypatch, name, expected):
    monkeypatch.setattr(operator_answer_review, "resolve_provider_api_key", lambda *a, **kw: "test")
    calls = []

    def model(**kwargs):
        calls.append(kwargs)
        return {"verdict": "allow", "currencies": [], "station_names": [name]}

    monkeypatch.setattr(operator_answer_review, "structured_request", model)
    settings = SimpleNamespace(select_provider=lambda _: SimpleNamespace(model="test"))
    history = [{"cpms_site_search": {"data": {"records": [{"name": "Original Station"}]}}}]
    result = operator_answer_review.review("解释上次站点", {}, [], "问", "zh", settings, history)
    assert result["verdict"] == expected and len(calls) == 1
    unresolved = {"cpms_station_plan": {"data": {"name": "Central"}}}
    assert operator_answer_review.known_facts([unresolved]) == set()
    resolved = {
        "cpms_station_plan": {
            "data": {"name": "Central", "site_id": "SITE-1", "rules": {"name": "Central Charging Station"}}
        }
    }
    assert operator_answer_review.known_facts([resolved]) == {"Central Charging Station"}


def test_review_unavailable_does_not_deliver(monkeypatch):
    monkeypatch.setattr(operator_answer_review, "resolve_provider_api_key", lambda *a, **kw: "test")

    def failed(**kwargs):
        raise ValueError("incomplete response")

    monkeypatch.setattr(operator_answer_review, "structured_request", failed)
    settings = SimpleNamespace(select_provider=lambda _: SimpleNamespace(model="test"))
    assert operator_answer_review.review("回答", {}, [], "问", "zh", settings)["verdict"] == "unavailable"
