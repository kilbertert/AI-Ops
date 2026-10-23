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
import contextlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from pathlib import Path

EXIT_PASS, EXIT_FAIL, EXIT_UNVERIFIED = 0, 1, 2


def _private_output():
    """同目录的 `private_output.py` 按文件加载 —— 这些脚本不是包。"""
    import importlib.util

    module = sys.modules.get("consumer_status_private_output")
    if module is not None:
        return module
    path = pathlib.Path(__file__).with_name("private_output.py")
    spec = importlib.util.spec_from_file_location("consumer_status_private_output", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

HOST = "ulink.h5.mall.qushiyun.com"
POST_PATH = "/v1/assistant/questions"
_QA_PATH = re.compile(r"^/v1/assistant/questions/(qa_[0-9a-f]{32})$")
_OBSERVED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+\+00:00")
#: 回落到普通模型路径时的措辞。它出现即说明确定性路径没走通。
_REFUSALS = ("can't look up", "cannot look up", "无法在对话中直接查询", "無法在對話中直接查詢")


#: 状态语义的词表：回答里必须出现其中一个，否则「只有订单号+来源+时间」也算过。
#: **不能用 `charging` 这种裸词** —— 它会命中来源标记 `cpms.chargingInfo`，
#: 于是"没有状态"的回答也能过（本仓自己踩过：判据的空转就是这么来的）。
#: 要匹配充电中，用带上下文的形式 `status … charging` 或中文「充电中」。
_STATUS_WORDS = (
    "finished",
    "stopped",
    "unknown",
    "status code",
    "status_role",
    "状态码",
    "狀態碼",
    "结束",
    "充電中",
    "充电中",
    "停止",
    "未知",
)


def _qa_id_of(events: list[dict]) -> str | None:
    for event in events:
        if event["method"] == "POST" and event["path"] == POST_PATH:
            return (event.get("body") or {}).get("qa_id")
    return None


def answer_from_events(events: list[dict], qa_id: str | None) -> str:
    """**只**取「路径与响应体都属于本次 qa_id」的那条 GET 的 blocks。

    重复轮询同一个 QA 时，**最后一条**匹配的即可 —— 不把前几轮的 blocks 拼起来，
    否则同一段答案会出现多次，`answer == region` 之类的判据会误判失败。
    """
    if not qa_id:
        return ""
    chosen = None
    for event in events:
        if event["method"] != "GET":
            continue
        match = _QA_PATH.match(event["path"])
        if not match or match.group(1) != qa_id:
            continue
        if (event.get("body") or {}).get("qa_id") != qa_id:
            continue
        chosen = event
    if chosen is None:
        return ""
    blocks = ((chosen.get("body") or {}).get("result") or {}).get("blocks") or []
    return "\n".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))


def judge(
    *,
    events: list[dict],
    payloads: list[dict],
    answer_text: str,
    order_no: str,
    label: str,
    assistant_region_text: str,
    region_in_user_bubble: bool = False,
) -> dict[str, bool]:
    """纯判据：给定抓到的网络事件与两段文本，返回逐条布尔。"""
    posts = [e for e in events if e["method"] == "POST" and e["path"] == POST_PATH]
    post = posts[-1] if posts else None
    body = (post or {}).get("body") or {}
    qa_id = body.get("qa_id")

    # 本次 qa_id 的 GET：**路径与响应体都要对上**，否则读到别人的结果也算过。
    final = None
    if qa_id:
        for event in events:
            if event["method"] != "GET":
                continue
            match = _QA_PATH.match(event["path"])
            if not match or match.group(1) != qa_id:
                continue
            if (event.get("body") or {}).get("qa_id") != qa_id:
                continue  # 路径对、body 是另一条 —— 不接受
            final = event
    final_body = (final or {}).get("body") or {}
    final_status = str(final_body.get("status") or "")
    blocks = ((final_body.get("result") or {}).get("blocks")) or []
    block_text = "\n".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))

    req_body = payloads[-1]["body"] if payloads else None
    question = str((req_body or {}).get("question", ""))

    return {
        "exactly_one_post": len(posts) == 1,
        # 没有请求体就等于不知道这次问的什么 —— 不能通过。
        "request_captured": isinstance(req_body, dict) and bool(question),
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
        # 响应体的完成状态也要对：`failed` 但带旧 blocks 不是成功。
        "get_completed": final_status == "completed",
        "no_store": "no-store" in str((final or {}).get("cache_control") or ""),
        "all_v1_json": all(
            str(e.get("content_type") or "").startswith("application/json")
            for e in events
            if "/v1/" in e["path"]
        ),
        # ---- 回答必须来自回答块与助手区域，不是整页、更不是用户提问 ----
        "answer_has_order": order_no in block_text if order_no else False,
        "answer_has_status": any(w in block_text.lower() for w in _STATUS_WORDS),
        "answer_has_source": "cpms.chargingInfo" in block_text,
        "answer_has_observed_at": bool(_OBSERVED_AT.search(block_text)),
        "assistant_region_has_answer": _region_has_answer(assistant_region_text, order_no),
        "not_model_refusal": not any(r in block_text.lower() for r in _REFUSALS),
        # 区域里必须**真的包含**这段回答。空区域 ⇒ 不通过。
        # 比之前先归一化空白：浏览器 `inner_text()` 会把换行/连续空格折叠，
        # 直接子串比较会把"渲染对了"判成不一致。
        "answer_matches_qa_blocks": bool(answer_text.strip())
        and bool((assistant_region_text or "").strip())
        and _normalized(answer_text) in _normalized(assistant_region_text or ""),
        # 区域落在用户气泡里 ⇒ 取到的是提问，不是回答。
        "region_is_not_user_bubble": not region_in_user_bubble,
    }


def _normalized(text: str) -> str:
    """把空白折叠成单个空格 —— 浏览器渲染出的文本与 API 文本只差空白。"""
    return " ".join(text.split())


def _region_has_answer(region: str, order_no: str) -> bool:
    """助手区域必须同时展示：订单号、状态、来源、观察时间。

    「只有订单号+来源+时间」不算 —— 那正是判据要拦的不完整回答。
    """
    if not region.strip():
        return False
    lowered = region.lower()
    return (
        (order_no in region if order_no else False)
        and any(w in lowered for w in _STATUS_WORDS)
        and "cpms.chargingInfo" in region
        and bool(_OBSERVED_AT.search(region))
    )


def _write_private(path: Path, payload: dict) -> None:
    """私有证据：显式路径、0600、**必须在库外**。

    旧版只 chmod，不校验库外、且会跟随符号链接。现在走共享模块。
    """
    write_private_report = _private_output().write_private_report

    write_private_report(
        path,
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
        code_root=Path(__file__).parent,
    )


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


def tunnel_argv(*, target: str, port: int) -> list[str]:
    """**实际连接**也走 dev-host —— 不是"先校验、再自己 ssh"。

    `dev-host exec` 把 `--` 之后的参数原样交给它的 `ssh`（同一个 `do_remote`，
    同一套 `StrictHostKeyChecking` + 项目侧 known_hosts + 主机指纹校验）。
    所以在 `--` 后面传 `-N -L ...` 就得到一条**由 dev-host 建立并校验**的隧道。

    上一版是"先 `dev-host exec hostname` 校验，再另起普通 ssh" —— 那次校验
    说明不了**实际连接**连的是谁。现在两者是同一个连接。
    """
    return [
        "dev-host",
        "exec",
        target,
        "--allow-service-exec",
        "--timeout",
        "60",
        "--",
        # 显式绑定开发机回环；端口动态分配由调用方选好。
        "-N",
        "-L",
        f"127.0.0.1:{port}:127.0.0.1:443",
    ]


def _listening_pids(port: int) -> set[int]:
    """端口上有哪些 PID 在听。dev-host 包了一层 shell，**监听者不一定是 Popen 的 pid**。"""
    try:
        out = subprocess.run(
            ["ss", "-ltnpH", f"sport = :{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(m) for m in re.findall(r"pid=(\d+)", out)}


def _descendants(pid: int) -> set[int]:
    """本进程及其所有子进程 —— 清理时只碰这一棵子树。"""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {pid}
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        child, parent = int(parts[0]), int(parts[1])
        children.setdefault(parent, []).append(child)
    seen = {pid}
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def _terminate_tree(proc: subprocess.Popen) -> None:
    """只清理本次进程及其子进程。"""
    if proc.poll() is not None:
        return
    tree = _descendants(proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    # 包装进程可能留下孙进程：再按同一棵树收一次。
    for pid in sorted(tree - {proc.pid}, reverse=True):
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        with contextlib.suppress(OSError):
            os.kill(pid, 15)


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

    # 启动前先确认端口没人用。
    if _wait_port(port, timeout=0.2):
        print(f"本地端口 {port} 已被占用，拒绝复用（可能是别人的隧道）", file=sys.stderr)
        return EXIT_UNVERIFIED

    # **实际连接也走 dev-host**：同一条命令既建隧道又做主机身份校验。
    tunnel = subprocess.Popen(
        tunnel_argv(target=args.dev_host_target, port=port),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if not _wait_port(port):
            err = ""
            if tunnel.poll() is not None and tunnel.stderr is not None:
                err = (tunnel.stderr.read() or "").strip()[:300]
            print(f"隧道没有起来（退出码 {tunnel.poll()}）：{err}", file=sys.stderr)
            return EXIT_UNVERIFIED
        # dev-host 是 shell 包装，监听者可能是它的子进程 —— 判"属于这棵树"，
        # 不能假定 `ss` 里的 pid 等于 `Popen.pid`。
        listening = _listening_pids(port)
        if not listening or not (listening & _descendants(tunnel.pid)):
            print(
                f"端口 {port} 不是本次 dev-host 进程树在听（监听者 {sorted(listening)}）",
                file=sys.stderr,
            )
            return EXIT_UNVERIFIED
        events, payloads, page_text, region_text, region_in_user_bubble = _drive(
            port=port,
            login=login,
            question=question,
            timeout=args.evidence_timeout,
        )
    finally:
        _terminate_tree(tunnel)

    # 答案只来自**本次 qa_id 的那一条** GET（不拼接其他 GET）。
    answer_text = answer_from_events(events, _qa_id_of(events))
    checks = judge(
        events=events,
        payloads=payloads,
        answer_text=answer_text,
        order_no=args.order_no,
        label=args.label,
        assistant_region_text=region_text,
        region_in_user_bubble=region_in_user_bubble,
    )

    # **终端只打摘要**：问题原文、回答正文、页面文本都不上屏。
    summary = {
        "label": args.label,
        "order_no_prefix": args.order_no[:6] + "…",
        "checks": checks,
        "all_pass": all(checks.values()),
        "answer_chars": len(answer_text),
        "page_chars": len(page_text),
        "network": [
            {"m": e["method"], "p": e["path"], "http": e["http"], "cc": e["cache_control"]} for e in events
        ],
    }
    # 私有报告：完整问题/回答/页面文本 **只写显式指定的库外目录**。
    report = {
        **summary,
        "question": question,
        "order_no": args.order_no,
        "answer_excerpt": answer_text[:400],
        "page_excerpt_tail": page_text[-300:],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if state_dir:
        _write_private(state_dir / f"result.{args.label}.private.json", report)
    return EXIT_PASS if summary["all_pass"] else EXIT_FAIL


#: 助手回答区域的选择器 —— **对着现役产物核过**，不是猜的。
#:
#: 依据：41 上 `static/js/aiPackage-pages-chat-chat.<hash>.js`（当前
#: `d86b6f8a`）的渲染分支：
#:
#:     "user" === e.role
#:        ? <view class="user-bubble"><text class="bubble-text">{e.shown}</text></view>
#:        : <view class="ai-row"><view class="ai-msg">
#:              <text class="bubble-text">{e.shown}</text> …
#:
#: 即：**用户消息在 `.user-bubble`，助手消息在 `.ai-msg`**，两者不重叠。
#: 上一版写的 `.msg-list .msg-item` / `.chat-list .chat-item` 在这份产物里
#: **一个都不存在** —— 选择器命不中时整段视为缺失（不退回整页），所以不会
#: 误报通过，但也从来没真正命中过。
ASSISTANT_SELECTORS = (
    ".ai-msg .bubble-text",
    ".ai-msg",
)
#: 用户消息容器。区域若落在它里面，说明取到的是提问而不是回答 —— 直接判失败。
USER_MESSAGE_SELECTORS = (".user-bubble",)


#: 真实站点的导航与交互。**回放时换成离线版本**，走的是同一条驱动路径
#: （同一个 `_drive`、同一套请求/响应捕获、同一个轮询与区域读取）。
def _live_steps(page, *, host: str, login: dict, question: str) -> None:
    page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=25000)
    page.evaluate(
        "(d)=>{uni.setStorageSync('third_session',d.thirdSession);uni.setStorageSync('user_info',d)}",
        login,
    )
    # 干净聊天上下文：重新进 chat 页，不带上一轮的任何状态。
    page.goto(
        f"https://{host}/pages/user/user-center/dragindex",
        wait_until="domcontentloaded",
    )
    page.get_by_text("AI客服1", exact=True).click(timeout=20000)
    page.locator(".mode-btn").click(timeout=20000)
    page.locator("input").fill(question)
    page.locator(".send-btn").click()


def _drive(
    *,
    port: int,
    login: dict,
    question: str,
    timeout: float,
    steps=None,
    region_timeout: float = 10.0,
):
    """驱动浏览器。

    `steps` 是**注入点**：真实运行用 `_live_steps`（连真实站点），离线回放用
    一个本地页面 + 合成 API。除此之外的代码路径完全一样 —— 请求/响应捕获、
    按 `qa_id` 轮询到 `completed`、助手区域读取、用户气泡判定。
    """
    steps = steps or _live_steps
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
            if "/v1/" in resp.url:
                rec = {
                    "path": "/v1/" + resp.url.split("/v1/", 1)[-1],
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

        steps(page, host=HOST, login=login, question=question)

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

        # 助手区域：等**完整**回答，而不是"第一次出现非空文本"。
        # 逐步渲染时第一段是不完整的；读到它就收工会把半截当答案
        # （Codex 在 #1010 的 P2 指出的正是这个时序）。
        expected = answer_from_events(events, _qa_id_of(events))
        region_text = ""
        region_in_user_bubble = False
        region_deadline = time.monotonic() + region_timeout
        while time.monotonic() < region_deadline:
            for selector in ASSISTANT_SELECTORS:
                node = page.locator(selector).last
                try:
                    if not (node.count() and node.inner_text(timeout=800).strip()):
                        continue
                    candidate = node.inner_text(timeout=1500)
                except Exception:  # noqa: BLE001
                    continue
                # 有完整答案可比对时，必须**包含**它；否则继续等。
                if expected.strip() and _normalized(expected) not in _normalized(candidate):
                    region_text = candidate  # 记住当前进度，但不算完成
                    continue
                region_text = candidate
                break
            if region_text and not expected.strip():
                break
            if expected.strip() and _normalized(expected) in _normalized(region_text):
                break
            page.wait_for_timeout(300)

        # 命中的节点若在 `.user-bubble` 里面，说明取到的是用户提问 —— 判失败。
        for user_selector in USER_MESSAGE_SELECTORS:
            try:
                if page.locator(user_selector).filter(has_text=region_text).count() and region_text:
                    region_in_user_bubble = True
            except Exception:  # noqa: BLE001
                continue

        page_text = page.locator("body").inner_text()  # 仅供私有摘录，不参与判据
        browser.close()
    return events, payloads, page_text, region_text, region_in_user_bubble


if __name__ == "__main__":
    sys.exit(main())
