#!/usr/bin/env python3
"""APP-RUNTIME-01 续办：**原 App 聊天入口 → 真 Gateway → 真 Java 节点 → 原 GET**。

这一层要证的是一条完整的用户路径，而不是某一段：

    App  POST /v1/assistant/questions（原形状，不加字段）
      → 真 Gateway 进程（按租户选绑定、选单、授权、签发 run）
      → 派发到编排平台（替身，只负责起一个真节点进程）
      → **真 Java 节点**自己来领取、自己跑模型工具循环、自己发完成通知
      → 同一 run 的 succeeded 快照
      → App  GET /v1/assistant/questions/{qa_id}（原形状）拿到 blocks

| 环节 | 真 / 替 |
|---|---|
| 聊天入口、QA 生命周期、读取守卫、取消 | **真 Gateway 进程**（本仓 `create_gateway_app`） |
| 绑定选择、选单、授权、签发、`final_release` | **本仓真实代码** |
| 领取 / 整轮完成通知 | **真 Java 节点自己发出**（不是 Python 派发器代劳） |
| 节点装配、工具、整轮守卫 | **真 Java 节点**（编译自 `services/execution-node/src`） |
| 模型上游 | 本机合成 OpenAI-compatible 服务（允许） |
| CPMS | 本机合成（允许） |
| 编排平台 | 本机替身：收到派发**只起一个真节点进程**，同步等它结束 |
| 调用者身份 / 订单授权 / 选单数据 / 平台目录 | 既有 `gateway_launcher.py` 的固定身份与合成表 |

    M3NODE_DEPS=… M3NODE_JDK_IMAGE=… \
      uv run python tools/experiments/m2_close/m3node/verify_chat_node_entry.py --out report.json

退出码 0=PASS，1=有判据失败，2=未取证（前置缺失，**不跳过**）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

REPO = Path(__file__).resolve().parents[4]
M3NODE = Path(__file__).resolve().parent
TOKEN = "chat-node-real-token"
NODE_TOKEN = "chat-node-real-node-token"
OTHER_TOKEN = "chat-node-real-token-b"
TENANT = "TENANT-A"
OTHER_TENANT = "TENANT-B"
C_USER = "USER-A"
OTHER_C_USER = "USER-B"
#: 两个本人订单：`ORDER-1001` 是最新（created_time 更晚），`ORDER-1002` 更早。
#: 这两条号**必须含数字**：`_extract_order_no` 拒绝纯字母候选，正文里的订单号得能被认出来。
ORDER_LATEST = "ORDER-1001"
ORDER_OLDER = "ORDER-1002"
#: 另一个租户主体的本人订单。**必须与上面两条不同号**：`order_no` 是主键，
#: 复用同一个号会把它从 USER-A 手里换走（INSERT OR REPLACE），于是「最新一单」
#: 变成另一张单 —— 这条曾经让 CHAT-03 以一个错误的理由通过。
ORDER_OTHER = "ORDER-2001"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(
    url: str,
    method: str = "GET",
    token: str | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib_request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib_error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 连不上也要成为一条可观察结果
        return 0, f"{type(exc).__name__}: {exc}"


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:400]}


# --------------------------------------------------------------------------
# 合成模型上游：第一轮给工具调用，第二轮给最终文本
# --------------------------------------------------------------------------


class ModelHandler(BaseHTTPRequestHandler):
    state: dict[str, Any] = {}

    def log_message(self, *args: Any) -> None:  # noqa: D102 - 静音
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server 接口
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).state.setdefault("prompts", []).append(body.get("messages", []))
        type(self).state["calls"] = type(self).state.get("calls", 0) + 1
        type(self).state["last_messages"] = [
            str(message.get("role")) for message in body.get("messages") or []
        ]
        # 轮次按**请求内容**判：有 tool 消息就是第二轮。按全局序号判会让并发的
        # 第二个 run 的第一轮被当成第二轮，于是它拿不到工具调用。
        has_tool_result = any(message.get("role") == "tool" for message in body.get("messages") or [])
        call = 2 if has_tool_result else 1
        streaming = bool(body.get("stream"))
        mode = type(self).state.get("mode", "normal")
        if call >= 2 and mode == "round2-500":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not streaming:
            payload = {
                "id": "chatcmpl-x",
                "object": "chat.completion",
                "created": 0,
                "model": "synthetic",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "final"},
                        "finish_reason": "stop",
                    }
                ],
            }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if call == 1:
            chunk = self._tool_chunk()
        elif mode == "truncated":
            self.wfile.write(("data: " + self._text_chunk("partial", None) + "\n\n").encode())
            self.wfile.flush()
            self.close_connection = True
            return
        else:
            chunk = self._text_chunk("final-answer", "stop")
        self.wfile.write(("data: " + chunk + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _tool_chunk(self) -> str:
        return json.dumps(
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "synthetic",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "charging_status_snapshot", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        )

    @staticmethod
    def _text_chunk(text: str, finish: str | None) -> str:
        return json.dumps(
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "synthetic",
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": finish}
                ],
            }
        )


class CpmsHandler(BaseHTTPRequestHandler):
    calls: list[str] = []

    def log_message(self, *args: Any) -> None:  # noqa: D102
        pass

    def do_GET(self) -> None:  # noqa: N802
        order_no = self.path.rsplit("/", 1)[-1]
        type(self).calls.append(order_no)
        body = json.dumps(
            {
                "code": 0,
                "msg": "success",
                "data": {
                    "orderNo": order_no,
                    "status": "0",
                    "plateNo": "DO-NOT-LEAK",
                    "userId": "DO-NOT-LEAK",
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PlatformHandler(BaseHTTPRequestHandler):
    """编排平台替身：收到派发**只**起一个真 Java 节点进程（领取与完成由节点自己发）。"""

    state: dict[str, Any] = {}

    def log_message(self, *args: Any) -> None:  # noqa: D102
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        sid = str(payload.get("sid") or "")
        state = self.state
        state["dispatched"].append(sid)
        result: dict[str, Any] = {}

        def run_node() -> None:
            inner = (
                "java -cp '/classes:/deps/lib/*' m3node.NodeLinkAcceptance "
                f"--run-id={sid} --gateway={state['gateway']} --model={state['model']} "
                f"--scenario={state['scenario']} --node-token={NODE_TOKEN}"
            )
            if state.get("managed"):
                inner += " --managed-publication"
            elif state.get("manifest"):
                inner += f" --manifest=/case/{Path(state['manifest']).name}"
            cmd = [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network=host",
                "--cap-drop=ALL",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{state['classes']}:/classes:ro",
                "-v",
                f"{state['deps']}:/deps:ro",
                "-v",
                f"{state['case_dir']}:/case:ro",
                state["jdk"],
                "bash",
                "-c",
                inner,
            ]
            try:
                completed = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=state.get("timeout", 180)
                )
                result["rc"] = completed.returncode
                result["log"] = completed.stdout + completed.stderr
            except subprocess.TimeoutExpired as exc:
                result["rc"] = "timeout"
                result["log"] = str(exc)
            result["sid"] = sid
            state["node_results"].append(result)

        thread = threading.Thread(target=run_node, daemon=True)
        thread.start()
        state["threads"].append(thread)
        # 真实平台是**同步**接口：它等节点的流结束才回包。替身保持这个顺序。
        thread.join(timeout=state.get("timeout", 180) + 20)
        body = json.dumps({"status": 1, "data": {"content": "已派发"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _rows(database: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM agent_runs ORDER BY created_at")]
    finally:
        connection.close()


def _compile(classes: Path, deps: Path, jdk: str) -> tuple[int, str]:
    sources = " ".join(
        str(Path("/repo") / p.relative_to(REPO))
        for p in (REPO / "services/execution-node/src/main/java").rglob("*.java")
    )
    command = (
        f'javac -nowarn -proc:none -cp "/deps/lib/*" -d /out {sources} '
        f"/repo/tools/experiments/m2_close/m3node/NodeLinkAcceptance.java"
    )
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--cap-drop=ALL",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{REPO}:/repo:ro",
            "-v",
            f"{deps}:/deps:ro",
            "-v",
            f"{classes}:/out",
            jdk,
            "bash",
            "-c",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    return completed.returncode, completed.stdout + completed.stderr


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)


CLASSES = ""
DEPS = ""
JDK = ""


def _write_bindings(case_root: Path, *, tenancies: dict[str, str], manifest: Path, platform_url: str) -> Path:
    """每个租户一条登记，各自一个执行配置（节点、app 都不同）。"""
    entries = []
    for tenant, node_id in tenancies.items():
        raw = {
            "environment": "env-local",
            "snail_base_url": platform_url,
            "snail_app_id": f"aiops-{node_id}",
            "snail_app_token": "unused",
            "snail_agent_id": 1,
            "snail_open_id": "open-id",
            "expected_node_id": node_id,
            "nodes": [
                {
                    "node_id": node_id,
                    "environment": "env-local",
                    "token_sha256": hashlib.sha256(NODE_TOKEN.encode()).hexdigest(),
                }
            ],
        }
        config = case_root / f"config/agent-runs-{tenant}.json"
        config.write_text(json.dumps(raw))
        config.chmod(0o600)
        entries.append(
            {
                "environment": "env-local",
                "tenant_id": tenant,
                "entry": "consumer",
                "scenario": "consumer_own_order_status",
                "config_file": str(config),
                "manifest_file": str(manifest),
            }
        )
    path = case_root / "config/chat-bindings.json"
    path.write_text(json.dumps({"schema": "aiops-chat-execution/1", "bindings": entries}))
    path.chmod(0o600)
    return path


def _scenario(
    root: Path,
    label: str,
    *,
    mode: str,
    scenario: str,
    platform: ThreadingHTTPServer,
    model: ThreadingHTTPServer,
    cpms: ThreadingHTTPServer,
    tenancies: dict[str, str],
    question_a: str,
    question_b: str | None = None,
    second_c_user: str | None = None,
) -> dict[str, Any]:
    case_root = root / label
    (case_root / "config").mkdir(parents=True)
    (case_root / "data").mkdir(parents=True)
    settings = case_root / "config/production.env"
    settings.write_text("# synthetic\n")
    settings.chmod(0o600)

    model_url = f"http://127.0.0.1:{model.server_port}/v1"
    node_manifest = case_root / "node-manifest.json"
    manifest_path = case_root / "exec-manifest.json"
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--cap-drop=ALL",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{case_root}:/case",
            "-v",
            f"{CLASSES}:/classes:ro",
            "-v",
            f"{DEPS}:/deps:ro",
            JDK,
            "bash",
            "-c",
            "java -cp '/classes:/deps/lib/*' m3node.NodeLinkAcceptance "
            f"--write-manifest=/case/node-manifest.json --model={model_url}",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        return {"label": label, "error": "manifest write failed", "log": completed.stdout + completed.stderr}
    node_manifest.chmod(0o600)
    manifest_path.write_bytes(node_manifest.read_bytes())
    manifest_path.chmod(0o600)

    orders = case_root / "data/orders.db"
    connection = sqlite3.connect(orders)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS synthetic_orders "
        "(order_no TEXT PRIMARY KEY, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL, "
        "created_time TEXT NOT NULL, id TEXT NOT NULL)"
    )
    rows_to_insert = [
        (ORDER_LATEST, C_USER, TENANT, "2026-10-20 10:00:00", "2"),
        (ORDER_OLDER, C_USER, TENANT, "2026-10-18 10:00:00", "1"),
    ]
    if second_c_user:
        # 第二个主体：**另一个租户**的本人订单（用来证明未登记租户不进入派发）
        rows_to_insert.append((ORDER_OTHER, second_c_user, OTHER_TENANT, "2026-10-20 10:00:00", "2"))
    connection.executemany("INSERT OR REPLACE INTO synthetic_orders VALUES (?, ?, ?, ?, ?)", rows_to_insert)
    connection.commit()
    connection.close()

    bindings = _write_bindings(
        case_root,
        tenancies=tenancies,
        manifest=manifest_path,
        platform_url=f"http://127.0.0.1:{platform.server_port}",
    )
    port = _free_port()
    env = {
        **os.environ,
        "AIOPS_DATA_HOME": str(case_root / "data"),
        "AIOPS_CONFIG_HOME": str(case_root / "config"),
        "AIOPS_GATEWAY_DATA_HOME": str(case_root / "data"),
        "AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(settings),
        "AIOPS_GATEWAY_DATABASE_FILE": str(case_root / "data/gateway.db"),
        "AIOPS_GATEWAY_BIND": "127.0.0.1",
        "AIOPS_GATEWAY_PORT": str(port),
        "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": f"http://127.0.0.1:{cpms.server_port}/charging-pile",
        "AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED": "1",
        # 部署身份（#879）：每个真正的启动入口都必须回答"我在哪个环境"。
        "AIOPS_GATEWAY_ENVIRONMENT": "chat-node-entry",
        "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": "env-local",
        # 委派入口**不配**：本片只开聊天绑定，证明它自己能跑完回调。
        "AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE": "",
        "AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE": "",
        "AIOPS_GATEWAY_CHAT_EXECUTION_BINDINGS_FILE": str(bindings),
        "AIOPS_GATEWAY_PLATFORM_CONSUMER": "1",
        "AIOPS_M2_LOCAL_TOKEN": TOKEN,
        "AIOPS_M2_ORDERS_DB": str(orders),
        "AIOPS_M2_TENANT": TENANT,
        "AIOPS_M2_C_USER": C_USER,
        "AIOPS_M2_PLATFORM_CONSUMER": "1",
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
    }
    if second_c_user:
        env["AIOPS_M2_SECOND_TOKEN"] = OTHER_TOKEN
        env["AIOPS_M2_SECOND_C_USER"] = second_c_user
        env["AIOPS_M2_SECOND_TENANT"] = OTHER_TENANT
    base = f"http://127.0.0.1:{port}"

    PlatformHandler.state = {
        "gateway": base,
        "model": model_url,
        "scenario": scenario,
        "manifest": str(node_manifest),
        "dispatched": [],
        "node_results": [],
        "threads": [],
        "timeout": 180,
        "classes": CLASSES,
        "deps": DEPS,
        "jdk": JDK,
        "case_dir": str(case_root),
        "platform_url": f"http://127.0.0.1:{platform.server_port}",
    }
    ModelHandler.state = {"calls": 0, "mode": mode}
    CpmsHandler.calls = []

    process = subprocess.Popen(
        [sys.executable, str(REPO / "tools/experiments/m2_close/gateway_launcher.py")],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    result: dict[str, Any] = {"label": label, "mode": mode, "scenario": scenario}
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if http(f"{base}/health", timeout=2.0)[0] == 200:
                break
            time.sleep(0.3)
        else:
            # 起不来说明这一条根本没跑：把 Gateway 自己的输出带出来，而不是只报"没就绪"。
            _stop(process)
            result["error"] = "gateway did not become ready"
            result["gateway_output"] = (process.stdout.read() if process.stdout else "")[-4000:]
            return result

        # A：POST 立即返回 —— 这条同时是「App 不等节点」的判据。
        started = time.monotonic()
        first_status, first_text = http(
            f"{base}/v1/assistant/questions",
            method="POST",
            token=TOKEN,
            body={"question": question_a},
            headers={"X-Business-Entry": "consumer"},
            timeout=30.0,
        )
        result["post_elapsed_ms"] = int((time.monotonic() - started) * 1000)
        result["post_status"] = first_status
        first = _json(first_text) if first_status else {}
        result["post_body"] = first
        qa_a = first.get("qa_id") if isinstance(first, dict) else None
        result["qa_id"] = qa_a
        result["post_completed_before_node"] = isinstance(first, dict) and first.get("status") in {
            "queued",
            "running",
        }

        second = None
        qa_b = None
        if question_b:
            second_status, second_text = http(
                f"{base}/v1/assistant/questions",
                method="POST",
                token=OTHER_TOKEN if second_c_user else TOKEN,
                body={"question": question_b},
                headers={"X-Business-Entry": "consumer"},
                timeout=30.0,
            )
            second = _json(second_text) if second_status else {}
            result["second_post_status"] = second_status
            qa_b = second.get("qa_id") if isinstance(second, dict) else None
            result["second_qa_id"] = qa_b

        def poll(qa_id: str | None, token: str) -> dict[str, Any]:
            if not qa_id:
                return {}
            deadline = time.monotonic() + 180
            last: dict[str, Any] = {}
            while time.monotonic() < deadline:
                # 入口判据要求调用方**自己声明** consumer（缺头是拒绝，不是默认成本人），
                # 所以轮询也必须带同一个头 —— 少了它每次都 403，看起来像"节点没跑完"。
                status, text = http(
                    f"{base}/v1/assistant/questions/{qa_id}",
                    token=token,
                    headers={"X-Business-Entry": "consumer"},
                )
                if status == 200:
                    last = _json(text)
                    if last.get("status") in {"completed", "failed", "cancelled", "expired"}:
                        return last
                else:
                    last = {"status": f"http_{status}", "body": _json(text)}
                    break
                time.sleep(0.1)
            return last

        result["final"] = poll(qa_a, TOKEN)
        if qa_b:
            result["second_final"] = poll(qa_b, OTHER_TOKEN if second_c_user else TOKEN)
        for thread in list(PlatformHandler.state["threads"]):
            thread.join(timeout=200)
        rows = _rows(case_root / "data/gateway.db")
        result["rows"] = [
            {
                key: row.get(key)
                for key in (
                    "run_id",
                    "qa_id",
                    "state",
                    "reason",
                    "chat_binding",
                    "expected_node_id",
                    "actual_node_id",
                    "app_id",
                    "environment",
                    "expected_agent_version",
                    "observed_agent_version",
                    "exec_config_state",
                    "snapshot_json",
                )
            }
            for row in rows
        ]
        result["model_calls"] = ModelHandler.state.get("calls", 0)
        result["cpms_calls"] = list(CpmsHandler.calls)
        result["dispatched"] = list(PlatformHandler.state["dispatched"])
        result["nodes"] = list(PlatformHandler.state["node_results"])
        return result
    finally:
        _stop(process)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    deps = os.environ.get("M3NODE_DEPS", "")
    jdk = os.environ.get("M3NODE_JDK_IMAGE", "")
    if not deps or not Path(deps, "lib").is_dir() or not jdk:
        print("missing M3NODE_DEPS (with lib/) or M3NODE_JDK_IMAGE", file=sys.stderr)
        return 2

    global CLASSES, DEPS, JDK
    CLASSES, DEPS, JDK = "", deps, jdk

    root = Path(tempfile.mkdtemp(prefix="chat-node-entry-"))
    classes = root / "classes"
    classes.mkdir()
    rc, log = _compile(classes, Path(deps), jdk)
    if rc != 0:
        print("compile failed", file=sys.stderr)
        print(log[-4000:], file=sys.stderr)
        return 2
    CLASSES = str(classes)

    report: dict[str, Any] = {
        "kind": "chat_node_entry_e2e",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "synthetic_data": True,
        "real_company_services": False,
        "substituted": {
            "model_upstream": "loopback OpenAI-compatible synthetic server",
            "cpms": "loopback stub",
            "orchestration_platform": "loopback stub that starts one real Java node per dispatch",
            "caller_identity": "fixed local token (gateway_launcher)",
            "order_authorization": "synthetic_orders table",
            "order_selection_source": "synthetic_orders rows; range and ordering logic unchanged",
            "platform_directory": "consumer-only synthetic directory",
            "routing_classifier": "not exercised by the deterministic status branch",
        },
        "real": [
            "POST /v1/assistant/questions → QA lifecycle → bound run → dispatch → GET questions/{qa_id}",
            "binding selection by tenant, order selection, authorization, final release",
            "node assembly, ControlledChargingStatusTool, LoopTerminationGuard, ReportingChatSessionRuntime",
            "claim and loop-completion notifications issued by the node itself",
        ],
        "checks": [],
        "scenarios": {},
    }

    def check(name: str, ok: bool, detail: Any) -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    cpms = _serve(CpmsHandler)
    model = _serve(ModelHandler)
    platform = _serve(PlatformHandler)
    try:
        # CHAT-01 本人最新一单（真节点两轮模型工具循环）
        latest = _scenario(
            root,
            "latest",
            mode="normal",
            scenario="normal",
            platform=platform,
            model=model,
            cpms=cpms,
            tenancies={TENANT: "node-local"},
            question_a="check my latest order status",
        )
        report["scenarios"]["latest"] = latest
        final = latest.get("final") or {}
        row = (latest.get("rows") or [{}])[0]
        check(
            "CHAT-01 最新一单：POST 202 且**早于**节点完成",
            latest.get("post_status") == 202 and latest.get("post_completed_before_node") is True,
            {"status": latest.get("post_status"), "elapsed_ms": latest.get("post_elapsed_ms")},
        )
        check(
            "CHAT-01 最新一单：GET 到 completed，答案含该 run 快照的订单号",
            final.get("status") == "completed"
            and ORDER_LATEST in json.dumps(final.get("result") or {}, ensure_ascii=False),
            {"status": final.get("status"), "result": (final.get("result") or {}).get("blocks")},
        )
        check(
            "CHAT-01 最新一单：run 由真节点领取并整轮完成（succeeded + matched）",
            row.get("state") == "succeeded"
            and row.get("exec_config_state") == "matched"
            and row.get("actual_node_id") == "node-local",
            {k: row.get(k) for k in ("state", "exec_config_state", "actual_node_id")},
        )
        check(
            "CHAT-01 最新一单：QA 与 run 是存储关联",
            row.get("qa_id") == latest.get("qa_id"),
            {"qa_id": latest.get("qa_id"), "row_qa_id": row.get("qa_id")},
        )
        check(
            "CHAT-01 最新一单：业务只读一次",
            latest.get("cpms_calls") == [ORDER_LATEST],
            latest.get("cpms_calls"),
        )
        check(
            "CHAT-01 最新一单：派发一次", len(latest.get("dispatched") or []) == 1, latest.get("dispatched")
        )
        check(
            "CHAT-01 最新一单：模型两轮（工具调用 + 最终答案）",
            latest.get("model_calls") == 2,
            latest.get("model_calls"),
        )
        check(
            "CHAT-01 最新一单：节点报告完成",
            "STATUS=PASS" in str((latest.get("nodes") or [{}])[-1].get("log", "")),
            (latest.get("nodes") or [{}])[-1].get("rc"),
        )

        # CHAT-02 正文指定另一张本人订单：两条请求各自选单、各自派发
        two = _scenario(
            root,
            "two-orders",
            mode="normal",
            scenario="normal",
            platform=platform,
            model=model,
            cpms=cpms,
            tenancies={TENANT: "node-local"},
            question_a="check my latest order status",
            question_b=f"check order {ORDER_OLDER} status",
        )
        report["scenarios"]["two-orders"] = two
        rows = two.get("rows") or []
        orders_seen = [
            json.loads(row["snapshot_json"])["order_no"] for row in rows if row.get("snapshot_json")
        ]
        check(
            "CHAT-02 指定订单：两条请求分别选到最新单与指定单",
            sorted(orders_seen) == sorted([ORDER_LATEST, ORDER_OLDER]),
            orders_seen,
        )
        check(
            "CHAT-02 指定订单：各自的业务读取按订单分开",
            sorted(two.get("cpms_calls") or []) == sorted([ORDER_LATEST, ORDER_OLDER]),
            two.get("cpms_calls"),
        )
        check(
            "CHAT-02 指定订单：两条 run 都是真节点完成的 succeeded",
            [row.get("state") for row in rows] == ["succeeded", "succeeded"],
            [row.get("state") for row in rows],
        )
        check(
            "CHAT-02 指定订单：两次派发各带自己的 run",
            sorted(two.get("dispatched") or []) == sorted(row.get("run_id") for row in rows),
            {"dispatched": two.get("dispatched"), "rows": [row.get("run_id") for row in rows]},
        )

        # CHAT-03 两个租户、两个节点：各自派到自己的节点
        tenants = _scenario(
            root,
            "two-tenants",
            mode="normal",
            scenario="normal",
            platform=platform,
            model=model,
            cpms=cpms,
            tenancies={TENANT: "node-local"},
            question_a="check my latest order status",
            question_b="check my latest order status",
            second_c_user=OTHER_C_USER,
        )
        report["scenarios"]["two-tenants"] = tenants
        a_rows = tenants.get("rows") or []
        check(
            "CHAT-03 未登记租户：第二个主体的请求**没有**产生 run（走原直读）",
            all(row.get("qa_id") != tenants.get("second_qa_id") for row in a_rows),
            {"second_qa_id": tenants.get("second_qa_id"), "run_qa_ids": [r.get("qa_id") for r in a_rows]},
        )
        second_final = tenants.get("second_final") or {}
        check(
            "CHAT-03 未登记租户：仍拿到**本人**直读答案（原分支未被打坏）",
            second_final.get("status") == "completed"
            and ORDER_OTHER in json.dumps(second_final.get("result") or {}, ensure_ascii=False),
            {
                "status": second_final.get("status"),
                "result": (second_final.get("result") or {}).get("blocks"),
            },
        )

        # CHAT-04 第二轮模型失败 ⇒ 失败，不交付正文，不回落直读
        failed = _scenario(
            root,
            "round2-500",
            mode="round2-500",
            scenario="round2-500",
            platform=platform,
            model=model,
            cpms=cpms,
            tenancies={TENANT: "node-local"},
            question_a="check my latest order status",
        )
        report["scenarios"]["round2-500"] = failed
        row = (failed.get("rows") or [{}])[0]
        final = failed.get("final") or {}
        check(
            "CHAT-04 第二轮失败：QA 终态 failed 且无正文",
            final.get("status") == "failed" and not (final.get("result") or {}).get("blocks"),
            {"status": final.get("status"), "result": final.get("result")},
        )
        check(
            "CHAT-04 第二轮失败：run 绝不 succeeded",
            row.get("state") != "succeeded",
            {"state": row.get("state"), "reason": row.get("reason")},
        )

        # CHAT-05 末轮截断 ⇒ 同样不能是成功
        cut = _scenario(
            root,
            "truncated",
            mode="truncated",
            scenario="truncated",
            platform=platform,
            model=model,
            cpms=cpms,
            tenancies={TENANT: "node-local"},
            question_a="check my latest order status",
        )
        report["scenarios"]["truncated"] = cut
        row = (cut.get("rows") or [{}])[0]
        check(
            "CHAT-05 末轮截断：run 绝不 succeeded，QA 不是 completed",
            row.get("state") != "succeeded" and (cut.get("final") or {}).get("status") != "completed",
            {"state": row.get("state"), "qa": (cut.get("final") or {}).get("status")},
        )
        check(
            "CHAT-05 末轮截断：没有回落直读（业务只被读了领取那一次）",
            cut.get("cpms_calls") == [ORDER_LATEST],
            cut.get("cpms_calls"),
        )
    finally:
        for server in (cpms, model, platform):
            server.shutdown()
        shutil.rmtree(root, ignore_errors=True)

    failures = [item for item in report["checks"] if not item["ok"]]
    report["failures"] = len(failures)
    report["status"] = "PASS" if not failures else "FAIL"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
