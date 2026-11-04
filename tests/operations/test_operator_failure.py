"""节点失败经真实Gateway回读；不以一行日志代替用户可见结果。"""

import logging

import pytest
from execution.test_m2_close_contract import NODE_A_TOKEN

from aiops_diagnostics.execution.agent_runs import AgentRunDispatchError
from aiops_diagnostics.operator import operator_cpms
from operations.test_operator_cpms import APP, HEADERS, ORDER, environment, terminal


@pytest.fixture
def operator(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        yield f


@pytest.mark.parametrize("order", [ORDER, ""])
@pytest.mark.parametrize("dispatch_raises", [False, True])
@pytest.mark.parametrize(
    "reason,code",
    [
        ("model_quota_exhausted", "OPERATOR_MODEL_QUOTA_EXHAUSTED"),
        ("model_failed", "OPERATOR_DEPENDENCY_UNAVAILABLE"),
        ("node_failed", "OPERATOR_DEPENDENCY_UNAVAILABLE"),
        ("private_upstream", "OPERATOR_DEPENDENCY_UNAVAILABLE"),
        ("model_stream_incomplete", "OPERATOR_MODEL_OUTPUT_UNSTABLE"),
    ],
)
def test_node_failure_reaches_caller_without_readback(
    operator, monkeypatch, caplog, order, dispatch_raises, reason, code
):
    f = operator
    attempts = []

    def dispatch(_, config, run_id):
        attempts.append(run_id)
        headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
        publication = f["client"].post(
            "/v1/internal/charging-status/execution-config/" + run_id, headers=headers
        )
        headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
        response = f["client"].post(
            "/v1/internal/charging-status/completions/" + run_id,
            headers=headers,
            json={"status": "failed", "reason": reason},
        )
        assert response.status_code == 200, response.text
        if dispatch_raises:
            raise AgentRunDispatchError("agent platform unavailable")

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    with caplog.at_level(logging.WARNING, logger="aiops.operator"):
        posted = f["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={
                "question": "请分析电压" if order else "订单状态3是什么意思",
                **({"order_no": order} if order else {}),
            },
        )
        assert posted.status_code == 202, posted.text
        final = terminal(f, posted.json()["qa_id"])
    assert final.json()["status"] == "failed"
    assert final.json()["error"]["code"] == code, final.text
    assert not final.json()["result"]
    assert len(attempts) == 1
    with pytest.raises(operator_cpms.OperatorRefused, match="OPERATOR_RESULT_UNAVAILABLE"):
        f["ctx"].operator_cpms.authorize_readback(
            f["resolver"].current, posted.json()["qa_id"], "operator-private"
        )
    assert len(f["state"]["calls"]) == (1 if order else 0)
    assert "private_upstream" not in caplog.text
    assert "TypeError" not in caplog.text
    assert "OPERATOR_RESULT_UNAVAILABLE" not in caplog.text
    if reason == "private_upstream":
        run = f["ctx"].agent_run_store.by_qa(posted.json()["qa_id"])
        assert run.reason == "operator_node_failed: node_failed"
    if reason == "model_quota_exhausted":
        assert "额度不足" in final.json()["error"]["message"]
        assert final.json()["error"]["retryable"] is False


@pytest.mark.parametrize("native", [False, True])
def test_trial_quota_failure_has_actionable_message(operator, monkeypatch, native):
    import time

    from aiops_diagnostics.dify.dify_console_publish import _trial_report_text
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    f = operator
    f["settings"].environment = "env-a"
    f["settings"].dify_trial_api_key = "trial-only"
    with DifyWorkspaces(f["db"])._connection(write=True) as c:
        c.execute(
            "INSERT INTO dify_workspaces "
            "(tenant_id,environment,workspace_id,setup_state,created_at,updated_at) "
            "VALUES (?,?,?,'ready',?,?)",
            ("TENANT-A", "env-a", "workspace", "2026-10-26", "2026-10-26"),
        )
    monkeypatch.setattr(
        f["ctx"].operator_cpms,
        "console_identity",
        lambda session: {"token": "operator-private", "user_id": "OPERATOR-A", "tenant_id": "TENANT-A"},
    )

    def dispatch(_, config, run_id):
        headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
        publication = f["client"].post(
            "/v1/internal/charging-status/execution-config/" + run_id, headers=headers
        )
        headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
        response = f["client"].post(
            "/v1/internal/charging-status/completions/" + run_id,
            headers=headers,
            json={"status": "failed", "reason": "model_quota_exhausted"},
        )
        assert response.status_code == 200, response.text

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    head = {"Authorization": "Bearer trial-only", "X-AIOps-Console-Session": "valid-session"}
    payload = {"app_id": APP, "order_no": ORDER, "question": "请分析电压"}
    if native:
        payload["operator_preview"] = {
            "pre_prompt": "UNSAVED_DRAFT",
            "tools": [
                {"tool_name": name, "provider_id": "provider-id", "provider_type": "api", "enabled": True}
                for name in operator_cpms.TOOL_NAMES
            ],
        }
    response = f["client"].post("/v1/dify/trial", headers=head, json=payload)
    assert response.status_code == 202, response.text
    for _ in range(200):
        final = f["client"].get("/v1/dify/trial/" + response.json()["trial_id"], headers=head)
        assert final.status_code == 200, final.text
        if final.json()["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    body = final.json()
    assert body["status"] == "failed", body
    assert body["error_code"] == "OPERATOR_MODEL_QUOTA_EXHAUSTED"
    assert "额度不足" in body["error_message"]
    assert "补充额度" in _trial_report_text(body, "failed")
    assert body["result"] is None
    assert f["ctx"].agent_run_store.by_qa(response.json()["trial_id"]).snapshot is None
