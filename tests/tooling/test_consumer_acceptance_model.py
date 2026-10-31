"""合成供应商必须满足现役事实协议，不能让验收停在旧输出形状。"""

import importlib.util
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from aiops_diagnostics.consumer.consumer_knowledge import final_answer


@pytest.mark.parametrize("source", ["cpms.chargingInfo", "cpms.orderDetail"])
def test_acceptance_model_uses_fact_segments(source):
    path = Path(__file__).resolve().parents[2] / "tools/verification/verify_consumer_chatflow.py"
    spec = importlib.util.spec_from_file_location("consumer_acceptance_model", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    snapshot = {
        "order_no": "ORDER-3001",
        "source": source,
        "observed_at": "2026-10-30T00:00:00Z",
        "status_role": "finished",
        "status_code": "1",
    }
    if source == "cpms.orderDetail":
        snapshot.update(
            order_amount="27.36", recorded_payment_amount="20", recorded_refund_amount="0", refund_status="0"
        )
    context = {"mode": "documents", "status": "live", "fact_contract": "slots", "live_snapshot": snapshot}
    body = {"messages": [{"role": "user", "content": "本轮取数结果：" + json.dumps(context)}]}
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions", data=json.dumps(body).encode()
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = json.load(response)["choices"][0]["message"]["content"]
        answer = final_answer(raw, context, "zh", None)
        assert "ORDER-3001" in answer["blocks"][0]["text"]
        if source == "cpms.orderDetail":
            assert "27.36" in answer["blocks"][0]["text"]
    finally:
        server.shutdown()
        server.server_close()
