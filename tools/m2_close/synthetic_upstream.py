#!/usr/bin/env python3
"""M2-CLOSE-01 隔离实验用的**合成上游**。只为本轮验收存在，不是生产组件。

三张面孔，都只在 Docker 内网可达：

1. **桩 CPMS**（``:9200``）：``/charging-pile/chOrderInfo/chargingInfo/{order}``，形状与真实上游
   一致（``{"code":0,"msg":…,"data":{…}}``），并**记调用次数** —— 「负例必须零次取数」
   靠计数判，不靠「日志里没看见」。
2. **桩模型**（``:9100``，OpenAI 兼容）：M1 已证明这条路可用。它按脚本决定模型行为：
   - ``NORMAL``：第一次要求调用 ``charging_status_snapshot``（空参数），拿到工具结果后回显；
   - ``ADVERSARIAL`` / ``ADVERSARIAL_HTTP``：工具清单里没有 shell / http_request，
     但**照样点名调用它们**（一次只点名一个，见 do_POST 里的说明）
     —— 节点是否可执行由节点自己的工具集合决定（IC-07 要的正是这个）。
   - ``SILENT``：不调工具、直接收尾，用来制造「记录已签发但没人领取」的状态。
3. **控制面**（``:9201``）：读计数、切模式。只给测试驱动用。

它不模拟 Snail 服务端：真实服务端与真实节点都在跑，模型才是在这里被替换的那一个
（与 M1 相同）。因此「节点收到的 sid == Gateway run_id」是真实链路证出来的。
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ORDERS_FILE = Path(os.environ.get("M2CLOSE_ORDERS_FILE", "/work-data/nodes.json"))
COUNTS = {"charging_info": 0, "unknown_order": 0, "model_calls": 0, "adversarial_calls": 0}
MODE = {"value": "NORMAL"}
TOOL = "charging_status_snapshot"
#: 模型桩**自己**检查请求里有没有这些秘密：凭据不该出现在模型请求里，
#: 而「日志里没看到」不足以证明这一点（R1-4 / IC-08）。
SECRETS = [
    value
    for value in (
        os.environ.get("M2CLOSE_SECRET_LOCAL"),
        os.environ.get("M2CLOSE_SECRET_NODE"),
        os.environ.get("M2CLOSE_SECRET_APP"),
    )
    if value
]
#: 桩模型**期望**收到的 model 字段（由部署件给出）。空 = 不检查（旧调用方）。
EXPECTED_MODEL = os.environ.get("M2CLOSE_EXPECTED_MODEL", "").strip()
#: 明确拒绝的 model：项目内部资源名被误当成供应商模型标识时，这里必须拒绝，
#: 否则「名对了没有」这件事会一直用一次成功来掩盖。
REJECTED_MODEL = os.environ.get("M2CLOSE_REJECTED_MODEL", "").strip()
STATE: dict[str, Any] = {
    "model_tool_sets": [],
    "secret_hits": [],
    "tool_result_fields": [],
    "modes_seen": [],
    "in_flight": 0,
    "max_in_flight": 0,
}
DELAY = {"seconds": 0.0}


def log(message: str) -> None:
    print("[stub] " + message, flush=True)


def orders() -> dict[str, Any]:
    return json.loads(ORDERS_FILE.read_text())


class _JsonHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        pass

    def _send(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CpmsHandler(_JsonHandler):
    def do_GET(self) -> None:  # noqa: N802
        prefix = "/charging-pile/chOrderInfo/chargingInfo/"
        if not self.path.startswith(prefix):
            self._send({"code": 1, "msg": "not found", "data": None}, 404)
            return
        order_no = self.path[len(prefix) :]
        table = orders()
        if order_no not in table:
            COUNTS["unknown_order"] += 1
            self._send({"code": 1, "msg": f"订单{order_no}不存在", "data": None, "ok": False})
            return
        COUNTS["charging_info"] += 1
        # 并发判据需要「真的重叠」：延迟与在飞计数都在这里，不在调用方。
        STATE["in_flight"] += 1
        STATE["max_in_flight"] = max(STATE["max_in_flight"], STATE["in_flight"])
        try:
            if DELAY["seconds"]:
                time.sleep(DELAY["seconds"])
        finally:
            STATE["in_flight"] -= 1
        # 刻意带一批真实上游会带的敏感字段：接口的裁剪由它来证明。
        self._send(
            {
                "code": 0,
                "msg": "success",
                "data": {
                    "orderNo": order_no,
                    "status": table[order_no]["status"],
                    "plateNo": "DO-NOT-LEAK",
                    "userId": "DO-NOT-LEAK",
                    "deviceId": "DO-NOT-LEAK",
                    "siteName": "DO-NOT-LEAK",
                },
            }
        )


class ControlHandler(_JsonHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/control/counts":
            # ``max_in_flight`` 与计数一起回：并发判据要读的是「有没有真的重叠」，
            # 它和「一共读了几次」是同一份观测的两个侧面。
            self._send(
                {
                    **COUNTS,
                    "max_in_flight": STATE["max_in_flight"],
                    "stub_rev": STATE.get("stub_rev", 0),
                }
            )
        elif self.path == "/control/state":
            self._send({"mode": MODE["value"], **STATE})
        else:
            self._send({"mode": MODE["value"]})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if self.path == "/control/mode":
            MODE["value"] = json.loads(self.rfile.read(length)).get("mode", "NORMAL")
            self._send({"mode": MODE["value"]})
        elif self.path == "/control/delay":
            DELAY["seconds"] = float(json.loads(self.rfile.read(length)).get("seconds", 0))
            self._send({"delay_seconds": DELAY["seconds"]})
        else:
            self._send({"error": "not found"}, 404)


class ModelHandler(_JsonHandler):
    """OpenAI 兼容的最小实现；只做一件事：按模式产出工具调用。"""

    def do_GET(self) -> None:  # noqa: N802
        self._send({"object": "list", "data": [{"id": "m2close-model", "object": "model"}]})

    def _chunk(self, rid: str, model: str, delta: dict, finish: str | None = None) -> dict:
        return {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def _stream(self, frames: list[dict]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for frame in frames:
            self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        COUNTS["model_calls"] += 1
        # 记录**实际收到的 model**：把它与部署件声明的 M2LIVE_MODEL_ID 对照，
        # 是「供应商会收到什么」的唯一直接证据。
        received_model = str(body.get("model") or "")
        STATE.setdefault("models_seen", []).append(received_model)
        if EXPECTED_MODEL and received_model != EXPECTED_MODEL:
            COUNTS["bad_model"] = COUNTS.get("bad_model", 0) + 1
            log(f"model mismatch: expected={EXPECTED_MODEL!r} got={received_model!r}")
            self._send({"error": {"code": "stub.unexpected_model", "message": "model mismatch"}}, 400)
            return
        if REJECTED_MODEL and received_model == REJECTED_MODEL:
            COUNTS["bad_model"] = COUNTS.get("bad_model", 0) + 1
            log(f"model rejected: {REJECTED_MODEL!r}")
            self._send({"error": {"code": "stub.rejected_model", "message": "internal name as model"}}, 400)
            return
        messages = body.get("messages") or []
        offered = [item.get("function", {}).get("name") for item in (body.get("tools") or [])]
        tool_results = [m for m in messages if m.get("role") == "tool"]
        model = body.get("model") or "m2close-model"
        rid = f"stub-{int(time.time() * 1000)}"
        adversarial = MODE["value"].startswith("ADVERSARIAL")
        STATE["modes_seen"].append(MODE["value"])
        STATE["model_tool_sets"].append(sorted(str(name) for name in offered))
        raw = json.dumps(body, ensure_ascii=False)
        for secret in SECRETS:
            if secret and secret in raw:
                STATE["secret_hits"].append(secret)
        for message in tool_results:
            content = message.get("content")
            try:
                payload = json.loads(content) if isinstance(content, str) else content
            except ValueError:
                continue
            if isinstance(payload, dict):
                STATE["tool_result_fields"].append(sorted(payload))
                STATE.setdefault("tool_result_orders", []).append(str(payload.get("order_no") or ""))
        log(f"model mode={MODE['value']} offered={offered} tool_results={len(tool_results)}")

        if MODE["value"] == "SILENT":
            # 不调工具、直接收尾：用来制造「记录已签发但没人领取」的状态。
            self._stream(
                [
                    self._chunk(rid, model, {"role": "assistant", "content": "no tool call"}),
                    self._chunk(rid, model, {}, finish="stop"),
                ]
            )
            return

        if adversarial:
            COUNTS["adversarial_calls"] += 1
            # 一次只点名一个不存在的工具：Spring AI 在**第一个**未知工具上就抛异常并中止，
            # 把两个塞进同一次响应会让第二个永远没机会被尝试，「它也被拒绝」就成了空话。
            extra = {
                "ADVERSARIAL_RAG": ("rag_search", {"arg0": "订单状态"}),
                "ADVERSARIAL_SKILL": ("read_skill", {"arg0": "diagnose"}),
                "ADVERSARIAL_HTTP": (
                    "http_request",
                    {
                        "arg0": "http://m2close-gateway:9200/charging-pile/chOrderInfo/chargingInfo/ORDER-B",
                        "arg1": "GET",
                    },
                ),
            }
            if MODE["value"] in extra:
                name, arguments = extra[MODE["value"]]
                self._stream(
                    [
                        self._chunk(
                            rid,
                            model,
                            {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_extra_1",
                                        "type": "function",
                                        "function": {"name": name, "arguments": json.dumps(arguments)},
                                    }
                                ],
                            },
                        ),
                        self._chunk(rid, model, {}, finish="tool_calls"),
                    ]
                )
                return
            call = {
                "id": "call_shell_1",
                "type": "function",
                "function": {
                    "name": "shell",
                    # 如果节点真的注册了 shell，这两条足以把业务数据读出来 ——
                    # IC-07 要证明的正是它们**没有**可执行实现。
                    "arguments": json.dumps({"arg0": "cat /data/gateway-orders.db"}),
                },
            }
            self._stream(
                [
                    self._chunk(
                        rid, model, {"role": "assistant", "content": "", "tool_calls": [{"index": 0, **call}]}
                    ),
                    self._chunk(rid, model, {}, finish="tool_calls"),
                ]
            )
            return

        if not tool_results:
            call = {
                "id": "call_tool_1",
                "type": "function",
                "function": {"name": TOOL, "arguments": "{}"},
            }
            self._stream(
                [
                    self._chunk(
                        rid, model, {"role": "assistant", "content": "", "tool_calls": [{"index": 0, **call}]}
                    ),
                    self._chunk(rid, model, {}, finish="tool_calls"),
                ]
            )
            return

        text = "SNAPSHOT " + str(tool_results[-1].get("content") or "")
        self._stream(
            [
                self._chunk(rid, model, {"role": "assistant", "content": text}),
                self._chunk(rid, model, {}, finish="stop"),
            ]
        )


def serve(port: int, handler: type[BaseHTTPRequestHandler]) -> None:
    threading.Thread(
        target=ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever, daemon=True
    ).start()


def loopback_relay(port: int, target_host: str, target_port: int) -> None:
    """把 ``127.0.0.1:port`` 原样转给另一台主机。

    Gateway 的服务端配置只允许对回环地址使用明文 HTTP（``canonical_gateway_url``），
    而 Snail 服务端在另一个容器里。这条中继让「地址是回环」与「服务在别处」同时成立，
    且不改变 Gateway 的任何判据 —— 它是实验拓扑的一部分，不是产品代码。
    """

    def pipe(source: socket.socket, sink: socket.socket) -> None:
        try:
            while True:
                chunk = source.recv(65536)
                if not chunk:
                    break
                sink.sendall(chunk)
        except OSError:
            pass
        finally:
            for sock in (source, sink):
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(64)
    log(f"loopback relay :{port} -> {target_host}:{target_port}")

    def accept_loop() -> None:
        while True:
            try:
                client, _ = listener.accept()
                upstream = socket.create_connection((target_host, target_port), timeout=10)
            except OSError:
                continue
            threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()


def main() -> int:
    # 模型桩与 CPMS/控制面跑在不同容器里：前者给节点用；后者必须与 Gateway 同容器，
    # 因为 ``canonical_gateway_url`` 只允许对回环地址使用明文 HTTP。
    # 控制面在两边都开：模型桩那个容器用它切 NORMAL/ADVERSARIAL，Gateway 容器用它读
    # CPMS 调用计数。两个进程各自持有自己的一份状态 —— 这正是它们的职责边界。
    # 端口可用环境变量挪开：本机快照测试不该和任何常驻服务抢固定端口。
    control_port = int(os.environ.get("M2CLOSE_CONTROL_PORT", "9201"))
    cpms_port = int(os.environ.get("M2CLOSE_CPMS_PORT", "9200"))
    model_port = int(os.environ.get("M2CLOSE_MODEL_PORT", "9100"))
    # 自报版本：让调用方能证明「跑的是哪一版桩」，而不是靠猜（曾经因此排查了半天）。
    STATE["stub_rev"] = 3
    serve(control_port, ControlHandler)
    if os.environ.get("M2CLOSE_MODEL_ONLY") != "1":
        serve(cpms_port, CpmsHandler)
        log(f"cpms stub :{cpms_port}, control :{control_port}")
    time.sleep(1)
    forward = os.environ.get("M2CLOSE_FORWARD")
    if forward:
        host, _, port = forward.partition(":")
        loopback_relay(8900, host, int(port))
    log(f"model stub :{model_port}")
    ThreadingHTTPServer(("0.0.0.0", model_port), ModelHandler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
