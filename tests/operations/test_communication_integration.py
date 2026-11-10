"""通讯候选接线：真实本地Gateway与HTTP，合成身份和节点。"""

import pytest

from aiops_diagnostics.operator import communication, operator_cpms
from operations.test_operator_cpms import (
    _authorized,
    _refusal,
    environment,
    node,
    request,
    terminal,
)


@pytest.fixture
def operator(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        f["ctx"].runtime.diagnostic_settings.mysql.timezone = "Asia/Shanghai"
        yield f


def grant(f, run_id, head, **extra):
    return f["client"].post(
        f"/v1/internal/operator-runs/{run_id}/tools/{communication.NAME}/authorization",
        headers={**head, "X-AI-Ops-Tool-Contract": operator_cpms.OBJECT_CONTRACT},
        json={"arguments": {"order_no": f["state"]["order"], **extra}},
    )


def test_empty_sources_are_delivered_as_independent_results(operator, monkeypatch):
    f = operator
    monkeypatch.setattr(
        operator_cpms.SnailDispatcher,
        "dispatch",
        lambda _, cfg, run_id: node(f, run_id, tools=("cpms_order_detail", communication.NAME)),
    )
    response = request(f["client"])
    final = terminal(f, response.json()["qa_id"])
    assert final.json()["status"] == "completed", final.text
    snapshot = f["ctx"].agent_run_store.by_qa(response.json()["qa_id"]).snapshot
    data = snapshot["evidence"][communication.NAME]["data"]
    assert all(s["status"] == "empty" and s["total"] == 0 for s in data["sources"])
    calls = sorted(
        f["state"]["tsdata_calls"], key=lambda p: [s[0] for s in communication.SOURCES].index(p["table"])
    )
    assert [p["deviceId"] for p in calls] == [
        "PILE-1",
        "PILE-1",
        "PILE-1-1",
        "PILE-1",
        "PILE-1-1",
    ]
    assert data["association"] == "device_window_candidate"


def test_http_500_is_not_delivered_as_zero_records(operator, monkeypatch):
    f = operator
    f["state"]["tsdata_error"] = True
    monkeypatch.setattr(
        operator_cpms.SnailDispatcher,
        "dispatch",
        lambda _, cfg, run_id: node(f, run_id, tools=("cpms_order_detail", communication.NAME)),
    )
    response = request(f["client"])
    final = terminal(f, response.json()["qa_id"])
    assert final.json()["status"] == "completed", final.text
    data = (
        f["ctx"]
        .agent_run_store.by_qa(response.json()["qa_id"])
        .snapshot["evidence"][communication.NAME]["data"]
    )
    assert all(s["status"] == "unavailable" and s["total"] is None for s in data["sources"])


def test_undelivered_detail_and_model_device_are_refused(operator, monkeypatch):
    f = operator
    seen = []

    def dispatch(_, cfg, run_id):
        head, _ = _authorized(f, run_id)
        response = grant(f, run_id, head, device="OTHER")
        seen.append(_refusal(response))
        response = grant(f, run_id, head)
        assert response.status_code == 200, response.text
        g = response.json()
        response = f["client"].get(
            g["base_url"] + g["request"]["path"], headers={"Authorization": "Bearer " + g["bearer"]}
        )
        seen.append(_refusal(response))
        assert not f["state"]["tsdata_calls"]
        return node(f, run_id)

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    response = request(f["client"])
    final = terminal(f, response.json()["qa_id"])
    assert final.json()["status"] == "completed", final.text
    assert seen == ["OPERATOR_TOOL_ARGUMENTS_INVALID", "OPERATOR_DETAIL_REQUIRED"]


def test_cached_communications_recheck_order_scope(operator, monkeypatch):
    f = operator
    seen = []

    def changed():
        run_id = next(iter(f["ctx"].operator_cpms.tasks))
        f["state"]["site"] = "OTHER-SITE"
        task = f["ctx"].operator_cpms.tasks[run_id]
        target = operator_cpms.tool_request(communication.NAME, {"order_no": f["state"]["order"]})
        response = f["client"].get(
            "/v1/internal/operator-runs/" + run_id + "/cpms" + target["path"],
            headers={"Authorization": "Bearer " + task.grant},
        )
        seen.append(_refusal(response))

    monkeypatch.setattr(
        operator_cpms.SnailDispatcher,
        "dispatch",
        lambda _, cfg, run_id: node(
            f, run_id, tools=("cpms_order_detail", communication.NAME), before_complete=changed
        ),
    )
    response = request(f["client"])
    terminal(f, response.json()["qa_id"])
    assert seen and seen[0] in {"CPMS_SCOPE_MISMATCH", "OPERATOR_SCOPE_CHANGED"}
