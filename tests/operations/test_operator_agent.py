"""统一模型循环：模型输入来自会话，业务读取仍穿过真实Gateway授权。"""

import json

import pytest
from execution.test_m2_close_contract import NODE_A_TOKEN

from aiops_diagnostics.operator import operator_agent, operator_cpms
from operations.test_operator_cpms import HEADERS, ORDER, TIMELINE_ROW, environment, terminal


def call_tool(f, run_id, tool, arguments=None):
    client = f["client"]
    headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
    publication = client.post("/v1/internal/charging-status/execution-config/" + run_id, headers=headers)
    assert publication.status_code == 200, publication.text
    headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
    kind = operator_cpms._declared(tool).kind
    headers["X-AI-Ops-Tool-Contract"] = "request-target/2" if kind == "object" else "request-target/1"
    path = f"/v1/internal/operator-runs/{run_id}/tools/{tool}"
    response = client.post(path + "/authorization", headers=headers, json={"arguments": arguments or {}})
    assert response.status_code == 200, response.text
    grant = response.json()
    request = grant["request"]
    response = client.get(
        grant["base_url"] + request["path"] + ("?" + request["query"] if request["query"] else ""),
        headers={"Authorization": "Bearer " + grant["bearer"]},
    )
    assert response.status_code == 200, response.text
    evidence = f["ctx"].operator_cpms.tasks[run_id].evidence[tool]
    observed = {k: evidence[k] for k in ["tool", "source", "data", "observed_at"]}
    observed["inputs"] = grant["inputs"]
    response = client.post(
        path + "/evidence",
        headers=headers,
        json={"authorization_id": grant["authorization_id"], "evidence": observed},
    )
    assert response.status_code == 200, response.text
    return observed, headers, publication.json()


def finish(f, run, headers, reply, tools):
    r = f["client"].post(
        f"/v1/internal/operator-runs/{run}/completions",
        headers=headers,
        json={"status": "completed", "answer": reply},
    )
    assert r.status_code == 200, r.text


def test_model_can_select_order_and_receive_calculation_before_answer(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:

        def dispatch(self, config, run):
            assert f["ctx"].agent_run_store.get(run).order_no == ""
            for name in ["cpms_order_detail", "cpms_order_tariff", "cpms_order_billing"]:
                evidence, headers, publication = call_tool(f, run, name, {"order_no": ORDER})
            evidence, headers, _ = call_tool(f, run, "cpms_order_amount_check", {})
            assert evidence["data"]["checks"][2]["status"] == "待验证"
            assert publication["messages"][-1]["role"] == "user"
            finish(
                f,
                run,
                headers,
                "这项核验待验证，缺少独立记录。",
                ["cpms_order_amount_check"],
            )

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        r = f["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "查一下" + ORDER + "为什么这么贵"}
        )
        assert r.status_code == 202, r.text
        result = terminal(f, r.json()["qa_id"])
        assert result.json()["status"] == "completed", result.text
        assert "待验证" in result.json()["result"]["blocks"][0]["text"]
        assert f["ctx"].agent_run_store.by_qa(r.json()["qa_id"]).order_no == ORDER


def test_start_records_the_request_language_on_the_run(tmp_path, monkeypatch):
    """Devin #1486：start 收到 language 却从不传给 issue，日志里恒显示未记录。"""
    with environment(tmp_path, monkeypatch) as f:

        def dispatch(self, config, run):
            evidence, headers, _ = call_tool(f, run, "cpms_order_detail", {"order_no": ORDER})
            finish(f, run, headers, "正常。", ["cpms_order_detail"])

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        r = f["client"].post(
            "/v1/assistant/questions",
            headers={**HEADERS, "Accept-Language": "en"},
            json={"question": "查一下" + ORDER + "为什么这么贵"},
        )
        assert r.status_code == 202, r.text
        assert terminal(f, r.json()["qa_id"]).json()["status"] == "completed"
        assert f["ctx"].agent_run_store.by_qa(r.json()["qa_id"]).language == "en"


@pytest.mark.parametrize(
    "question", ["为什么这一项还是待验证？", "那优惠没算进去吗？", "这笔单在哪个站点充的？"]
)
def test_context_has_actual_reply_and_model_can_choose_current_order(tmp_path, monkeypatch, question):
    with environment(tmp_path, monkeypatch) as f:
        contexts = []

        def dispatch(self, config, run):
            evidence, headers, publication = call_tool(f, run, "cpms_order_detail")
            contexts.append(publication["messages"])
            finish(f, run, headers, "本次记录需要补充原始交易证据。", ["cpms_order_detail"])

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        first = (
            f["client"]
            .post(
                "/v1/assistant/questions", headers=HEADERS, json={"question": "查询费用", "order_no": ORDER}
            )
            .json()
        )
        assert terminal(f, first["qa_id"]).json()["status"] == "completed"
        second = (
            f["client"]
            .post(
                "/v1/assistant/questions",
                headers=HEADERS,
                json={"question": question, "conversation_id": first["conversation_id"]},
            )
            .json()
        )
        assert terminal(f, second["qa_id"]).json()["status"] == "completed"
        assert contexts[1][1]["role"] == "assistant"
        assert "需要补充原始交易证据" in contexts[1][1]["content"]
        assert f["ctx"].agent_run_store.by_qa(second["qa_id"]).order_no == ORDER


def test_candidate_is_not_authorization_or_an_invented_order():
    assert operator_agent.order_candidate({"order_no": "ABC_123"}, "订单ABC_123", "", "") == "ABC_123"
    with pytest.raises(operator_cpms.OperatorRefused):
        operator_agent.order_candidate({"order_no": "invented"}, "查订单", "", "")
    with pytest.raises(operator_cpms.OperatorRefused):
        operator_agent.order_candidate({"order_no": "B"}, "查B", "A", "")


def test_site_readback_rechecks_authorization_after_the_final_cpms_request(tmp_path, monkeypatch):
    from operations.test_operator_cpms import scope

    with environment(tmp_path, monkeypatch) as f:

        def dispatch(_, config, run):
            _, headers, _ = call_tool(f, run, "cpms_site_search", {"address_terms": []})
            finish(f, run, headers, "已读取可见站点，详细情况还需核对。", ["cpms_site_search"])

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        response = f["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "查天河站点"}
        )
        qa = response.json()["qa_id"]
        assert terminal(f, qa).json()["status"] == "completed"
        f["state"]["hook"] = lambda: setattr(f["resolver"], "current", scope("OTHER"))
        response = f["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS)
        assert response.status_code == 403 and "result" not in response.json()


def test_revoked_history_is_omitted_without_blocking_a_new_general_question(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        seen = []

        def dispatch(self, config, run):
            if not seen:
                _, head, publication = call_tool(f, run, "cpms_order_detail")
                finish(f, run, head, "历史秘密订单说明", ["cpms_order_detail"])
            else:
                headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
                publication = (
                    f["client"]
                    .post("/v1/internal/charging-status/execution-config/" + run, headers=headers)
                    .json()
                )
                assert "历史秘密" not in str(publication["messages"])
                headers["X-AI-Ops-Exec-Config"] = publication["sha256"]
                finish(f, run, headers, "可以解释一般充电方法。", [])
            seen.append(publication["messages"])

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        first = (
            f["client"]
            .post(
                "/v1/assistant/questions", headers=HEADERS, json={"question": "查询费用", "order_no": ORDER}
            )
            .json()
        )
        assert terminal(f, first["qa_id"]).json()["status"] == "completed"
        f["state"]["site"] = "OTHER"
        second = (
            f["client"]
            .post(
                "/v1/assistant/questions",
                headers=HEADERS,
                json={"question": "介绍充电操作方法", "conversation_id": first["conversation_id"]},
            )
            .json()
        )
        assert terminal(f, second["qa_id"]).json()["status"] == "completed"
        assert json.loads(seen[1][-1]["content"])["conversation_order"] == ""


def test_order_cannot_change_site_between_tool_authorizations(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        from operations.test_operator_cpms import tenant_owner

        f["resolver"].current = tenant_owner()

        def dispatch(self, config, run):
            _, headers, _ = call_tool(f, run, "cpms_order_detail")
            f["state"]["site"] = "OTHER"
            response = f["client"].post(
                f"/v1/internal/operator-runs/{run}/tools/cpms_charging_samples/authorization",
                headers=headers,
                json={},
            )
            assert response.status_code == 403

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        first = (
            f["client"]
            .post(
                "/v1/assistant/questions", headers=HEADERS, json={"question": "检查订单", "order_no": ORDER}
            )
            .json()
        )
        assert terminal(f, first["qa_id"]).json()["status"] == "failed"


def test_history_only_followup_cannot_hide_revoked_ancestor(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        seen = []

        def dispatch(_, config, run):
            if not seen:
                _, head, publication = call_tool(f, run, "cpms_order_detail")
                reply = "敏感订单原始结论"
                tools = ["cpms_order_detail"]
            else:
                head = {"Authorization": "Bearer " + NODE_A_TOKEN}
                publication = (
                    f["client"]
                    .post("/v1/internal/charging-status/execution-config/" + run, headers=head)
                    .json()
                )
                head["X-AI-Ops-Exec-Config"] = publication["sha256"]
                reply = "复述敏感订单原始结论" if len(seen) == 1 else "一般操作说明"
                tools = []
            seen.append(publication["messages"])
            finish(f, run, head, reply, tools)

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        first = (
            f["client"]
            .post("/v1/assistant/questions", headers=HEADERS, json={"question": "查订单", "order_no": ORDER})
            .json()
        )
        assert terminal(f, first["qa_id"]).json()["status"] == "completed"
        second = (
            f["client"]
            .post(
                "/v1/assistant/questions",
                headers=HEADERS,
                json={"question": "解释刚才结论", "conversation_id": first["conversation_id"]},
            )
            .json()
        )
        assert terminal(f, second["qa_id"]).json()["status"] == "completed"
        f["state"]["site"] = "OTHER"
        third = (
            f["client"]
            .post(
                "/v1/assistant/questions",
                headers=HEADERS,
                json={"question": "介绍一般操作", "conversation_id": first["conversation_id"]},
            )
            .json()
        )
        assert terminal(f, third["qa_id"]).json()["status"] == "completed"
        assert "敏感订单" not in str(seen[-1])


def test_absent_order_and_unknown_analysis_fail_closed():
    with pytest.raises(operator_cpms.OperatorRefused):
        operator_agent.order_candidate({}, "查询", "", "")
    with pytest.raises(operator_cpms.OperatorRefused):
        operator_agent.analysis({"cpms_order_detail": {"data": {}}}, "unknown")
    with pytest.raises(operator_cpms.OperatorRefused):
        operator_agent.render("", {})


def test_process_statistics_tool_requires_only_current_detail_and_samples(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        f["state"]["monitor_rows"] = [
            {
                "orderNo": ORDER,
                "tenantId": f["state"]["tenant"],
                "siteId": f["state"]["site"],
                "voltage": "220",
                "current": "10",
                "power": "2.2",
            },
            {
                "orderNo": ORDER,
                "tenantId": f["state"]["tenant"],
                "siteId": f["state"]["site"],
                "voltage": "221",
                "current": "9",
                "power": "2.1",
            },
        ]

        def dispatch(_, config, run):
            call_tool(f, run, "cpms_order_detail")
            call_tool(f, run, "cpms_charging_samples")
            evidence, head, _ = call_tool(f, run, "cpms_order_process_stats")
            assert evidence["data"]["sample_count"] == 2
            assert evidence["data"]["average_power_kw"] == "91.20"
            assert {m["name"] for m in evidence["data"]["metrics"]} == {
                "voltage",
                "current",
                "power",
            }
            finish(f, run, head, "平均功率91.20 kW。", [])

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        result = (
            f["client"]
            .post(
                "/v1/assistant/questions", headers=HEADERS, json={"question": "看看功率", "order_no": ORDER}
            )
            .json()
        )
        assert terminal(f, result["qa_id"]).json()["status"] == "completed"


def test_process_statistics_falls_back_to_authorized_timeline(tmp_path, monkeypatch):
    """监测表结构性空行（远程/普通启动单、汽车桩）：过程统计吃同一授权订单、
    同一设备与窗口的时序证据（SOP §2.2），字段口径按时序列名。"""
    with environment(tmp_path, monkeypatch) as f:
        f["state"]["tsdata_rows"] = [
            dict(TIMELINE_ROW, txSerialNo="17", outputVoltage="220", outputCurrent="10", power="2.2"),
            dict(
                TIMELINE_ROW,
                _ts="2026-11-05T06:30:41.000Z",
                txSerialNo="17",
                outputVoltage="221",
                outputCurrent="9",
                power="2.1",
            ),
        ]

        def dispatch(_, config, run):
            call_tool(f, run, "cpms_order_detail")
            call_tool(f, run, "cpms_charging_samples")
            call_tool(f, run, "cpms_charging_timeline", {"order_no": ORDER})
            evidence, head, _ = call_tool(f, run, "cpms_order_process_stats")
            assert evidence["data"]["sample_count"] == 2
            assert evidence["data"]["complete_timeline"] is True
            assert {m["name"] for m in evidence["data"]["metrics"]} == {
                "outputVoltage",
                "outputCurrent",
                "power",
            }
            voltage = next(m for m in evidence["data"]["metrics"] if m["name"] == "outputVoltage")
            assert str(voltage["minimum"]) == "220" and str(voltage["maximum"]) == "221"
            finish(f, run, head, "过程统计按枪时序给出。", [])

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        result = (
            f["client"]
            .post(
                "/v1/assistant/questions", headers=HEADERS, json={"question": "看看功率", "order_no": ORDER}
            )
            .json()
        )
        assert terminal(f, result["qa_id"]).json()["status"] == "completed"


def test_process_statistics_prefers_monitor_when_it_has_rows(tmp_path, monkeypatch):
    """监测表有行的订单不因同时取了时序而改口径：统计仍吃监测样本。"""
    with environment(tmp_path, monkeypatch) as f:
        f["state"]["monitor_rows"] = [
            {
                "orderNo": ORDER,
                "tenantId": f["state"]["tenant"],
                "siteId": f["state"]["site"],
                "voltage": "220",
            }
        ]
        f["state"]["tsdata_rows"] = [dict(TIMELINE_ROW, outputVoltage="380")]

        def dispatch(_, config, run):
            call_tool(f, run, "cpms_order_detail")
            call_tool(f, run, "cpms_charging_samples")
            call_tool(f, run, "cpms_charging_timeline", {"order_no": ORDER})
            evidence, head, _ = call_tool(f, run, "cpms_order_process_stats")
            assert evidence["data"]["sample_count"] == 1
            assert {m["name"] for m in evidence["data"]["metrics"]} == {
                "voltage",
                "current",
                "power",
            }
            finish(f, run, head, "监测样本统计。", [])

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        result = (
            f["client"]
            .post(
                "/v1/assistant/questions", headers=HEADERS, json={"question": "看看功率", "order_no": ORDER}
            )
            .json()
        )
        assert terminal(f, result["qa_id"]).json()["status"] == "completed"


def test_process_statistics_does_not_merge_mixed_transaction_serials(tmp_path, monkeypatch):
    """时序窗口混入相邻交易流水：不跨流水合并（SOP 分段叙述不合并）。"""
    with environment(tmp_path, monkeypatch) as f:
        f["state"]["tsdata_rows"] = [
            dict(TIMELINE_ROW, txSerialNo="17", outputVoltage="220", power="2.2"),
            dict(
                TIMELINE_ROW,
                _ts="2026-11-05T06:30:41.000Z",
                txSerialNo="18",
                outputVoltage="380",
                power="9.9",
            ),
        ]

        def dispatch(_, config, run):
            call_tool(f, run, "cpms_order_detail")
            call_tool(f, run, "cpms_charging_timeline", {"order_no": ORDER})
            evidence, head, _ = call_tool(f, run, "cpms_order_process_stats")
            assert evidence["data"]["sample_count"] == 2
            assert evidence["data"]["metrics"] == []
            assert "流水" in evidence["data"]["limitations"]
            finish(f, run, head, "窗口混入多笔流水，统计未合并。", [])

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        result = (
            f["client"]
            .post(
                "/v1/assistant/questions", headers=HEADERS, json={"question": "看看功率", "order_no": ORDER}
            )
            .json()
        )
        assert terminal(f, result["qa_id"]).json()["status"] == "completed"


def test_process_statistics_refuses_when_no_series_has_rows(tmp_path, monkeypatch):
    """监测表与时序都拿不到行：拒绝，不落一个零样本统计对象。"""
    with environment(tmp_path, monkeypatch) as f:
        observed = {}

        def dispatch(_, config, run):
            call_tool(f, run, "cpms_order_detail")
            call_tool(f, run, "cpms_charging_samples")
            client = f["client"]
            headers = {"Authorization": "Bearer " + NODE_A_TOKEN}
            publication = client.post("/v1/internal/charging-status/execution-config/" + run, headers=headers)
            headers["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
            headers["X-AI-Ops-Tool-Contract"] = "request-target/2"
            grant = client.post(
                f"/v1/internal/operator-runs/{run}/tools/cpms_order_process_stats/authorization",
                headers=headers,
                json={"arguments": {}},
            )
            assert grant.status_code == 200, grant.text
            body = grant.json()
            refused = client.get(
                body["base_url"] + body["request"]["path"],
                headers={"Authorization": "Bearer " + body["bearer"]},
            )
            observed["status"] = refused.status_code
            observed["code"] = refused.json()["error"]["code"]
            finish(f, run, headers, "两源都没有过程样本。", [])

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        result = (
            f["client"]
            .post(
                "/v1/assistant/questions", headers=HEADERS, json={"question": "看看功率", "order_no": ORDER}
            )
            .json()
        )
        assert terminal(f, result["qa_id"]).json()["status"] == "completed"
        assert observed == {"status": 403, "code": "OPERATOR_EVIDENCE_MISSING"}
