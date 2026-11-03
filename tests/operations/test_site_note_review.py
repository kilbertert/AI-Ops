"""同一工具取证链上的说明降级、冻结与撤权。"""

import json
import threading
from dataclasses import replace

import pytest

from aiops_diagnostics.common.config import AgentSettings, ProviderConfig
from aiops_diagnostics.identity.scope_context import DataScope
from aiops_diagnostics.operator import operator_conversation, operator_cpms, site_note_review
from operations.test_operator_conversation import (
    complete,
    drive_site_tool,
    selection,
)
from operations.test_operator_conversation import (
    operator as operator,
)
from operations.test_operator_cpms import HEADERS, NODE_A_TOKEN, terminal


@pytest.mark.parametrize("verdict", ["allow", "reject", "uncertain", "unavailable"])
@pytest.mark.parametrize("trial", [False, True])
def test_note_decision_preserves_evidence_and_freezes_readback(operator, monkeypatch, verdict, trial):
    f = operator
    f["state"]["site_address"] = "广州天河测试路"
    calls = []
    completed = []
    note = "查询结果在下方，请核对地点。"

    def review(answer, sites, language, settings):
        calls.append(answer)
        assert sites["sites"][0]["name"] == "天河测试站"
        return {"verdict": verdict, "version": "test-review", "model": "test-model"}

    def dispatch(_, config, run):
        drive_site_tool(f, run, {"address": "广州 天河"})
        f["ctx"].operator_cpms.tasks[run].trial = trial
        completed.append(complete(f, run, {**selection(), "text": note}))

    monkeypatch.setattr(site_note_review, "review", review)
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    response = f["client"].post(
        "/v1/assistant/questions", headers=HEADERS, json={"question": "找广州天河充电站"}
    )
    qa = response.json()["qa_id"]
    final = terminal(f, qa)
    assert final.json()["status"] == "completed", (final.text, completed)
    assert "天河测试站" in final.text
    assert (note in final.text) == (verdict == "allow")
    assert ("说明未通过审核" in final.text) == (trial and verdict != "allow")
    run = f["ctx"].agent_run_store.by_qa(qa)
    assert run.snapshot["site_note_review"]["verdict"] == verdict
    assert run.snapshot["answer"]["text"] == (note if verdict == "allow" else "")
    monkeypatch.setattr(site_note_review, "review", lambda *_: pytest.fail("回读不得重审"))
    monkeypatch.setattr(operator_conversation, "render", lambda *_: pytest.fail("回读不得重渲染"))
    again = f["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS)
    assert again.json()["result"] == final.json()["result"]
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["cancel", "scope", "expiry"])
def test_late_review_cannot_revive_cancelled_or_unauthorized_run(operator, monkeypatch, change):
    from datetime import UTC, datetime, timedelta

    f = operator
    f["state"]["site_address"] = "广州天河测试路"
    current = []
    applied = []
    completions = []
    finished = threading.Event()

    def review(*_):
        run_id = current[0]
        task = f["ctx"].operator_cpms.tasks[run_id]
        acquired = []

        def check_lock():
            if task.lock.acquire(timeout=0.2):
                acquired.append(True)
                task.lock.release()

        worker = threading.Thread(target=check_lock)
        worker.start()
        worker.join(timeout=1)
        assert acquired == [True], "网络审核不得占用证据锁"
        run = f["ctx"].agent_run_store.get(run_id)
        if change == "cancel":
            f["client"].post("/v1/assistant/questions/" + run.qa_id + "/cancel", headers=HEADERS)
        elif change == "scope":
            f["resolver"].current = replace(
                f["resolver"].current, data_scope=DataScope(type="organ", site_ids=()), scope_fingerprint=""
            )
        else:
            with f["ctx"].agent_run_store._connection(write=True) as db:
                db.execute(
                    "UPDATE agent_runs SET expires_at=? WHERE run_id=?",
                    ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), run_id),
                )
        applied.append(change)
        return {"verdict": "allow", "version": "test", "model": "test"}

    def dispatch(_, config, run):
        try:
            current.append(run)
            drive_site_tool(f, run, {"address": "广州 天河"})
            record = f["ctx"].agent_run_store.get(run)
            completions.append(
                f["client"].post(
                    f"/v1/internal/operator-runs/{run}/completions",
                    headers={
                        "Authorization": "Bearer " + NODE_A_TOKEN,
                        "X-AI-Ops-Exec-Config": record.expected_agent_version,
                    },
                    json={"status": "completed", "answer": json.dumps({**selection(), "text": "结果如下。"})},
                )
            )
        finally:
            finished.set()

    monkeypatch.setattr(site_note_review, "review", review)
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    response = f["client"].post(
        "/v1/assistant/questions", headers=HEADERS, json={"question": "找广州天河充电站"}
    )
    final = terminal(f, response.json()["qa_id"])
    assert final.json().get("status") != "completed" and not final.json().get("result"), final.text
    assert finished.wait(5)
    assert applied == [change]
    assert len(completions) == 1 and completions[0].status_code == 403, completions
    assert f["ctx"].agent_run_store.get(current[0]).state != "succeeded"


@pytest.mark.parametrize("response", [{"verdict": "allow"}, {"verdict": "reject"}, {}, {"verdict": []}])
def test_review_uses_only_rendered_facts_and_validates_result(monkeypatch, response):
    from aiops_diagnostics.common import model_request

    seen = []

    def request(spec, **_):
        seen.append(json.loads(spec.body))
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(response)}}]}

    monkeypatch.setattr(model_request, "request_json", request)
    monkeypatch.setenv("REVIEW_TEST_KEY", "private-test-key")
    settings = AgentSettings(
        providers=(
            ProviderConfig(
                "test",
                "https://model.example/v1",
                wire_api="chat",
                api_key_env="REVIEW_TEST_KEY",
                model="test",
            ),
        ),
        default_provider="test",
    )
    sites = {
        "sites": [],
        "empty_scope": False,
        "limited": False,
        "total": 0,
        "source": "cpms.sitePage",
        "observed_at": "synthetic",
        "scope_limited": False,
    }
    result = site_note_review.review({**selection(), "text": "没有匹配结果。"}, sites, "zh", settings)
    assert (
        result["verdict"] == response.get("verdict", "unavailable")
        if response in ({"verdict": "allow"}, {"verdict": "reject"})
        else result["verdict"] == "unavailable"
    )
    assert len(seen) == 1 and "tools" not in seen[0] and seen[0]["max_tokens"] == 2048
    assert "private-test-key" not in json.dumps(seen)
    assert "未找到匹配站点" in seen[0]["messages"][1]["content"]


def test_review_transport_failure_hides_note(monkeypatch):
    monkeypatch.setattr(site_note_review, "resolve_provider_api_key", lambda *a, **k: "test")
    monkeypatch.setattr(
        site_note_review, "structured_request", lambda **k: (_ for _ in ()).throw(TimeoutError())
    )
    sites = {"sites": [], "empty_scope": True, "limited": False, "total": 0, "scope_limited": True}
    assert (
        site_note_review.review({**selection(), "text": "结果如下。"}, sites, "zh", AgentSettings())[
            "verdict"
        ]
        == "unavailable"
    )


@pytest.mark.parametrize("fault", ["missing-evidence", "different-query", "credential", "wrong-kind"])
def test_review_cannot_turn_boundary_failure_into_a_list(operator, monkeypatch, fault):
    f = operator
    f["state"]["site_address"] = "广州天河测试路"
    seen = []

    def review(*_):
        seen.append(True)
        return {"verdict": "allow", "version": "test", "model": "test"}

    def dispatch(_, config, run):
        if fault != "missing-evidence":
            drive_site_tool(f, run, {"address": "广州 天河"})
        value = {**selection(), "text": "结果如下。"}
        if fault == "different-query":
            value["address_terms"] = []
        if fault == "wrong-kind":
            value.update(kind="answer", name="", address_terms=[])
        if fault == "credential":
            value["text"] = f["ctx"].operator_cpms.tasks[run].bearer
        complete(f, run, value)

    monkeypatch.setattr(site_note_review, "review", review)
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    response = f["client"].post(
        "/v1/assistant/questions", headers=HEADERS, json={"question": "找广州天河充电站"}
    )
    final = terminal(f, response.json()["qa_id"])
    assert final.json()["status"] == "failed" and not final.json().get("result"), final.text
    assert not seen


def test_dify_trial_shows_hidden_note_notice(operator, monkeypatch):
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
    from operations.test_operator_cpms import APP

    f = operator
    f["state"]["site_address"] = "广州天河测试路"
    f["settings"].dify_trial_api_key = "trial-only"
    f["settings"].environment = "env-a"
    monkeypatch.setattr(
        "aiops_diagnostics.gateway.gateway_api.authenticated_operator_console",
        lambda *_: (f["resolver"].current, "operator-private"),
    )
    with DifyWorkspaces(f["db"])._connection(write=True) as db:
        db.execute(
            "INSERT INTO dify_workspaces "
            "(tenant_id,environment,workspace_id,setup_state,created_at,updated_at) "
            "VALUES (?,?,?,'ready',?,?)",
            ("TENANT-A", "env-a", "workspace", "2026-11-03", "2026-11-03"),
        )

    def dispatch(_, config, run):
        drive_site_tool(f, run, {"address": "广州 天河"})
        complete(f, run, {**selection(), "text": "全都免费。"})

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    monkeypatch.setattr(
        site_note_review, "review", lambda *_: {"verdict": "reject", "version": "test", "model": "test"}
    )
    headers = {"Authorization": "Bearer trial-only", "X-AIOps-Console-Session": "session"}
    response = f["client"].post(
        "/v1/dify/trial", headers=headers, json={"app_id": APP, "question": "找广州天河充电站"}
    )
    assert response.status_code == 202, response.text
    trial = response.json()["trial_id"]
    terminal(f, trial)
    final = f["client"].get("/v1/dify/trial/" + trial, headers=headers)
    assert final.json()["status"] == "completed", final.text
    text = final.json()["result"]["report"]["summary"]
    assert "天河测试站" in text and "全都免费" not in text and "说明未通过审核" in text


@pytest.mark.parametrize(
    "note,verdict", [("", "empty"), ("x" * 401, "reject"), ("{{fact:secret}}", "reject")]
)
def test_empty_or_invalid_note_never_calls_model(monkeypatch, note, verdict):
    monkeypatch.setattr(site_note_review, "structured_request", lambda **_: pytest.fail("不应调用模型"))
    assert (
        site_note_review.review({**selection(), "text": note}, {}, "zh", AgentSettings())["verdict"]
        == verdict
    )
