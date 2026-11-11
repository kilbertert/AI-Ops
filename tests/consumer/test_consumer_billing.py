"""账务记录与实际银行收付分开；只读原订单且保留空值/零值。"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from execution.test_app_runtime_dify_entry import HEADERS, dify_chat
from execution.test_app_runtime_node_entry import ORDER_A

from aiops_diagnostics.consumer.consumer_billing import (
    _amount,
    billing_answer,
    is_billing_question,
    read_billing_record,
)
from aiops_diagnostics.data.charging_status import ChargingStatusError
from consumer.test_consumer_chat_service import Client, done
from consumer.test_consumer_live_context import ask, wire


@pytest.fixture
def backend():
    state = {
        "row": {
            "orderNo": ORDER_A,
            "totalAmount": "2.26",
            "payAmount": "2.26",
            "refundStatus": 0,
            "refundAmount": 0,
        },
        "calls": [],
        "after": None,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            state["calls"].append((self.path, self.headers.get("user-id")))
            if state["after"]:
                state["after"]()
            body = json.dumps(
                {
                    "code": 0,
                    "data": {"orderNo": ORDER_A, "chOrderInfoModel": state["row"], **state.get("extra", {})},
                }
            ).encode()
            body = state.get("raw", body)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()


def test_billing_record_is_not_a_payment_confirmation_and_null_is_not_zero(backend):
    def read():
        return read_billing_record(backend["base"], ORDER_A, tenant_id="TENANT-A", user_id="USER-A")

    snapshot = read()
    assert snapshot["recorded_refund_amount"] == "0"
    assert snapshot["order_amount"] == "2.26"
    text = billing_answer(snapshot, "zh")["blocks"][0]["text"]
    # 断言**语义**：支付那一行必须否认"已扣款"，退款那一行必须否认"已到账"。
    # 不抄整句 —— 抄了测试就变成文案的副本，改措辞要改两处，而真正要拦的是
    # 「免责被简化成听起来钱已经到账」这一种改法。
    # 只看**逐项解释行**（`·` 开头）：结论句里也会出现「支付金额」四个字，抓错了会空断言。
    payment_line = next(line for line in text.splitlines() if line.startswith("·") and "支付金额" in line)
    refund_line = next(line for line in text.splitlines() if line.startswith("·") and "退款" in line)
    assert "扣款" in payment_line and any(w in payment_line for w in ("不等", "不代表", "不证明"))
    assert "到账" in refund_line and any(w in refund_line for w in ("不等", "不代表", "不证明"))
    assert "币种" in text and "原订单页面" in text
    assert "没有退款记录" in text and "已退金额 0" in text
    backend["row"].update(refundStatus=2, refundAmount="1.10", payAmount=None)
    snapshot = read()
    assert snapshot["recorded_payment_amount"] is None
    assert "partial refund is recorded" in billing_answer(snapshot, "en")["blocks"][0]["text"]
    backend["row"]["refundStatus"] = 987
    assert "待核实" in billing_answer(read(), "zh")["blocks"][0]["text"]
    backend["row"]["orderNo"] = "WRONG"
    with pytest.raises(ChargingStatusError):
        read()


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "-1", "abc", [], "1e-999999", "100000000000000"])
def test_invalid_money_is_rejected(value):
    with pytest.raises(ChargingStatusError):
        _amount(value)


def test_guidance_is_not_treated_as_a_billing_query():
    assert is_billing_question("查询我的最新订单退款记录")
    assert is_billing_question("check my latest order payment record")
    for text in ["如何申请退款？", "How do I apply for a refund?", "退款规则是什么？", "如何查看支付记录？"]:
        assert not is_billing_question(text)


def configure_billing(env, monkeypatch, tmp_path, backend):
    runtime, kb = wire(env, monkeypatch, tmp_path)
    doc = json.loads(env["bindings"].read_text())
    doc["bindings"][0]["live_billing_record"] = True
    env["bindings"].write_text(json.dumps(doc))
    env["settings"].charging_status_base_url = backend["base"]
    return runtime, kb


def test_original_api_delivers_billing_and_reauthorizes_history(tmp_path, monkeypatch, backend):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = configure_billing(env, monkeypatch, tmp_path, backend)
        first = ask(env, "查询我的最新订单退款记录")
        assert first.status_code == 202, first.text
        final = done(env, first.json()["qa_id"]).json()
        assert final["status"] == "completed", final
        # 措辞改成「支付金额」（ADR-0181）；这条断言要的是「账单事实确实讲到了支付那一项」。
        assert "支付金额" in final["result"]["blocks"][0]["text"]
        assert len(backend["calls"]) == 1 and not kb.calls
        run = runtime.dify_run_store.by_qa(first.json()["qa_id"])
        assert run.data_kind == "billing" and run.order_no == ORDER_A
        assert backend["calls"][0] == ("/chOrderInfo/getOrderInfoDetail/" + ORDER_A, "USER-A")
        from execution.test_app_runtime_node_entry import TOKENS

        monkeypatch.setitem(TOKENS, "token-b", ("USER-B", "TENANT-A"))
        other = {**HEADERS, "Authorization": "Bearer token-b"}
        denied = ask(env, f"check order {ORDER_A} refund record", headers=other)
        assert denied.status_code == 404, denied.text
        env["orders"].ownership.clear()
        assert done(env, first.json()["qa_id"]).status_code == 404
        assert len(Client.calls) == 1 and len(backend["calls"]) == 1


def test_billing_revoked_during_read_is_not_disclosed(tmp_path, monkeypatch, backend):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = configure_billing(env, monkeypatch, tmp_path, backend)
        backend["after"] = lambda: env["orders"].ownership.clear()
        first = ask(env, "查询我的最新订单退款记录").json()
        assert done(env, first["qa_id"]).json()["status"] == "failed"
        run = runtime.dify_run_store.by_qa(first["qa_id"])
        assert runtime.dify_run_store.context(run.run_id) is None


def composition_source(backend):
    # 独立合成源：字段来自父会话核对的DTO，数额不由候选输出生成。
    backend["row"].update(
        totalAmount="8.50",
        payAmount="7.50",
        electricity="6.25",
        time="30",
        createdTime="2026-11-11 08:00:00",
        stopTime="2026-11-11 08:31:00",
        electricityFee="5.20",
        serviceFee="3.30",
        launchFee="0",
        dsElectricFee="0.10",
        dsServiceFee="0.02",
        parkFee="0.50",
        occupyFee=None,
        marketAmount="0.70",
        vat="6",
        vatFee="0.51",
        phone="SECRET-PHONE",
        customer={"name": "SECRET-NAME"},
    )
    backend["extra"] = {
        "periodFeeDetail": [
            dict(
                startTime="08:00",
                endTime="08:15",
                electricFee="2.00",
                serviceFee="1.00",
                totalFee="3.00",
                electricity="2.50",
                feeRate="1.2000",
                type=1,
            ),
            dict(
                startTime="08:15",
                endTime="08:30",
                electricFee="3.20",
                serviceFee="2.30",
                totalFee="5.50",
                electricity="3.75",
                feeRate="1.4667",
                type=2,
            ),
        ]
    }


def read_composition(backend, **kwargs):
    return read_billing_record(backend["base"], ORDER_A, tenant_id="TENANT-A", user_id="USER-A", **kwargs)


def test_composition_keeps_source_amounts_periods_and_no_personal_data(backend):
    composition_source(backend)
    result = read_composition(backend)
    assert result["order_summary"]["time"] == "30"
    assert result["order_summary"]["electricity"] == "6.25"
    assert result["order_summary"]["createdTime"] == "2026-11-11 08:00:00"
    assert result["fee_components"] == {
        "electricityFee": "5.20",
        "serviceFee": "3.30",
        "launchFee": "0",
        "dsElectricFee": "0.10",
        "dsServiceFee": "0.02",
        "parkFee": "0.50",
        "occupyFee": None,
        "marketAmount": "0.70",
        "vat": "6",
        "vatFee": "0.51",
        "missing_fields": ["occupyFee"],
        "invalid_fields": [],
    }
    periods = result["periods"]
    assert periods["source_count"] == 2 and periods["omitted_count"] == 0
    assert [r["totalFee"] for r in periods["records"]] == ["3.00", "5.50"]
    assert [r["electricFee"] for r in periods["records"]] == ["2.00", "3.20"]
    assert [r["feeRate"] for r in periods["records"]] == ["1.2000", "1.4667"]
    assert result["currency"] is None and result["currency_status"] == "missing"
    assert "SECRET" not in json.dumps(result)
    assert len(backend["calls"]) == 1


@pytest.mark.parametrize("bad", [None, True, "NaN", "Infinity", "-0.01", {}, "1e-999999", "9" * 100])
def test_bad_component_is_explicitly_unavailable_not_zero(backend, bad):
    backend["row"].update(electricityFee=bad, serviceFee=0)
    fields = read_composition(backend)["fee_components"]
    assert fields["electricityFee"] is None and fields["serviceFee"] == "0"
    assert "electricityFee" in fields["missing_fields" if bad is None else "invalid_fields"]


@pytest.mark.parametrize(
    "periods,status", [(None, "missing"), ([], "empty"), ({}, "invalid"), ([False], "partial")]
)
def test_missing_or_malformed_periods_are_reported(backend, periods, status):
    backend["extra"] = {"periodFeeDetail": periods}
    result = read_composition(backend)["periods"]
    assert result["status"] == status


def test_period_bounds_currency_conflict_and_numeric_json_precision(backend):
    composition_source(backend)
    backend["row"].update(currency="USD", currencyCode="EUR")
    row = backend["extra"]["periodFeeDetail"][0]
    row.update(startTime="x" * 65, electricity=None, phone="SECRET")
    backend["extra"]["periodFeeDetail"] = [row] * 25
    result = read_composition(backend)
    assert result["currency"] is None and result["currency_status"] == "conflicting"
    assert len(result["periods"]["records"]) == 24 and result["periods"]["omitted_count"] == 1
    assert result["periods"]["status"] == "partial"
    assert result["periods"]["records"][0]["startTime"] is None
    assert "SECRET" not in json.dumps(result) and len(json.dumps(result).encode()) < 24000
    backend["extra"]["periodFeeDetail"] = []
    backend["row"]["currencyCode"] = "USD"
    assert read_composition(backend)["currency"] == "USD"
    backend["raw"] = (
        '{"code":0,"data":{"chOrderInfoModel":{"orderNo":"'
        + ORDER_A
        + '","totalAmount":99999999999.12345678,"electricityFee":0.12345678}}}'
    ).encode()
    result = read_composition(backend)
    assert result["order_amount"] == "99999999999.12345678"
    assert result["fee_components"]["electricityFee"] == "0.12345678"


def test_legacy_projection_does_not_expand_old_validator_evidence(backend):
    composition_source(backend)
    snapshot = read_composition(backend, include_composition=False)
    assert set(snapshot) == {
        "order_no",
        "order_amount",
        "recorded_payment_amount",
        "refund_status",
        "recorded_refund_amount",
        "observed_at",
        "source",
    }
    assert "8.50" in billing_answer(snapshot, "en")["blocks"][0]["text"]


def test_invalid_tax_currency_and_period_type_do_not_invent_valid_values(backend):
    composition_source(backend)
    backend["row"].update(vat="100.01", currency=["USD", "EUR"])
    backend["extra"]["periodFeeDetail"][0]["type"] = True
    result = read_composition(backend)
    assert result["fee_components"]["vat"] is None
    assert result["fee_components"]["invalid_fields"] == ["vat"]
    assert result["currency"] is None and result["currency_status"] == "invalid"
    assert result["periods"]["records"][0]["type"] is None
    assert "type" in result["periods"]["records"][0]["invalid_fields"]
    backend["raw"] = b'{"code":0,"data":' + b" " * 131072
    with pytest.raises(ChargingStatusError):
        read_composition(backend)


@pytest.mark.parametrize(
    "value,period",
    [
        ("2026-11-11 08:00:00", False),
        ("2026-11-11T08:00:00.123456789+08:00", False),
        ("2026-11-11T08:00:00Z", False),
        ("24:00", True),
        ("24:00:00", True),
        ("08:15:30", True),
    ],
)
def test_source_time_preserves_known_formats_without_inventing_timezone(value, period):
    from aiops_diagnostics.consumer.consumer_billing import _source_time

    assert _source_time(value, period=period) == value


@pytest.mark.parametrize("value", ["2026-02-30 08:00:00", "24:01", "25:00", "08:60", "08:15:60", ""])
def test_source_time_rejects_invalid_calendar_or_clock(value):
    from aiops_diagnostics.consumer.consumer_billing import _source_time

    with pytest.raises(ChargingStatusError):
        _source_time(value, period=True)
