#!/usr/bin/env python3
"""真实 H5 页面上跑一条本人订单状态查询，记录网络与页面证据。

判据（缺一不可，任一不满足就 FAIL，**不重试凑通过**）：

1. POST `/v1/assistant/questions` → 202、`type=qa`、**`status=completed`**
   （走 consumer-order-chat 是同步完成的；`queued`/`running` 说明落回了普通模型路径）
2. 请求体：`latest` 用例**不得**带 `order_no`；`specified` 用例正文必须含该单号
3. GET 必须命中**本次 POST 返回的那个 `qa_id`** —— 不允许回退到别的 GET
4. 该 GET → 200 JSON、带 `no-store`
5. **助手回答区域**（不是整页文本）出现订单号、状态码语义、`cpms.chargingInfo`、观察时间

第 5 条是这一版修的：第一版用整页 `inner_text()` 判订单号，而**用户自己刚发出去的那条提问
就含订单号**，于是"页面只有用户问题、助手没回答"也能通过。回答必须从回答块里取。

第 3 条也是修的：第一版 `next((...), get)` —— 匹配不到本次 qa_id 就**回退到任意一个 GET**，
于是"读到的是上一轮的 QA"也算通过。

退出码（三个 consumer-status 脚本统一）：
  0 通过 / 1 判据不过 / 2 前提缺失、无法判定
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

EXIT_PASS, EXIT_FAIL, EXIT_UNVERIFIED = 0, 1, 2

HOST = "ulink.h5.mall.qushiyun.com"
POST_PATH = "/v1/assistant/questions"
_QA_PATH = re.compile(r"^/v1/assistant/questions/(qa_[0-9a-f]{32})$")
_OBSERVED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+\+00:00")
#: 回落到普通模型路径时的措辞。它出现即说明确定性路径没走通。
_REFUSALS = ("can't look up", "cannot look up", "无法在对话中直接查询", "無法在對話中直接查詢")


def judge(
    *,
    events: list[dict],
    payloads: list[dict],
    answer_text: str,
    order_no: str,
    label: str,
    assistant_region_text: str,
) -> dict[str, bool]:
    """纯判据：给定抓到的网络事件与两段文本，返回逐条布尔。

    抽成纯函数是为了能被合成反例直接驱动 —— 真实的 H5 页面不适合近距离
    反复重放，但这些判据的形状必须逐条钉住。
    """
    posts = [e for e in events if e["method"] == "POST" and e["path"] == POST_PATH]
    post = posts[-1] if posts else None
    body = (post or {}).get("body") or {}
    qa_id = body.get("qa_id")

    # 只认**本次 POST 的 qa_id**：匹配不到就是没证据，不回退到别的 GET。
    final = None
    if qa_id:
        for event in events:
            if event["method"] != "GET":
                continue
            match = _QA_PATH.match(event["path"])
            if match and match.group(1) == qa_id:
                final = event
    final_body = (final or {}).get("body") or {}
    blocks = ((final_body.get("result") or {}).get("blocks")) or []
    block_text = "\n".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))

    req_body = payloads[-1]["body"] if payloads else None
    question = str((req_body or {}).get("question", ""))

    return {
        "exactly_one_post": len(posts) == 1,
        "request_has_no_order_no": (
            not (isinstance(req_body, dict) and req_body.get("order_no"))
            if label == "latest"
            else order_no in question
        ),
        "post_202": (post or {}).get("http") == 202,
        "post_type_qa": body.get("type") == "qa",
        "post_completed": body.get("status") == "completed",
        "get_matches_own_qa_id": final is not None,
        "get_200_json": (final or {}).get("http") == 200 and not (final or {}).get("not_json"),
        "no_store": "no-store" in str((final or {}).get("cache_control") or ""),
        "all_v1_json": all(
            str(e.get("content_type") or "").startswith("application/json")
            for e in events
            if "/v1/" in e["path"]
        ),
        # ---- 回答必须来自回答块与助手区域，不是整页、更不是用户提问 ----
        "answer_has_order": order_no in block_text if order_no else False,
        "answer_has_source": "cpms.chargingInfo" in block_text,
        "answer_has_observed_at": bool(_OBSERVED_AT.search(block_text)),
        "assistant_region_has_answer": bool(assistant_region_text.strip())
        and (order_no in assistant_region_text if order_no else False)
        and "cpms.chargingInfo" in assistant_region_text,
        "not_model_refusal": not any(r in block_text.lower() for r in _REFUSALS),
        "answer_matches_qa_blocks": bool(answer_text.strip())
        and answer_text.strip() in (assistant_region_text or ""),
    }


def _wait_port(port: int, *, timeout: float = 10.0) -> bool:
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _port_owned_by(port: int, pid: int) -> bool:
    """隧道端口必须是我起的那个进程在听，不能是别人的。"""
    try:
        out = subprocess.run(
            ["ss", "-ltnpH", f"sport = :{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return f"pid={pid}" in out


def _host_alias(target: str) -> str:
    """从 `dev-host show` 取 ssh 别名 —— 主机身份的唯一来源，不在这里另写一份。"""
    out = subprocess.run(["dev-host", "show", target], capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise SystemExit(f"dev-host show {target} 失败：{out.stderr.strip()[:200]}")
    for line in out.stdout.splitlines():
        if line.startswith("ssh alias"):
            return line.split(None, 2)[-1].strip()
    raise SystemExit(f"dev-host show {target} 没有给出 ssh alias")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("order_no")
    parser.add_argument("label", choices=("latest", "specified"))
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("AIOPS_STATE_DIR", ""),
        help="私有原件目录（库外）。必须含 login-response.json；不指定则不写任何文件。",
    )
    parser.add_argument("--dev-host-target", default="41")
    parser.add_argument("--local-port", type=int, default=0)
    parser.add_argument("--evidence-timeout", type=float, default=30.0)
    args = parser.parse_args()

    state_dir = pathlib.Path(args.state_dir) if args.state_dir else None
    login_file = (state_dir / "login-response.json") if state_dir else None
    if not login_file or not login_file.exists():
        print(
            "缺少登录态：请用 --state-dir 指向库外私有目录，且其中含 login-response.json",
            file=sys.stderr,
        )
        return EXIT_UNVERIFIED
    login = json.loads(login_file.read_text())["data"]

    import socket

    port = args.local_port
    if not port:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

    question = (
        "check my latest order status" if args.label == "latest" else f"check order {args.order_no} status"
    )

    try:
        alias = _host_alias(args.dev_host_target)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_UNVERIFIED

    # 启动前先确认端口没人用；起来后确认**是这条 ssh 在听**。
    if _wait_port(port, timeout=0.2):
        print(f"本地端口 {port} 已被占用，拒绝复用（可能是别人的隧道）", file=sys.stderr)
        return EXIT_UNVERIFIED

    tunnel = subprocess.Popen(
        ["ssh", "-N", "-L", f"{port}:127.0.0.1:443", alias],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_port(port):
            print("隧道没有起来", file=sys.stderr)
            return EXIT_UNVERIFIED
        if not _port_owned_by(port, tunnel.pid):
            print(f"端口 {port} 不是本进程（{tunnel.pid}）在听，拒绝使用", file=sys.stderr)
            return EXIT_UNVERIFIED
        events, payloads, page_text, region_text = _drive(
            port=port,
            login=login,
            question=question,
            timeout=args.evidence_timeout,
        )
    finally:
        # 只清理自己启动的那个进程。
        tunnel.terminate()
        try:
            tunnel.wait(timeout=10)
        except subprocess.TimeoutExpired:
            tunnel.kill()

    answer_text = "\n".join(
        str(b.get("text") or "")
        for e in events
        for b in ((((e.get("body") or {}).get("result") or {}).get("blocks")) or [])
        if isinstance(b, dict) and e["method"] == "GET"
    )
    checks = judge(
        events=events,
        payloads=payloads,
        answer_text=answer_text,
        order_no=args.order_no,
        label=args.label,
        assistant_region_text=region_text,
    )

    out = {
        "label": args.label,
        "question": question,
        "order_no_prefix": args.order_no[:6] + "…",
        "checks": checks,
        "all_pass": all(checks.values()),
        "answer_excerpt": answer_text[:400],
        "page_excerpt_tail": page_text[-300:],
        "network": [
            {"m": e["method"], "p": e["path"], "http": e["http"], "cc": e["cache_control"]} for e in events
        ],
    }
    payload = json.dumps(out, ensure_ascii=False, indent=1)
    print(payload)
    if state_dir:
        (state_dir / f"result.{args.label}.private.json").write_text(payload + "\n")
    return EXIT_PASS if out["all_pass"] else EXIT_FAIL


#: 助手回答区域的选择器。回答块与用户提问在 DOM 里是不同容器；
#: 优先用列表节点里的**最后一条非用户消息**，取不到就整段视为缺失（不退回整页）。
ASSISTANT_SELECTORS = (
    ".msg-list .msg-item:not(.self) .content",
    ".chat-list .chat-item:not(.user) .content",
    ".message-list .message:not(.user) .content",
)


def _drive(*, port: int, login: dict, question: str, timeout: float):
    from playwright.sync_api import sync_playwright

    events: list[dict] = []
    payloads: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[f"--host-resolver-rules=MAP {HOST}:443 127.0.0.1:{port}"],
        )
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            locale="en-US",
        )
        page = ctx.new_page()

        def on_request(req):
            if POST_PATH in req.url and req.method == "POST":
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
        page.evaluate(
            "(d)=>{uni.setStorageSync('third_session',d.thirdSession);uni.setStorageSync('user_info',d)}",
            login,
        )
        # 干净聊天上下文：重新进 chat 页，不带上一轮的任何状态。
        page.goto(
            f"https://{HOST}/pages/user/user-center/dragindex",
            wait_until="domcontentloaded",
        )
        page.get_by_text("AI客服1", exact=True).click(timeout=20000)
        page.locator(".mode-btn").click(timeout=20000)
        page.locator("input").fill(question)
        page.locator(".send-btn").click()

        # 用条件等待而不是固定 sleep：等到本次 qa_id 的 GET 变成 completed。
        deadline = time.monotonic() + timeout
        qa_id = None
        while time.monotonic() < deadline:
            posts = [e for e in events if e["method"] == "POST" and e["path"] == POST_PATH]
            if posts:
                qa_id = ((posts[-1].get("body") or {}).get("qa_id")) or None
            if qa_id and any(
                e["method"] == "GET"
                and e["path"] == f"{POST_PATH}/{qa_id}"
                and ((e.get("body") or {}).get("status") == "completed")
                for e in events
            ):
                break
            page.wait_for_timeout(500)

        region_text = ""
        for selector in ASSISTANT_SELECTORS:
            node = page.locator(selector).last
            try:
                if node.count() and node.inner_text(timeout=1000).strip():
                    region_text = node.inner_text(timeout=2000)
                    break
            except Exception:  # noqa: BLE001
                continue
        page_text = page.locator("body").inner_text()
        browser.close()
    return events, payloads, page_text, region_text


if __name__ == "__main__":
    sys.exit(main())
