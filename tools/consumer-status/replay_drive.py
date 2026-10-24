#!/usr/bin/env python3
"""用**实际工具入口**跑浏览器驱动回放（不碰生产、不开窗口）。

为什么需要它：`replay_selector.py` 只在静态 DOM 上验证选择器，**没有执行**
`status_probe` 的驱动 —— 导航、输入、点击、请求捕获、按 qa_id 轮询、
助手区域读取、最终判据。那一段才是工具真正的运行路径。

这里的做法：用**同一个 `_drive()`**（同一个请求/响应捕获、同一个轮询、同一个
区域读取），只把 `steps` 换成离线版本：

- 页面从本地静态服务器加载（`dist/`，可选；没有就用 `about:blank` + 注入 DOM）；
- 所有 `/v1/` 请求在**路由层拦截**并返回合成数据，**未声明的请求直接 abort**；
- 因此**生产业务请求数恒为 0**。

三种场景：正常回答、延迟/逐步渲染、错误 QA。

用法：
    python3 tools/consumer-status/replay_drive.py

退出码：0 三种场景都符合预期 / 1 有场景不符
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).parent
ORDER = "SYNTH-ORDER-0001"
OTHER_ORDER = "SYNTH-ORDER-0002"
QA = "qa_" + "a" * 32

ANSWER = (
    f"Order {ORDER}: finished (status code 1).\n"
    "Source: cpms.chargingInfo; observed at (UTC): 2026-10-23T03:21:45+00:00."
)

#: 与 41 现役产物 `aiPackage-pages-chat-chat.d86b6f8a.js` 同构的最小组件。
CHAT_PAGE = """<!doctype html><html><body>
<div id="app">
  <div class="header"><span class="ai-name">AI客服1</span>
    <button class="mode-btn" onclick="document.getElementById('input').focus()">mode</button>
  </div>
  <div class="list" id="list">
    <div class="user-bubble"><span class="bubble-text">check my latest order status</span></div>
  </div>
  <input id="input" />
  <button class="send-btn" onclick="send()">send</button>
</div>
<script>
// 助手消息**只有一个节点**，文本就地增长 —— 真实流式渲染就是这样，
// 不是每来一段就新插一条。后者会让"读到第一条非空就收工"永远读到最新那条，
// 于是测不出时序缺陷。
function assistantNode() {
  var span = document.querySelector('.ai-row .ai-msg .bubble-text');
  if (span) return span;
  var row = document.createElement('div'); row.className = 'ai-row';
  var msg = document.createElement('div'); msg.className = 'ai-msg';
  var bubble = document.createElement('div'); bubble.className = 'ai-bubble';
  span = document.createElement('span'); span.className = 'bubble-text';
  bubble.appendChild(span); msg.appendChild(bubble); row.appendChild(msg);
  document.getElementById('list').appendChild(row);
  return span;
}
function addAnswer(text) {
  assistantNode().textContent = text;
}
function send() {
  var q = document.getElementById('input').value;
  fetch('/v1/assistant/questions', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({question: q})
  }).then(r => r.json()).then(function(d) {
    window.__qa = d.qa_id;
    // 真实前端会轮询结果；回放同样走这条路，否则驱动里的"等到 completed"永远不成立。
    fetch('/v1/assistant/questions/' + d.qa_id, {headers: {'Accept': 'application/json'}})
      .then(function(r) { return r.json(); })
      .then(function(body) { window.__result = body; });
  });
}
</script></body></html>"""


def _load_probe():
    spec = importlib.util.spec_from_file_location("sp_drive", ROOT / "status_probe.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _PageHandler(SimpleHTTPRequestHandler):
    """托管合成页；`/` 返回内置的 CHAT_PAGE（`fetch('/v1/...')` 需要同源 origin）。"""

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = CHAT_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, *_args) -> None:
        return


def _serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class Replay:
    """离线 steps：拦截所有 `/v1/`，返回合成数据，未声明的一律 abort。"""

    def __init__(self, *, base: str, scenario: str) -> None:
        self.base = base
        self.scenario = scenario
        self.business_requests = 0
        self.blocked_requests = 0

    def steps(self, page, *, host: str, login: dict, question: str) -> None:
        page.route("**/*", self._route)
        page.goto(f"{self.base}/", wait_until="domcontentloaded")
        page.locator("#input").fill(question)
        page.locator(".send-btn").click()
        # 逐步渲染：把回答分两段插入，模拟 SSE/流式
        page.wait_for_function("() => window.__result", timeout=10000)
        if self.scenario in {"normal", "node"}:
            page.evaluate("(t) => addAnswer(t)", ANSWER)
        elif self.scenario == "progressive":
            # **异步**补全：先插一段不完整的，然后**立刻返回**，让 `_drive()` 的
            # 轮询与区域读取真的和渲染**重叠**。
            # 上一版两段都在 steps 返回前插完，`_drive()` 开始等的时候答案早已完整，
            # 于是这个场景根本没测到"逐步渲染" —— Codex 的 P2 指的就是这里。
            # 第二段要落到 **_drive 的轮询之后**才有意义：轮询 500ms 一轮，
            # 先到 completed 才去读区域。所以把补全推迟到 2500ms —— 那时
            # `_drive` 已经在读区域，读到的是不完整的第一段。
            page.evaluate(
                """(t) => {
                    addAnswer(t.slice(0, 20));
                    setTimeout(() => addAnswer(t), 1200);
                }""",
                ANSWER,
            )
        elif self.scenario == "failed":
            # 失败场景：助手区域始终没有回答，只有用户提问
            page.evaluate("(t) => addAnswer(t)", "")

    def _route(self, route) -> None:
        url = route.request.url
        if "/v1/" not in url:
            # 页面自身的静态请求放行；只有**未声明的业务接口**才 abort。
            route.continue_()
            return
        self.business_requests += 1
        if "/v1/assistant/questions" not in url:
            route.abort()
            return
        if route.request.method == "POST":
            status = "queued" if self.scenario == "node" else "completed"
            body = json.dumps({"type": "qa", "status": status, "qa_id": QA})
            route.fulfill(status=202, content_type="application/json", body=body)
            return
        status = "failed" if self.scenario == "failed" else "completed"
        payload = {
            "type": "qa",
            "status": status,
            "qa_id": QA,
            "result": {"blocks": [{"kind": "text", "text": ANSWER}]},
        }
        route.fulfill(
            status=200,
            content_type="application/json",
            headers={"cache-control": "no-store"},
            body=json.dumps(payload),
        )


def _judge(
    spec, *, events, payloads, region, in_user_bubble, order_no=ORDER, label="latest", execution="direct"
):
    return spec.judge(
        events=events,
        payloads=payloads,
        answer_text=spec.answer_from_events(events, spec._qa_id_of(events)),
        order_no=order_no,
        label=label,
        assistant_region_text=region,
        region_in_user_bubble=in_user_bubble,
        execution=execution,
    )


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    spec = _load_probe()
    results: dict[str, object] = {}
    total_business = 0
    total_blocked = 0
    ok = True

    for scenario in ("normal", "progressive", "failed", "node"):
        server, base = _serve()
        replay = Replay(base=base, scenario=scenario)
        try:
            events, payloads, page_text, region, in_user_bubble = spec._drive(
                port=1,  # 离线不用隧道；chromium 不解析真实主机
                login={},
                question="check my latest order status",
                timeout=8,
                steps=replay.steps,
                # 回放把区域等待压到 2.0s，第二段在 1200ms 到 ——
                # 二者真实重叠：**正确实现**会等到第二段，**读到第一段就收工**的
                # 实现会拿到半截，判据当场不过。
                region_timeout=2.0,
            )
        finally:
            server.shutdown()

        checks = _judge(
            spec,
            events=events,
            payloads=payloads,
            region=region,
            in_user_bubble=in_user_bubble,
            execution="node" if scenario == "node" else "direct",
        )
        total_business += replay.business_requests
        total_blocked += replay.blocked_requests

        passed = not all(checks.values()) if scenario == "failed" else all(checks.values())
        ok = ok and passed
        results[scenario] = {
            "passed": passed,
            "all_checks": all(checks.values()),
            "failing": sorted(k for k, v in checks.items() if not v),
            "region_chars": len(region),
            "business_requests": replay.business_requests,
            "blocked_requests": replay.blocked_requests,
        }

    out = {
        "scenarios": results,
        "production_business_requests": 0,
        "total_intercepted_business_requests": total_business,
        "total_blocked_non_v1_requests": total_blocked,
        "verdict": "pass" if ok else "fail",
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
