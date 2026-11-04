"""合成计费快照；断言真实取证边界与金额核对，不使用生产记录。"""

from copy import deepcopy

import pytest

from aiops_diagnostics.operator import operator_tools
from aiops_diagnostics.operator.operator_conversation import OperatorRefused
from operations.test_operator_cpms import scope


def snapshot():
    return {
        "code": 0,
        "data": {
            "orderNo": "ORDER-1",
            "tenantId": "TENANT-A",
            "phone": "PRIVATE",
            "feeTemplate": {
                "billingMode": 2,
                "type": 0,
                "name": "合成时段",
                "feeTemplateItems": [
                    {"startTime": "00:00", "endTime": "08:00", "price": "1.20", "feePrice": "0.30"},
                    {"startTime": "08:00", "endTime": "23:59", "price": "2.00", "feePrice": "0.50"},
                ],
            },
            "periodFeeDetail": [
                {
                    "startTime": "00:00",
                    "endTime": "08:00",
                    "electricity": "2.00",
                    "electricFee": "2.40",
                    "serviceFee": "0.60",
                    "totalFee": "3.00",
                    "feeRate": "1.50",
                },
            ],
        },
    }


def test_historical_snapshot_is_a_declared_order_only_tool():
    tool = operator_tools.tool("cpms_order_tariff")
    assert tool.applies_to == ("order",) and tool.model_inputs == ()
    assert tool.request({"order_no": "ORDER-1"})["path"] == "/chFeeTemplateRecord/ORDER-1"


def test_snapshot_projection_strips_private_unknown_fields():
    from aiops_diagnostics.operator.order_pricing import read_snapshot

    body = snapshot()
    body["data"]["feeTemplate"]["contact"] = "PRIVATE"
    result = read_snapshot(body, scope(), "ORDER-1")
    assert "PRIVATE" not in str(result)
    assert result["feeTemplate"]["feeTemplateItems"][0]["price"] == "1.20"


@pytest.mark.parametrize("fault", ["tenant", "order", "array", "nested", "oversize"])
def test_snapshot_rejects_mismatch_or_unbounded_shape(fault):
    from aiops_diagnostics.operator.order_pricing import read_snapshot

    body = snapshot()
    if fault == "tenant":
        body["data"]["tenantId"] = "OTHER"
    if fault == "order":
        body["data"]["orderNo"] = "OTHER"
    if fault == "array":
        body["data"]["feeTemplate"]["feeTemplateItems"] = "invalid"
    if fault == "nested":
        body["data"]["feeTemplate"]["name"] = {"secret": "PRIVATE"}
    if fault == "oversize":
        body["data"]["periodFeeDetail"] *= 65
    with pytest.raises(OperatorRefused):
        read_snapshot(body, scope(), "ORDER-1")


def test_missing_snapshot_is_explicit_not_current_tariff_fallback():
    from aiops_diagnostics.operator.order_pricing import read_snapshot

    assert read_snapshot({"code": 0, "data": None}, scope(), "ORDER-1") == {"missing": True}


def test_historical_arithmetic_does_not_call_derived_energy_independent():
    from aiops_diagnostics.operator.order_pricing import historical_report

    body = snapshot()["data"]
    detail = {"electricityFee": "2.40", "serviceFee": "0.60", "electricity": "2.001"}
    text = historical_report(body, detail, "zh")
    assert "2.40 + 0.60 = 3.00" in text
    assert "独立电表读数" in text and "不能证明" in text
    assert "1.20" in text and "0.30" in text
    changed = deepcopy(body)
    changed["periodFeeDetail"][0]["totalFee"] = "3.50"
    assert "差额 0.50" in historical_report(changed, detail, "zh")


def test_live_gateway_tariff_lifecycle(tmp_path, monkeypatch):
    import json
    from datetime import UTC, datetime

    from aiops_diagnostics.operator import operator_cpms
    from operations.test_operator_cpms import CONTRACT, NODE_A_TOKEN, environment, request, terminal

    with environment(tmp_path, monkeypatch) as f:
        original = operator_cpms.cpms_get
        reads = []

        def upstream(url, bearer, tenant, **kwargs):
            if "/chFeeTemplateRecord/" in url:
                reads.append(url)
                data = snapshot()
                data["data"]["orderNo"] = "ORDER-1001"
                return data
            return original(url, bearer, tenant, **kwargs)

        monkeypatch.setattr(operator_cpms, "cpms_get", upstream)

        def dispatch(_, config, run_id):
            client = f["client"]
            head = {"Authorization": "Bearer " + NODE_A_TOKEN, **CONTRACT}
            publication = client.post("/v1/internal/charging-status/execution-config/" + run_id, headers=head)
            head["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
            for tool in ("cpms_order_detail", "cpms_order_tariff"):
                head["X-AI-Ops-Tool-Contract"] = (
                    operator_cpms.OBJECT_CONTRACT
                    if tool == "cpms_order_tariff"
                    else operator_cpms.TOOL_CONTRACT
                )
                path = f"/v1/internal/operator-runs/{run_id}/tools/{tool}"
                grant = client.post(path + "/authorization", headers=head, json={}).json()
                result = client.get(
                    grant["base_url"] + grant["request"]["path"],
                    headers={"Authorization": "Bearer " + grant["bearer"]},
                )
                assert result.status_code == 200, result.text
                if tool == "cpms_order_tariff":
                    assert "PRIVATE" not in result.text
                    evidence = {
                        "tool": tool,
                        "inputs": grant["inputs"],
                        "source": "cpms.orderTariff",
                        "observed_at": datetime.now(UTC).isoformat(),
                        "data": result.json()["data"]["value"],
                    }
                else:
                    evidence = operator_cpms.project_evidence(result.json(), "ORDER-1001", tool)
                    evidence.pop("order_no")
                    evidence["inputs"] = grant["inputs"]
                recorded = client.post(
                    path + "/evidence",
                    headers=head,
                    json={"authorization_id": grant["authorization_id"], "evidence": evidence},
                )
                assert recorded.status_code == 200, recorded.text
            from operations.test_operator_reply_facts import answer

            value = answer("本次核对。\n{{fact:order_facts}}")
            value["evidence_tools"] = ["cpms_order_detail", "cpms_order_tariff"]
            done = client.post(
                f"/v1/internal/operator-runs/{run_id}/completions",
                headers=head,
                json={"status": "completed", "answer": json.dumps(value)},
            )
            assert done.status_code == 200, done.text

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        created = request(f["client"])
        final = terminal(f, created.json()["qa_id"])
        assert final.json()["status"] == "completed", final.text
        assert "历史计费核对" in final.text and "1.20" in final.text
        assert len(reads) == 1


@pytest.mark.parametrize("failure", ["old-node", "transfer", "revoked"])
def test_tariff_refusal_precedes_business_read(tmp_path, monkeypatch, failure):
    from threading import Event

    from aiops_diagnostics.operator import operator_cpms
    from operations.test_operator_cpms import CONTRACT, NODE_A_TOKEN, environment, request, terminal

    with environment(tmp_path, monkeypatch) as f:
        verified = Event()

        def dispatch(_, config, run_id):
            client = f["client"]
            head = {"Authorization": "Bearer " + NODE_A_TOKEN, **CONTRACT}
            publication = client.post("/v1/internal/charging-status/execution-config/" + run_id, headers=head)
            head["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
            f["ctx"].operator_cpms.tasks[run_id].delivered.add("cpms_order_detail")
            if failure != "old-node":
                head["X-AI-Ops-Tool-Contract"] = operator_cpms.OBJECT_CONTRACT
            if failure == "revoked":
                f["resolver"].current = scope(site="SITE-OTHER")
            path = f"/v1/internal/operator-runs/{run_id}/tools/cpms_order_tariff/authorization"
            response = client.post(path, headers=head, json={})
            if failure in {"old-node", "revoked"}:
                assert response.status_code == 403, response.text
            else:
                assert response.status_code == 200, response.text
                grant = response.json()
                f["state"]["site"] = "SITE-OTHER"
                response = client.get(
                    grant["base_url"] + grant["request"]["path"],
                    headers={"Authorization": "Bearer " + grant["bearer"]},
                )
                assert response.status_code == 403, response.text
            assert not any("chFeeTemplateRecord" in path for path, _ in f["state"]["calls"])
            verified.set()
            raise RuntimeError("intentional refusal")

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        created = request(f["client"])
        final = terminal(f, created.json()["qa_id"])
        assert not final.json().get("result")
        assert verified.wait(5), "node callback did not reach the expected refusal"


def test_independent_order_period_energy_detects_rate_mismatch():
    from aiops_diagnostics.operator.order_pricing import historical_report

    body = snapshot()["data"]
    body["feeTemplate"]["feeTemplateItems"][0]["type"] = 3
    body["feeTemplate"]["feeTemplateItems"][1]["type"] = 1
    detail = {
        "type": 0,
        "deviceProtocol": "OCPP1.6",
        "launchType": "app",
        "packageType": None,
        "electricity": "2",
        "tipElectricity": "0",
        "peakElectricity": "0",
        "flatElectricity": "0",
        "valleyElectricity": "2",
        "electricityFee": "2.50",
        "serviceFee": "0.60",
    }
    text = historical_report(body, detail, "zh")
    assert "2 kWh × 1.20 = 2.40" in text
    assert "差额 0.10" in text
    # 同类多个不同时段价格不能拿总分时电量武断分配。
    body["feeTemplate"]["feeTemplateItems"][1]["type"] = 3
    assert "2 kWh ×" not in historical_report(body, detail, "zh")


def test_upstream_default_false_does_not_disable_a_tariff_item():
    from aiops_diagnostics.operator.order_pricing import historical_report

    body = snapshot()["data"]
    body["feeTemplate"]["feeTemplateItems"][0]["enabled"] = False
    assert "电价 1.20/kWh" in historical_report(body, {}, "zh")


def test_time_service_uses_custom_periods_with_verified_minute_unit():
    from aiops_diagnostics.operator.order_pricing import historical_report

    body = snapshot()["data"]
    body["feeTemplate"].update(
        {
            "billingMode": 1,
            "flat": "1.20",
            "flatService": "99.99",
            "customServiceItems": [{"startTime": "00:00", "endTime": "23:59", "price": "0.05"}],
        }
    )
    body["feeTemplate"]["feeTemplateItems"] = [{"startTime": "00:00", "endTime": "23:59", "type": 2}]
    text = historical_report(body, {}, "zh")
    assert "服务费 0.05/分钟" in text
    assert "99.99" not in text
