#!/usr/bin/env python3
"""真实 H5 页面上跑两条本人订单状态查询，记录网络与页面证据。

判据（缺一不可，任一不满足就 FAIL，不重试凑通过）：
  * POST /v1/assistant/questions → 202，type=qa，**status=completed**
    （走 consumer-order-chat 是同步完成的；queued/running 说明落回普通模型路径）
  * 请求体里**没有** order_no（第一条）——否则"最新"会被旧单覆盖
  * 随后 GET 同一 qa_id → 200 JSON，带 no-store
  * 页面正文里真的出现该订单号、状态、来源、观察时间
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).parent
HOST = "ulink.h5.mall.qushiyun.com"
PORT = 39735


def main() -> int:
    order_no = sys.argv[1] if len(sys.argv) > 1 else ""
    if not order_no:
        print("用法: status_probe.py <订单号> [latest|specified]", file=sys.stderr)
        return 2
    label = sys.argv[2] if len(sys.argv) > 2 else "latest"
    question = "check my latest order status" if label == "latest" else f"check order {order_no} status"

    # 私有原件不进仓库：会话与登录态由环境变量给出（或由 --state-dir 指到库外目录）。
    import os

    state_dir = pathlib.Path(os.environ.get("AIOPS_STATE_DIR", str(ROOT)))
    login = json.loads((state_dir / "login-response.json").read_text())["data"]

    tunnel = subprocess.Popen(
        ["ssh", "-N", "-L", f"{PORT}:127.0.0.1:443", "aiops-41"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    events: list[dict] = []
    payloads: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[f"--host-resolver-rules=MAP {HOST}:443 127.0.0.1:{PORT}"],
            )
            ctx = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
                locale="en-US",
            )
            page = ctx.new_page()

            def on_request(req):
                if "/v1/assistant/questions" in req.url and req.method == "POST":
                    try:
                        payloads.append({"url": req.url, "body": req.post_data_json})
                    except Exception:  # noqa: BLE001
                        payloads.append({"url": req.url, "body": None})

            def on_response(resp):
                if "/v1/" in resp.url and HOST in resp.url:
                    rec = {
                        "path": resp.url.split(HOST, 1)[-1],
                        "method": resp.request.method,
                        "http": resp.status,
                        "content_type": resp.headers.get("content-type"),
                        "cache_control": resp.headers.get("cache-control"),
                    }
                    try:
                        rec["body"] = resp.json()
                    except Exception:  # noqa: BLE001
                        rec["not_json"] = True
                    events.append(rec)

            page.on("request", on_request)
            page.on("response", on_response)

            page.goto(f"https://{HOST}/", wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(1200)
            page.evaluate(
                "(d)=>{uni.setStorageSync('third_session',d.thirdSession);uni.setStorageSync('user_info',d)}",
                login,
            )
            # 干净聊天上下文：新开 chat 页而不是从上次会话继续
            page.goto(
                f"https://{HOST}/pages/user/user-center/dragindex",
                wait_until="domcontentloaded",
            )
            page.get_by_text("AI客服1", exact=True).click(timeout=20000)
            page.locator(".mode-btn").click(timeout=20000)
            page.locator("input").fill(question)
            page.locator(".send-btn").click()
            page.wait_for_timeout(20000)
            text = page.locator("body").inner_text()
            page.screenshot(path=str(state_dir / f"page.{label}.private.png"))
            (state_dir / f"page.{label}.private.txt").write_text(text)
            browser.close()
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(timeout=10)
        except subprocess.TimeoutExpired:
            tunnel.kill()

    posts = [e for e in events if e["method"] == "POST" and e["path"] == "/v1/assistant/questions"]
    gets = [e for e in events if e["method"] == "GET" and e["path"].startswith("/v1/assistant/questions/")]
    post = posts[-1] if posts else None
    get = gets[-1] if gets else None
    body = (post or {}).get("body") or {}
    qa_id = body.get("qa_id")
    final_get = next((e for e in reversed(gets) if (e.get("body") or {}).get("qa_id") == qa_id), get)
    final_body = (final_get or {}).get("body") or {}
    blocks = ((final_body.get("result") or {}).get("blocks")) or []
    answer_text = "\n".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))

    req_body = payloads[-1]["body"] if payloads else None
    checks = {
        "request_has_no_order_no": not (isinstance(req_body, dict) and req_body.get("order_no"))
        if label == "latest"
        else order_no in str((req_body or {}).get("question", "")),
        "post_202": (post or {}).get("http") == 202,
        "post_type_qa": body.get("type") == "qa",
        "post_completed": body.get("status") == "completed",
        "get_200_json": (final_get or {}).get("http") == 200 and not (final_get or {}).get("not_json"),
        "no_store": "no-store" in str((final_get or {}).get("cache_control") or ""),
        "page_shows_order": order_no in text if order_no else False,
        "answer_mentions_order": order_no in answer_text if order_no else False,
        "answer_has_source": "cpms.chargingInfo" in answer_text,
        "answer_has_observed_at": bool(re.search(r"\d{4}-\d{2}-\d{2}T[\d:.]+\+00:00", answer_text)),
        "not_model_refusal": "can't look up" not in answer_text.lower(),
        "no_html": all(
            e.get("content_type", "").startswith("application/json") for e in events if "/v1/" in e["path"]
        ),
    }
    out = {
        "label": label,
        "question": question,
        "order_no_used": order_no,
        "qa_id": qa_id,
        "request_body_keys": sorted(req_body) if isinstance(req_body, dict) else req_body,
        "checks": checks,
        "all_pass": all(checks.values()),
        "answer_excerpt": answer_text[:400],
        "network": [
            {"m": e["method"], "p": e["path"], "http": e["http"], "cc": e["cache_control"]} for e in events
        ],
    }
    text_out = json.dumps(out, ensure_ascii=False, indent=1)
    (state_dir / f"result.{label}.private.json").write_text(text_out)
    print(text_out)
    return 0 if out["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
