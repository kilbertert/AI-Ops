"""节点失败经真实Gateway回读；不以一行日志代替用户可见结果。"""

import logging
import threading

import pytest
from execution.test_m2_close_contract import NODE_A_TOKEN

from aiops_diagnostics.execution.agent_runs import AgentRunDispatchError
from aiops_diagnostics.operator import operator_cpms
from operations.test_operator_cpms import (
    APP,
    CONTRACT,
    HEADERS,
    ORDER,
    environment,
    request,
    terminal,
)


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
        # 节点保留**原始**工具拒绝码上报：归因留原码，对用户的码仍归一类。
        ("tool_refused:OPERATOR_SCOPE_CHANGED", "OPERATOR_DEPENDENCY_UNAVAILABLE"),
        ("tool_refused:cpms_unavailable", "OPERATOR_DEPENDENCY_UNAVAILABLE"),
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
    assert len(f["state"]["calls"]) == 0
    assert "private_upstream" not in caplog.text
    assert "TypeError" not in caplog.text
    assert "OPERATOR_RESULT_UNAVAILABLE" not in caplog.text
    if reason == "private_upstream":
        run = f["ctx"].agent_run_store.by_qa(posted.json()["qa_id"])
        assert run.reason == "operator_node_failed: node_failed"
    if reason.startswith("tool_refused:"):
        # 原始拒绝码逐字进入运行记录——不再糊成 node_failed。
        run = f["ctx"].agent_run_store.by_qa(posted.json()["qa_id"])
        assert run.reason == "operator_node_failed: " + reason
    if reason == "model_quota_exhausted":
        assert "额度不足" in final.json()["error"]["message"]
        assert final.json()["error"]["retryable"] is False


def test_gate_refusal_stamps_original_code(operator, monkeypatch, caplog):
    """授权闸拒把原始码写进 run.reason：上报被同一道闸反杀时归因仍在。"""
    f = operator
    entered, release = threading.Event(), threading.Event()
    attempts, observed = [], {}

    def dispatch(_, cfg, run_id):
        attempts.append(run_id)
        entered.set()
        assert release.wait(5)
        head = {"Authorization": "Bearer " + NODE_A_TOKEN}
        # 换版后的节点回调必被闸拒——真实节点拿不到授权只能带着它死。
        denied = f["client"].post("/v1/internal/charging-status/execution-config/" + run_id, headers=head)
        observed["status"] = denied.status_code
        observed["code"] = denied.json()["error"]["code"]
        # 失败上报只过协议闸：授权已死也照收，且不得改写已落的码（首写胜出）。
        observed["report"] = (
            f["client"]
            .post(
                "/v1/internal/charging-status/completions/" + run_id,
                headers=head,
                json={"status": "failed", "reason": "model_failed"},
            )
            .status_code
        )
        raise AgentRunDispatchError("node session died on gate refusal")

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    with caplog.at_level(logging.WARNING, logger="aiops.operator"):
        posted = request(f["client"])
        assert posted.status_code == 202, posted.text
        assert entered.wait(5)
        f["candidate"]["enabled"] = []
        f["publish"](f["route"].route_revision)
        release.set()
        final = terminal(f, posted.json()["qa_id"])
    assert observed == {
        "status": 403,
        "code": "OPERATOR_PUBLICATION_CHANGED",
        "report": 200,
    }
    run = f["ctx"].agent_run_store.by_qa(posted.json()["qa_id"])
    assert run.state == "failed"
    assert run.reason == "tool_refused:OPERATOR_PUBLICATION_CHANGED"
    assert final.json()["status"] == "failed"
    assert final.json()["error"]["code"] == "OPERATOR_DEPENDENCY_UNAVAILABLE"
    # 带码终局的 run 不再按模型噪声空耗 resample。
    assert len(attempts) == 1
    assert "operator_gate_refused" in caplog.text
    assert "OPERATOR_PUBLICATION_CHANGED" in caplog.text


def test_reports_land_after_authorization_death(operator, monkeypatch):
    """授权已死：执行面仍硬拒，但失败/证据/成功上报全部照录进终局快照，不投递。"""
    f = operator
    entered, release = threading.Event(), threading.Event()
    observed = {}

    def dispatch(_, cfg, run_id):
        head = {"Authorization": "Bearer " + NODE_A_TOKEN}
        # 授权还活着：节点照常领配置，manifest 身份凭证在手。
        publication = f["client"].post(
            "/v1/internal/charging-status/execution-config/" + run_id, headers=head
        )
        assert publication.status_code == 200, publication.text
        head["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
        entered.set()
        assert release.wait(5)
        # 授权已死：执行能力面（再领配置）照样 403。
        observed["publication"] = (
            f["client"]
            .post("/v1/internal/charging-status/execution-config/" + run_id, headers=head)
            .status_code
        )
        # 上报面不再被授权闸反杀：证据、结局都收。
        record = f["client"].post(
            f"/v1/internal/operator-runs/{run_id}/tools/cpms_order_detail/evidence",
            headers=head,
            json={
                "authorization_id": "late",
                "evidence": {"tool": "cpms_order_detail", "data": {"siteId": "SITE-1"}},
            },
        )
        observed["record"] = (record.status_code, record.json().get("delivered"))
        complete = f["client"].post(
            "/v1/internal/operator-runs/" + run_id + "/completions",
            headers=head,
            json={"status": "completed", "answer": "站点电压正常"},
        )
        observed["complete"] = (complete.status_code, complete.json().get("delivered"))
        raise AgentRunDispatchError("node session done")

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    posted = request(f["client"])
    assert posted.status_code == 202, posted.text
    assert entered.wait(5)
    f["candidate"]["enabled"] = []
    f["publish"](f["route"].route_revision)
    release.set()
    final = terminal(f, posted.json()["qa_id"])
    assert observed == {
        "publication": 403,
        "record": (200, False),
        "complete": (200, False),
    }
    run = f["ctx"].agent_run_store.by_qa(posted.json()["qa_id"])
    assert run.state == "failed"
    assert run.reason == "tool_refused:OPERATOR_PUBLICATION_CHANGED"
    assert run.snapshot["superseded"] is True
    assert run.snapshot["answer"] == "站点电压正常"
    assert run.snapshot["evidence"]["cpms_order_detail"]["data"]["siteId"] == "SITE-1"
    assert final.json()["status"] == "failed"
    assert final.json()["error"]["code"] == "OPERATOR_DEPENDENCY_UNAVAILABLE"
    # 不投递：用户侧回读仍是终局拒绝。
    with pytest.raises(operator_cpms.OperatorRefused, match="OPERATOR_RESULT_UNAVAILABLE"):
        f["ctx"].operator_cpms.authorize_readback(
            f["resolver"].current, posted.json()["qa_id"], "operator-private"
        )


def test_tool_disabled_refusal_still_self_reports(operator, monkeypatch):
    """OPERATOR_TOOL_DISABLED 不 stamp：上报不带 tool，节点自归因路径原样工作。"""
    f = operator

    def dispatch(_, cfg, run_id):
        head = {"Authorization": "Bearer " + NODE_A_TOKEN}
        publication = f["client"].post(
            "/v1/internal/charging-status/execution-config/" + run_id, headers=head
        )
        head["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
        denied = f["client"].post(
            f"/v1/internal/operator-runs/{run_id}/tools/shell/authorization",
            headers=head,
            json={},
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "OPERATOR_TOOL_DISABLED"
        # 闸拒没有终结运行：节点仍能上报自己的终局。
        assert f["ctx"].agent_run_store.get(run_id).state in {"issued", "running"}
        reported = f["client"].post(
            "/v1/internal/operator-runs/" + run_id + "/completions",
            headers=head,
            json={"status": "failed", "reason": "tool_refused:OPERATOR_TOOL_DISABLED"},
        )
        assert reported.status_code == 200, reported.text

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    posted = request(f["client"])
    assert posted.status_code == 202, posted.text
    final = terminal(f, posted.json()["qa_id"])
    assert final.json()["status"] == "failed"
    assert final.json()["error"]["code"] == "OPERATOR_DEPENDENCY_UNAVAILABLE"
    run = f["ctx"].agent_run_store.by_qa(posted.json()["qa_id"])
    assert run.reason == "operator_node_failed: tool_refused:OPERATOR_TOOL_DISABLED"


def test_feedback_refusal_does_not_end_the_run(operator, monkeypatch):
    """回喂类业务码不 stamp：run 保持存活，节点还能上报终局。"""
    f = operator

    def dispatch(_, cfg, run_id):
        head = {"Authorization": "Bearer " + NODE_A_TOKEN}
        publication = f["client"].post(
            "/v1/internal/charging-status/execution-config/" + run_id, headers=head
        )
        head["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
        tool = operator_cpms.TOOL_NAMES[0]
        denied = f["client"].post(
            f"/v1/internal/operator-runs/{run_id}/tools/{tool}/authorization",
            headers={**head, **CONTRACT},
            json={"arguments": {"order_no": "OTHER"}},
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] in operator_cpms.FEEDBACK_REFUSALS
        assert f["ctx"].agent_run_store.get(run_id).state in {"issued", "running"}
        reported = f["client"].post(
            "/v1/internal/operator-runs/" + run_id + "/completions",
            headers=head,
            json={"status": "failed", "reason": "model_failed"},
        )
        assert reported.status_code == 200, reported.text

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    posted = request(f["client"])
    assert posted.status_code == 202, posted.text
    final = terminal(f, posted.json()["qa_id"])
    assert final.json()["status"] == "failed"
    run = f["ctx"].agent_run_store.by_qa(posted.json()["qa_id"])
    assert run.reason == "operator_node_failed: model_failed"


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
