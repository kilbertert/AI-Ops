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
                {"code": 0, "data": {"orderNo": ORDER_A, "chOrderInfoModel": state["row"]}}
            ).encode()
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
