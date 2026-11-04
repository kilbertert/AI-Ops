"""给定时间窗连续充电；用独立穷举验证最低价，不引用真实站点。"""

from decimal import Decimal

import pytest


def rates():
    return [
        {"startTime": "00:00", "endTime": "08:00", "price": "1", "feePrice": "0.5"},
        {"startTime": "08:00", "endTime": "23:59", "price": "3", "feePrice": "1"},
    ]


def test_continuous_plan_finds_cheapest_start_and_shows_cost():
    from aiops_diagnostics.operator.station_pricing import cheapest

    value = cheapest(rates(), "06:00", "10:00", "3", "2")
    assert value["start"] == "06:00" and value["end"] == "07:30"
    assert value["electricity"] == Decimal("3") and value["service"] == Decimal("1.5")


def test_boundary_optimum_matches_minute_bruteforce():
    from aiops_diagnostics.operator.station_pricing import cheapest

    periods = [
        {"startTime": "00:00", "endTime": "01:00", "price": "4", "feePrice": "0"},
        {"startTime": "01:00", "endTime": "02:00", "price": "1", "feePrice": "0"},
        {"startTime": "02:00", "endTime": "23:59", "price": "3", "feePrice": "0"},
    ]
    value = cheapest(periods, "00:00", "03:00", "1.5", "1")
    costs = [
        sum(4 if t < 60 else 1 if t < 120 else 3 for t in range(start, start + 90)) / 60
        for start in range(91)
    ]
    assert float(value["electricity"]) == min(costs)
    assert value["start"] == "01:00" and value["end"] == "02:30"


@pytest.mark.parametrize("change", ["gap", "overlap", "negative", "no-fit", "zero", "nan"])
def test_unprovable_plan_is_not_a_lowest_price(change):
    from aiops_diagnostics.operator.station_pricing import PlanUnavailable, cheapest

    rows = rates()
    energy, power = "3", "2"
    if change == "gap":
        rows[0]["endTime"] = "07:00"
    if change == "overlap":
        rows[0]["endTime"] = "09:00"
    if change == "negative":
        rows[0]["price"] = "-1"
    if change == "no-fit":
        energy = "100"
    if change == "zero":
        power = "0"
    if change == "nan":
        energy = "NaN"
    with pytest.raises(PlanUnavailable):
        cheapest(rows, "06:00", "10:00", energy, power)


def test_original_chat_clarifies_then_returns_scoped_frozen_plan(tmp_path, monkeypatch):
    import json
    from datetime import UTC, datetime, timedelta

    from aiops_diagnostics.operator import operator_cpms
    from operations.test_operator_cpms import HEADERS, NODE_A_TOKEN, environment, terminal
    from operations.test_station_tariff import responses

    with environment(tmp_path, monkeypatch) as f:
        day = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
        original = operator_cpms.cpms_get
        data = responses()
        calls = []

        def cpms(url, bearer, tenant, **kwargs):
            from urllib.parse import urlsplit

            path = urlsplit(url).path
            calls.append(path)
            return data[path] if path in data else original(url, bearer, tenant, **kwargs)

        monkeypatch.setattr(operator_cpms, "cpms_get", cpms)
        arguments = [
            {"name": "天河测试站", "date": day, "start": "06:00", "end": "10:00"},
            {
                "energy": "3",
                "power": "2",
                "timezone": "Asia/Shanghai",
                "basis": "普通按量、不用优惠、不预约、不买保险、充完离场",
            },
        ]
        checked = []

        def dispatch(_, config, run_id):
            head = {
                "Authorization": "Bearer " + NODE_A_TOKEN,
                "X-AI-Ops-Tool-Contract": operator_cpms.OBJECT_CONTRACT,
            }
            pub = (
                f["client"]
                .post("/v1/internal/charging-status/execution-config/" + run_id, headers=head)
                .json()
            )
            head["X-AI-Ops-Exec-Config"] = pub["sha256"]
            args = arguments.pop(0)
            path = f"/v1/internal/operator-runs/{run_id}/tools/cpms_station_plan"
            response = f["client"].post(path + "/authorization", headers=head, json={"arguments": args})
            assert response.status_code == 200, response.text
            grant = response.json()
            req = grant["request"]
            url = grant["base_url"] + req["path"] + "?" + req["query"]
            response = f["client"].get(url, headers={"Authorization": "Bearer " + grant["bearer"]})
            assert response.status_code == 200, response.text
            value = response.json()["data"]["value"]
            evidence = {
                "tool": "cpms_station_plan",
                "inputs": grant["inputs"],
                "source": "cpms.stationPlan",
                "observed_at": datetime.now(UTC).isoformat(),
                "data": value,
            }
            ack = f["client"].post(
                path + "/evidence",
                headers=head,
                json={"authorization_id": grant["authorization_id"], "evidence": evidence},
            )
            assert ack.status_code == 200, ack.text
            answer = {"kind": "station_plan", "text": "", "name": "", "address_terms": []}
            ack = f["client"].post(
                f"/v1/internal/operator-runs/{run_id}/completions",
                headers=head,
                json={"status": "completed", "answer": json.dumps(answer)},
            )
            assert ack.status_code == 200, ack.text
            checked.append(value)

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        first = f["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": f"天河测试站{day} 06:00到10:00最省费用方案"},
        )
        assert first.status_code == 202, first.text
        done = terminal(f, first.json()["qa_id"])
        assert done.json()["status"] == "completed", done.text
        assert "所需电量" in done.text and not calls
        second = f["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={
                "question": "补充3kWh和2kW，Asia/Shanghai，普通按量、不用优惠、不预约、不买保险、充完离场",
                "conversation_id": first.json()["conversation_id"],
            },
        )
        assert second.status_code == 202, second.text
        done = terminal(f, second.json()["qa_id"])
        assert done.json()["status"] == "completed", done.text
        assert "06:00–07:30" in done.text and "5.50" in done.text
        assert checked[-1]["rules"]["template"]["feeTemplateItems"][0]["price"] == "1"


def test_total_tax_and_prepaid_limit_do_not_turn_into_discount():
    from aiops_diagnostics.operator.station_pricing import plan_text
    from operations.test_station_tariff import responses

    data = responses()
    template = data["/chFeeTemplate/TARIFF-1"]["data"]
    template.update(vat="2", enablePackage=True, feeTemplatePackages=[{"type": 2, "price": "10"}])
    rules = {
        "name": "合成站",
        "date": "2026-11-06",
        "holiday": False,
        "timezone": "Asia/Shanghai",
        "template": template,
        "extras": {"parkFeeTemplateId": False},
    }
    args = {"start": "06:00", "end": "10:00", "energy": "3", "power": "2", "timezone": "Asia/Shanghai"}
    text = plan_text(rules, args)
    assert "税额 5.50 × 2% = 0.11；含税合计 5.61" in text
    assert "最小额度为 10.00" in text
    assert "预计节省0.00" in text


@pytest.mark.parametrize(
    "arguments,question",
    [({"energy": "2026"}, "2026-11-06去充电"), ({"power": "7"}, "充电7kWh"), ({"energy": "5"}, "充电15kWh")],
)
def test_plan_numbers_need_their_own_unit(arguments, question):
    from aiops_diagnostics.operator.operator_cpms import OperatorRefused, validate_tool_arguments

    with pytest.raises(OperatorRefused):
        validate_tool_arguments("cpms_station_plan", arguments, question)
