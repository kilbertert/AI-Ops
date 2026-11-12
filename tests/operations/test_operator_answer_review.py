"""语义事实一致性只影响交付，不允许改变权限或恢复被取消的任务。"""

from types import SimpleNamespace

import pytest

from aiops_diagnostics.operator import operator_answer_review, operator_cpms
from operations.test_operator_agent import call_tool
from operations.test_operator_cpms import HEADERS, ORDER, environment, terminal


@pytest.mark.parametrize("verdict", ["reject", "uncertain", "unavailable"])
def test_unverified_reply_is_delivered_with_verdict_recorded(tmp_path, monkeypatch, verdict):
    """复核 verdict 降为观察（ADR-0252）：答复照常交付，判定落进快照与轨迹。"""
    with environment(tmp_path, monkeypatch) as f:
        monkeypatch.setattr(operator_answer_review, "review", lambda *args, **kwargs: {"verdict": verdict})
        attempts = []

        def dispatch(_, config, run):
            attempts.append(run)
            _, head, _ = call_tool(f, run, "cpms_order_detail")
            response = f["client"].post(
                f"/v1/internal/operator-runs/{run}/completions",
                headers=head,
                json={
                    "status": "completed",
                    "answer": "整单收费都正确",
                },
            )
            assert response.status_code == 200

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        response = f["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "查询费用", "order_no": ORDER}
        )
        result = terminal(f, response.json()["qa_id"]).json()
        assert result["status"] == "completed"
        text = result["result"]["blocks"][0]["text"]
        assert "整单收费都正确" in text and "诊断结论未完成" not in text
        assert len(attempts) == 1
        run = f["ctx"].agent_run_store.by_qa(response.json()["qa_id"])
        assert run.snapshot["answer"] == "整单收费都正确"
        assert run.snapshot["answer_review"]["verdict"] == verdict
        assert run.snapshot["delivery_mode"] == "diagnosis"
        assert not any(event["phase"] == "resample" for event in run.trace)


@pytest.mark.parametrize("change", ["cancel", "revoke"])
@pytest.mark.parametrize("verdict", ["allow", "reject"])
def test_review_cannot_resurrect_cancelled_or_revoked_run(tmp_path, monkeypatch, change, verdict):
    with environment(tmp_path, monkeypatch) as f:

        def dispatch(_, config, run):
            _, head, _ = call_tool(f, run, "cpms_order_detail")

            def review(*args, **kwargs):
                if change == "cancel":
                    qa = f["ctx"].agent_run_store.get(run).qa_id
                    f["client"].post("/v1/assistant/questions/" + qa + "/cancel", headers=HEADERS)
                else:
                    from operations.test_operator_cpms import scope

                    f["resolver"].current = scope("OTHER")
                return {"verdict": verdict}

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
        ({"verdict": "allow", "currencies": ["元"], "station_names": []}, "allow"),
        ({"verdict": "reject", "currencies": ["元"], "station_names": []}, "reject"),
        ({"verdict": "allow", "currencies": ["元"], "station_names": ["猜测站"]}, "reject"),
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


@pytest.mark.parametrize("exc", [ValueError("incomplete response"), AttributeError("no provider")])
def test_review_unavailable_does_not_deliver(monkeypatch, exc):
    monkeypatch.setattr(operator_answer_review, "resolve_provider_api_key", lambda *a, **kw: "test")

    def failed(**kwargs):
        raise exc

    monkeypatch.setattr(operator_answer_review, "structured_request", failed)
    settings = SimpleNamespace(select_provider=lambda _: SimpleNamespace(model="test"))
    assert operator_answer_review.review("回答", {}, [], "问", "zh", settings)["verdict"] == "unavailable"


def test_review_settings_without_provider_selection_is_unavailable(monkeypatch):
    """settings/provider 缺属性同样是复核自身不可用（ADR-0252），不穿出异常。"""
    settings = SimpleNamespace()  # 无 select_provider → AttributeError
    assert operator_answer_review.review("回答", {}, [], "问", "zh", settings)["verdict"] == "unavailable"


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("quota_exhausted", "review_quota_exhausted"),
        ("auth_rejected", "review_auth_rejected"),
        ("rate_limited", "review_unavailable"),
    ],
)
def test_review_dependency_failure_keeps_only_a_fixed_reason(monkeypatch, reason, expected):
    from aiops_diagnostics.common.model_request import ModelRequestError

    monkeypatch.setattr(operator_answer_review, "resolve_provider_api_key", lambda *a, **kw: "private-key")

    def failed(**kwargs):
        raise ModelRequestError(reason, 403)

    monkeypatch.setattr(operator_answer_review, "structured_request", failed)
    settings = SimpleNamespace(select_provider=lambda _: SimpleNamespace(model="test"))
    result = operator_answer_review.review("回答", {}, [], "问", "zh", settings)
    assert result["verdict"] == "unavailable" and result["reason"] == expected
    assert "private-key" not in str(result)


def test_post_review_gate_refusal_is_terminal_not_resampled(tmp_path, monkeypatch):
    """复核后的授权/范围闸拒仍是终局（ADR-0252）：原码落 run.reason，不按「没交出答复」重派。"""
    with environment(tmp_path, monkeypatch) as f:
        attempts, saved = [], {}

        def dispatch(_, config, run):
            attempts.append(run)
            _, head, _ = call_tool(f, run, "cpms_order_detail")

            def review(*a, **kw):
                saved["current"] = f["resolver"].current
                from operations.test_operator_cpms import scope

                f["resolver"].current = scope("OTHER")
                return {"verdict": "allow"}

            monkeypatch.setattr(operator_answer_review, "review", review)
            response = f["client"].post(
                f"/v1/internal/operator-runs/{run}/completions",
                headers=head,
                json={"status": "completed", "answer": "核对完毕"},
            )
            assert response.status_code == 200 and response.json()["delivered"] is False

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        response = f["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "查询费用", "order_no": ORDER}
        )
        qa = response.json()["qa_id"]
        future = f["ctx"].runtime._futures.get(qa)
        if future:
            future.result(timeout=10)
        f["resolver"].current = saved["current"]
        result = terminal(f, qa).json()
        assert result["status"] == "failed"
        assert result["error"]["code"] == "OPERATOR_DEPENDENCY_UNAVAILABLE"
        assert len(attempts) == 1
        run = f["ctx"].agent_run_store.by_qa(qa)
        assert run.state == "failed"
        assert run.reason == "tool_refused:OPERATOR_SCOPE_CHANGED"
        assert not any(event["phase"] == "resample" for event in run.trace)


@pytest.mark.parametrize("dispatch_raises", [False, True])
def test_post_review_refusal_survives_stamp_failure(tmp_path, monkeypatch, dispatch_raises):
    """stamp 落账失败时也按 completion_refusal 原码终结（ADR-0252）：不重派。

    dispatch_raises=True 覆盖派发运输层抛错分支：闸拒兜底在两条 worker 路径
    （正常返回未推进 / AgentRunDispatchError）下都必须拦住重派。
    """
    with environment(tmp_path, monkeypatch) as f:
        attempts, saved = [], []
        monkeypatch.setattr(
            operator_cpms.OperatorCpms, "_stamp_gate_refusal", lambda self, run_id, code: None
        )

        def dispatch(_, config, run):
            attempts.append(run)
            _, head, _ = call_tool(f, run, "cpms_order_detail")

            def review(*a, **kw):
                saved.append(f["resolver"].current)
                from operations.test_operator_cpms import scope

                f["resolver"].current = scope("OTHER")
                return {"verdict": "allow"}

            monkeypatch.setattr(operator_answer_review, "review", review)
            f["client"].post(
                f"/v1/internal/operator-runs/{run}/completions",
                headers=head,
                json={"status": "completed", "answer": "核对完毕"},
            )
            if dispatch_raises:
                raise operator_cpms.AgentRunDispatchError("transport after refused completion")

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        response = f["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "查询费用", "order_no": ORDER}
        )
        qa = response.json()["qa_id"]
        future = f["ctx"].runtime._futures.get(qa)
        if future:
            future.result(timeout=10)
        if saved:
            f["resolver"].current = saved[0]
        result = terminal(f, qa).json()
        assert result["status"] == "failed"
        assert result["error"]["code"] == "OPERATOR_DEPENDENCY_UNAVAILABLE"
        assert len(attempts) == 1
        run = f["ctx"].agent_run_store.by_qa(qa)
        assert run.state == "failed"
        assert not any(event["phase"] == "resample" for event in run.trace)


@pytest.mark.parametrize("dispatch_raises", [False, True])
def test_rejected_reply_without_evidence_is_delivered(tmp_path, monkeypatch, dispatch_raises):
    """复核拒绝不再拦截交付（ADR-0252）：回执收答复、判拒只记录，运输错误不撤销。"""
    from execution.test_m2_close_contract import NODE_A_TOKEN

    with environment(tmp_path, monkeypatch) as f:
        monkeypatch.setattr(operator_answer_review, "review", lambda *a, **kw: {"verdict": "reject"})
        calls = []

        def dispatch(_, config, run):
            calls.append(run)
            headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
            published = (
                f["client"]
                .post("/v1/internal/charging-status/execution-config/" + run, headers=headers)
                .json()
            )
            headers["X-AI-Ops-Exec-Config"] = published["sha256"]
            response = f["client"].post(
                f"/v1/internal/operator-runs/{run}/completions",
                headers=headers,
                json={"status": "completed", "answer": "已为你退款。"},
            )
            assert response.status_code == 200
            if dispatch_raises:
                raise operator_cpms.AgentRunDispatchError("transport after accepted completion")

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        response = f["client"].post("/v1/assistant/questions", headers=HEADERS, json={"question": "帮我退款"})
        result = terminal(f, response.json()["qa_id"]).json()
        assert result["status"] == "completed"
        assert "已为你退款" in result["result"]["blocks"][0]["text"]
        assert len(calls) == 1
        run = f["ctx"].agent_run_store.by_qa(response.json()["qa_id"])
        assert run.snapshot["answer_review"]["verdict"] == "reject"
        assert run.snapshot["delivery_mode"] == "diagnosis"
