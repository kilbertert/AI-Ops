"""非订单提问必须经过现役节点，但不能取得订单工具。"""

import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from operations.test_operator_cpms import APP, HEADERS, NODE_A_TOKEN, environment, terminal

from aiops_diagnostics import operator_conversation as conversation
from aiops_diagnostics.identity.scope_context import DataScope
from aiops_diagnostics.operator import operator_cpms


@pytest.fixture
def operator(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        yield f


def test_no_order_question_reaches_node_without_business_reads(operator, monkeypatch):
    f = operator

    def dispatch(_, config, run_id):
        headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
        publication = f["client"].post(
            "/v1/internal/charging-status/execution-config/" + run_id, headers=headers
        )
        assert publication.status_code == 200, publication.text
        manifest = json.loads(publication.json()["manifest"])
        assert manifest["tools"] == []
        headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
        response = f["client"].post(
            f"/v1/internal/operator-runs/{run_id}/completions",
            headers=headers,
            json={
                "status": "completed",
                "answer": json.dumps(
                    {"kind": "answer", "text": "状态3表示异常已处理。", "name": "", "address_terms": []}
                ),
            },
        )
        assert response.status_code == 200, response.text

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    result = f["client"].post(
        "/v1/assistant/questions", headers=HEADERS, json={"question": "订单状态3是什么意思"}
    )
    assert result.status_code == 202, result.text
    final = terminal(f, result.json()["qa_id"])
    assert final.json()["status"] == "completed", final.text
    assert "异常已处理" in final.text
    assert not f["state"]["calls"]


def selection():
    return {"kind": "site_search", "text": "", "name": "", "address_terms": ["广州", "天河"]}


def sites():
    return [
        {
            "id": "SITE-1",
            "tenantId": "TENANT-A",
            "name": "天河测试站",
            "address": "广州市天河区测试路",
            "contactPhone": "PRIVATE-PHONE",
        },
    ]


def test_site_results_are_real_scoped_and_minimized(operator, monkeypatch):
    f = operator
    calls = []

    def cpms(url, bearer, tenant, *, timeout):
        assert timeout == 3
        calls.append((url, bearer, tenant))
        return {"code": 0, "data": {"records": sites(), "current": 1, "size": 50, "total": 1}}

    monkeypatch.setattr(operator_cpms, "cpms_get", cpms)
    caller = f["resolver"].current
    value = selection()
    conversation.validate(value, "帮我找一下广州天河附近的充电站")
    result = operator_cpms.search_sites(value, caller, "https://cpms.test", "private", lambda: caller)
    assert result["sites"] == [{"id": "SITE-1", "name": "天河测试站", "address": "广州市天河区测试路"}]
    assert parse_qs(urlsplit(calls[0][0]).query) == {
        "current": ["1"],
        "size": ["50"],
        "type": ["0"],
        "address": ["广州"],
    }
    assert calls[0][1:] == ("private", "TENANT-A")
    text = conversation.render(value, result, "zh")["blocks"][0]["text"]
    assert "天河测试站" in text and "未按距离排序" in text and "PRIVATE" not in text


@pytest.mark.parametrize(
    "fault",
    ["tenant", "site", "missing-id", "missing-tenant", "wrong-page", "oversized", "invalid-body", "revoked"],
)
def test_site_search_rejects_bad_evidence(operator, monkeypatch, fault):
    caller = operator["resolver"].current
    rows = sites()
    data = {"records": rows, "current": 1, "size": 50, "total": 1}
    if fault == "tenant":
        rows[0]["tenantId"] = "OTHER"
    if fault == "site":
        rows[0]["id"] = "SITE-OTHER"
    if fault.startswith("missing-"):
        rows[0].pop("id" if fault == "missing-id" else "tenantId")
    if fault == "wrong-page":
        data["current"] = 2
    if fault == "oversized":
        data["records"] = rows * 51
    calls = []

    def cpms(*_, **kwargs):
        calls.append(True)
        return [] if fault == "invalid-body" else {"code": 0, "data": data}

    monkeypatch.setattr(operator_cpms, "cpms_get", cpms)

    def auth():
        return (
            replace(caller, data_scope=DataScope(type="organ", site_ids=("OTHER",)), scope_fingerprint="")
            if fault == "revoked" and calls
            else caller
        )

    with pytest.raises(operator_cpms.OperatorRefused):
        operator_cpms.search_sites(selection(), caller, "https://cpms.test", "private", auth)


def test_empty_scope_never_calls_cpms(operator, monkeypatch):
    caller = replace(operator["resolver"].current, data_scope=DataScope(type="organ"), scope_fingerprint="")
    monkeypatch.setattr(operator_cpms, "cpms_get", lambda *_: pytest.fail("empty scope read"))
    result = operator_cpms.search_sites(selection(), caller, "https://cpms.test", "private", lambda: caller)
    assert result["empty_scope"] and not result["sites"]
    assert "尚未分配" in conversation.render(selection(), result, "zh")["blocks"][0]["text"]


def test_page_budget_is_bounded_and_not_claimed_complete(operator, monkeypatch):
    caller = operator["resolver"].current
    calls = []

    def cpms(url, *_, **kwargs):
        page = int(parse_qs(urlsplit(url).query)["current"][0])
        calls.append(page)
        return {"code": 0, "data": {"records": sites(), "current": page, "size": 50, "total": 10000}}

    monkeypatch.setattr(operator_cpms, "cpms_get", cpms)
    result = operator_cpms.search_sites(selection(), caller, "https://cpms.test", "private", lambda: caller)
    assert calls == [1, 2, 3, 4] and result["limited"]
    assert len(result["sites"]) == 1


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "site_search", "text": "编造站点", "name": "", "address_terms": ["广州"]},
        {"kind": "site_search", "text": "", "name": "", "address_terms": ["不存在的地点"]},
        {"kind": "site_search", "text": "", "name": "", "address_terms": ["%"]},
        {"kind": [], "text": "", "name": "", "address_terms": []},
    ],
)
def test_model_search_cannot_invent_conditions_or_results(value):
    with pytest.raises(operator_cpms.OperatorRefused):
        conversation.validate(value, "找广州天河充电站%")


def complete(f, run_id, answer):
    headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
    publication = f["client"].post("/v1/internal/charging-status/execution-config/" + run_id, headers=headers)
    assert publication.status_code == 200, publication.text
    headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
    denied = f["client"].post(
        f"/v1/internal/operator-runs/{run_id}/tools/cpms_order_detail/authorization", headers=headers, json={}
    )
    assert denied.status_code == 403
    response = f["client"].post(
        f"/v1/internal/operator-runs/{run_id}/completions",
        headers=headers,
        json={"status": "completed", "answer": json.dumps(answer)},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("entry", ["formal", "trial", "native", "managed"])
def test_site_search_uses_same_scope_on_all_entries(operator, monkeypatch, entry):
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    f = operator
    monkeypatch.setattr(
        operator_cpms.SnailDispatcher, "dispatch", lambda _, config, run: complete(f, run, selection())
    )
    f["settings"].dify_trial_api_key = "trial-only"
    f["settings"].environment = "env-a"
    monkeypatch.setattr(
        "aiops_diagnostics.gateway_api.authenticated_operator_console",
        lambda *_: (f["resolver"].current, "operator-private"),
    )
    with DifyWorkspaces(f["db"])._connection(write=True) as db:
        db.execute(
            "INSERT INTO dify_workspaces "
            "(tenant_id,environment,workspace_id,setup_state,created_at,updated_at) "
            "VALUES (?,?,?,'ready',?,?)",
            ("TENANT-A", "env-a", "workspace", "2026-10-29", "2026-10-29"),
        )
    body = {"question": "帮我找一下广州天河附近的充电站"}
    if entry == "managed":
        from aiops_diagnostics.execution import chat_execution

        original = chat_execution.binding_for_key

        def empty_trial_binding(settings, key):
            binding = original(settings, key)
            return replace(binding, manifest_text="") if binding else None

        monkeypatch.setattr(chat_execution, "binding_for_key", empty_trial_binding)
        monkeypatch.setattr(
            chat_execution, "select_binding", lambda *args, **kwargs: replace(f["route"], manifest_text="")
        )
    if entry == "formal":
        response = f["client"].post("/v1/assistant/questions", headers=HEADERS, json=body)
        assert response.status_code == 202, response.text
        final = terminal(f, response.json()["qa_id"])
    else:
        body["app_id"] = APP
        if entry == "native":
            body["operator_preview"] = {"pre_prompt": "无订单业务咨询", "tools": []}
        headers = {"Authorization": "Bearer trial-only", "X-AIOps-Console-Session": "session"}
        response = f["client"].post("/v1/dify/trial", headers=headers, json=body)
        assert response.status_code == 202, response.text
        terminal(f, response.json()["trial_id"])
        final = f["client"].get("/v1/dify/trial/" + response.json()["trial_id"], headers=headers)
    assert final.status_code == 200 and final.json()["status"] == "completed", final.text
    assert "天河测试站" in final.text and "PRIVATE-PHONE" not in final.text
    assert len(f["state"]["calls"]) == 1 and f["state"]["calls"][0][0].startswith("/chSite/page?")


def test_cancel_during_site_read_cannot_deliver(operator, monkeypatch):
    f = operator

    def dispatch(_, config, run):
        qa = f["ctx"].agent_run_store.get(run).qa_id
        f["state"]["hook"] = lambda: f["client"].post(
            "/v1/assistant/questions/" + qa + "/cancel", headers=HEADERS
        )
        complete(f, run, selection())

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    response = f["client"].post(
        "/v1/assistant/questions", headers=HEADERS, json={"question": "找广州天河充电站"}
    )
    final = terminal(f, response.json()["qa_id"])
    assert final.json()["status"] != "completed" and not final.json().get("result")


def test_empty_scope_can_ask_general_questions(operator, monkeypatch):
    f = operator
    f["resolver"].current = replace(
        f["resolver"].current, data_scope=DataScope(type="organ"), scope_fingerprint=""
    )
    monkeypatch.setattr(
        operator_cpms.SnailDispatcher,
        "dispatch",
        lambda _, config, run: complete(
            f, run, {"kind": "answer", "text": "可以咨询充电问题。", "name": "", "address_terms": []}
        ),
    )
    response = f["client"].post("/v1/assistant/questions", headers=HEADERS, json={"question": "你能做什么"})
    assert response.status_code == 202, response.text
    assert terminal(f, response.json()["qa_id"]).json()["status"] == "completed"
    assert not f["state"]["calls"]


def test_site_name_with_date_is_not_an_order(operator, monkeypatch):
    f = operator
    monkeypatch.setattr(
        operator_cpms.SnailDispatcher,
        "dispatch",
        lambda _, config, run: complete(
            f, run, {"kind": "site_search", "text": "", "name": "TEST2026-10-29", "address_terms": []}
        ),
    )
    response = f["client"].post(
        "/v1/assistant/questions",
        headers=HEADERS,
        json={"question": "帮我查询充电站 TEST2026-10-29 03:00:09"},
    )
    assert response.status_code == 202, response.text
    assert terminal(f, response.json()["qa_id"]).json()["status"] == "completed"
    assert f["state"]["calls"] and all(p.startswith("/chSite/page?") for p, _ in f["state"]["calls"])
