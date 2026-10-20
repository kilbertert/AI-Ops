#!/usr/bin/env python3
"""M3-NODE-LINK-01 的**真实端到端**验收：真 Gateway HTTP + 真 Java 节点 + 合成上游。

这一层要证的正是本片的核心命题：**「工具领取成功」不等于「整轮模型完成」**，
所以两边都必须是真的 ——

| 环节 | 真/替 |
|---|---|
| 委派入口 `/v1/.../delegated`、领取入口、完成入口 | **真的 Gateway 进程**（`create_gateway_app`） |
| `AgentRunStore` 生命周期、清单加载、身份核对 | **本仓真实代码** |
| 节点装配、工具、整轮守卫与通知 | **真的 Java 节点**（编译自 `tools/m2_close/node/src`） |
| 模型上游 | 本机合成 OpenAI-compatible 服务（允许） |
| CPMS | 本机合成（允许） |
| 编排平台 | 本机替身，只负责「发起一次 HTTP 派发」；领取与完成由**节点自己**发出 |
| 调用者身份 / 订单授权 | 既有 `gateway_launcher.py` 的固定身份与 synthetic_orders |

    M3NODE_DEPS=… M3NODE_JDK_IMAGE=… uv run python tools/m2_close/m3node/verify_node_link.py --out report.json

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

REPO = Path(__file__).resolve().parents[3]
M3NODE = Path(__file__).resolve().parent
TOKEN = "node-link-real-token"
NODE_TOKEN = "node-link-real-node-token"
TENANT = "TENANT-A"
C_USER = "USER-A"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(url: str, method: str = "GET", token: str | None = None,
         body: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
         timeout: float = 30.0) -> tuple[int, str]:
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


# --------------------------------------------------------------------------
# 合成模型上游：按场景决定每轮返回什么
# --------------------------------------------------------------------------

class ModelHandler(BaseHTTPRequestHandler):
    state: dict[str, Any] = {}

    def log_message(self, *args: Any) -> None:  # noqa: D102 - 静音
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server 接口
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        body = json.loads(raw or b"{}")
        self.state["calls"] += 1
        call = self.state["calls"]
        mode = self.state["mode"]
        streaming = bool(body.get("stream"))
        if call >= 2 and mode == "round2-500":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not streaming:
            payload = {
                "id": "chatcmpl-x", "object": "chat.completion", "created": 0, "model": "synthetic",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "final"},
                             "finish_reason": "stop"}],
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
            # 第二段：不发 finish_reason、不发 [DONE]，直接关闭。
            self.wfile.write(("data: " + self._text_chunk("partial", None) + "\n\n").encode())
            self.wfile.flush()
            self.close_connection = True
            return
        elif mode == "slow":
            self.wfile.write(("data: " + self._text_chunk("first-", None) + "\n\n").encode())
            self.wfile.flush()
            time.sleep(1.2)
            self.wfile.write(("data: " + self._text_chunk("second", "stop") + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        else:
            chunk = self._text_chunk("final-answer", "stop")
        self.wfile.write(("data: " + chunk + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _tool_chunk(self) -> str:
        return json.dumps({
            "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "synthetic",
            "choices": [{"index": 0, "delta": {
                "role": "assistant",
                "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "charging_status_snapshot", "arguments": "{}"}}]},
                "finish_reason": "tool_calls"}],
        })

    @staticmethod
    def _text_chunk(text: str, finish: str | None) -> str:
        return json.dumps({
            "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "synthetic",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                         "finish_reason": finish}],
        })


class CpmsHandler(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *args: Any) -> None:  # noqa: D102
        pass

    def do_GET(self) -> None:  # noqa: N802
        type(self).calls += 1
        # 与既有 `verify_gateway_exec_config.py` 的 CPMS 桩同形：`code/data` 信封，
        # 字段名用真实 CPMS 的那一套（`status` 而不是 `statusCode`）——
        # 否则 Gateway 会按上游失败处理，验收就测不到本片的东西。
        order_no = self.path.rsplit("/", 1)[-1]
        body = json.dumps({
            "code": 0,
            "msg": "success",
            "data": {"orderNo": order_no, "status": "0", "plateNo": "DO-NOT-LEAK",
                     "userId": "DO-NOT-LEAK"},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PlatformHandler(BaseHTTPRequestHandler):
    """编排平台替身：收到派发**只**起一个 Java 节点进程（领取与完成由节点自己发）。"""

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
            if state.get("pre_claim_without_snapshot"):
                # 复现一次**崩溃窗口**：另一个执行者已 claim（running）但没有写出快照。
                # 于是本节点的领取会得到 409 AGENT_RUN_IN_PROGRESS，而记录**仍是非终态** ——
                # 这正是「受控 claim 失败必须让整轮失败」能与其他拒绝区分开的唯一形态：
                # 若工具把失败降级成 ERROR 文本，整轮会继续并最终报成功通知。
                time.sleep(0.2)
                database = state.get("database")
                if database is not None:
                    for _ in range(200):
                        rows = _rows(database)
                        if rows:
                            connection = sqlite3.connect(database)
                            try:
                                connection.execute(
                                    "UPDATE agent_runs SET state='running', claimed_at=? "
                                    "WHERE run_id=? AND state='issued'",
                                    (datetime.now(UTC).isoformat(), rows[0]["run_id"]),
                                )
                                connection.commit()
                            finally:
                                connection.close()
                            break
                        time.sleep(0.05)
            inner = (
                "java -cp '/classes:/deps/lib/*' m3node.NodeLinkAcceptance "
                f"--run-id={sid} --gateway={state['gateway']} --model={state['model']} "
                f"--scenario={state['scenario']} --node-token={NODE_TOKEN}"
            )
            if state.get("manifest"):
                inner += f" --manifest=/case/{Path(state['manifest']).name}"
            if state.get("second_run_id"):
                inner += f" --second-run-id={state['second_run_id']}"
            # `--network=host` 是必要的：节点必须打**宿主 loopback** 上的真 Gateway。
            # bridge 容器到不了宿主 127.0.0.1；这是本机合成验收的已知取舍，
            # 写进报告，不当作隔离通过。
            cmd = [
                "docker", "run", "--rm", "--pull=never", "--network=host", "--cap-drop=ALL",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "-v", f"{state['classes']}:/classes:ro",
                "-v", f"{state['deps']}:/deps:ro",
                "-v", f"{state['case_dir']}:/case:ro",
                state["jdk"], "bash", "-c", inner,
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
            state["node_results"].append(result)

        thread = threading.Thread(target=run_node, daemon=True)
        thread.start()
        state["threads"].append(thread)
        # 真实的平台派发是**同步**的：它要等节点的 gRPC 流结束才回包，而节点的
        # completionConsumer 只在 Gateway 确认整轮之后才触发。替身必须保持这个顺序，
        # 否则「派发返回时记录已 succeeded」这条既有语义会被替身自己的异步性破坏。
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
    # 容器里仓库挂在 /repo，所以源文件路径必须是**容器内**路径。
    sources = " ".join(
        str(Path("/repo") / p.relative_to(REPO))
        for p in (REPO / "tools/m2_close/node/src/main/java").rglob("*.java")
    )
    command = (
        f'javac -nowarn -proc:none -cp "/deps/lib/*" -d /out {sources} '
        f"/repo/tools/m2_close/m3node/NodeLinkAcceptance.java"
    )
    completed = subprocess.run(
        ["docker", "run", "--rm", "--pull=never", "--network=none", "--cap-drop=ALL",
         "--user", f"{os.getuid()}:{os.getgid()}",
         "-v", f"{REPO}:/repo:ro", "-v", f"{deps}:/deps:ro", "-v", f"{classes}:/out",
         jdk, "bash", "-c", command],
        capture_output=True, text=True, timeout=600,
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


def _scenario(root: Path, label: str, *, mode: str, scenario: str, manifest: bool,
              platform: ThreadingHTTPServer, model: ThreadingHTTPServer,
              cpms: ThreadingHTTPServer, manifest_mutation: str | None = None,
              probe: Any = None, pre_claim_without_snapshot: bool = False) -> dict[str, Any]:
    case_root = root / label
    (case_root / "config").mkdir(parents=True)
    (case_root / "data").mkdir(parents=True)
    settings = case_root / "config/production.env"
    settings.write_text("# synthetic\n")
    settings.chmod(0o600)

    model_url = f"http://127.0.0.1:{model.server_port}/v1"
    # 节点用自己写的清单（`node-manifest.json`）；Gateway 用 `exec-manifest.json`。
    # 两者平时内容相同；`manifest_mutation` 只改 Gateway 那一份，制造一次**真实的**
    # 身份不一致（节点仍在跑它自己那份受控配置）。
    node_manifest = case_root / "node-manifest.json"
    manifest_path = case_root / "exec-manifest.json"
    if manifest:
        completed = subprocess.run(
            ["docker", "run", "--rm", "--pull=never", "--network=none", "--cap-drop=ALL",
             "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{case_root}:/case",
             "-v", f"{MANIFEST_CLASSES}:/classes:ro", "-v", f"{MANIFEST_DEPS}:/deps:ro",
             MANIFEST_JDK, "bash", "-c",
             f"java -cp '/classes:/deps/lib/*' m3node.NodeLinkAcceptance "
             f"--write-manifest=/case/node-manifest.json --model={model_url}"],
            capture_output=True, text=True, timeout=120,
        )
        if completed.returncode != 0:
            return {
                "label": label,
                "error": "manifest write failed",
                "log": completed.stdout + completed.stderr,
            }
        node_manifest.chmod(0o600)
        manifest_path.write_bytes(node_manifest.read_bytes())
        manifest_path.chmod(0o600)
    if manifest_mutation is not None:
        # Gateway 侧的清单与节点侧**内容不同**（只改 name，仍是合法清单）：
        # 期望身份与节点回传的身份必然不等 ⇒ 领取 403，节点必须让整轮失败。
        # 这是「受控领取被拒」在真实链路上的形态，不靠打桩。
        payload = json.loads(manifest_path.read_text())
        payload["name"] = manifest_mutation
        manifest_path.write_text(json.dumps(payload))
        manifest_path.chmod(0o600)

    agent_runs = case_root / "config/agent-runs.json"
    agent_runs.write_text(json.dumps({
        "environment": "env-local",
        "snail_base_url": f"http://127.0.0.1:{platform.server_port}",
        "snail_app_id": "node-link-check",
        "snail_app_token": "unused",
        "snail_agent_id": 1,
        "snail_open_id": "open-id",
        "expected_node_id": "node-local",
        "nodes": [{"node_id": "node-local", "environment": "env-local",
                   "token_sha256": hashlib.sha256(NODE_TOKEN.encode()).hexdigest()}],
    }))
    agent_runs.chmod(0o600)

    orders = case_root / "data/orders.db"
    connection = sqlite3.connect(orders)
    connection.execute("CREATE TABLE IF NOT EXISTS synthetic_orders "
                       "(order_no TEXT PRIMARY KEY, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL)")
    connection.execute("INSERT OR REPLACE INTO synthetic_orders VALUES ('ORDER-A', ?, ?)",
                       (C_USER, TENANT))
    connection.commit()
    connection.close()

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
        "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": "env-local",
        "AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE": str(agent_runs),
        "AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE": str(manifest_path) if manifest else "",
        "AIOPS_M2_LOCAL_TOKEN": TOKEN,
        "AIOPS_M2_ORDERS_DB": str(orders),
        "AIOPS_M2_TENANT": TENANT,
        "AIOPS_M2_C_USER": C_USER,
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
    }
    base = f"http://127.0.0.1:{port}"
    PlatformHandler.state = {
        "gateway": base, "model": model_url, "scenario": scenario,
        "manifest": str(node_manifest) if manifest else None,
        "dispatched": [], "node_results": [], "threads": [], "timeout": 180,
        "classes": str(CLASSES), "deps": str(MANIFEST_DEPS), "jdk": MANIFEST_JDK,
        "case_dir": str(case_root),
        "database": case_root / "data/gateway.db",
        "pre_claim_without_snapshot": pre_claim_without_snapshot,
    }
    if probe is not None:
        probe["database"] = case_root / "data/gateway.db"
        probe["gateway"] = base
        PlatformHandler.state["probe"] = probe
    ModelHandler.state = {"calls": 0, "mode": mode}
    CpmsHandler.calls = 0

    process = subprocess.Popen(
        [sys.executable, str(REPO / "tools/m2_close/gateway_launcher.py")],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if http(f"{base}/health", timeout=2.0)[0] == 200:
                break
            time.sleep(0.3)
        else:
            return {"label": label, "error": "gateway did not become ready"}

        delegated = http(f"{base}/v1/orders/ORDER-A/charging-status/delegated", method="POST",
                         token=TOKEN, body={}, headers={"X-Business-Entry": "consumer"})
        for thread in list(PlatformHandler.state["threads"]):
            thread.join(timeout=200)
        rows = _rows(case_root / "data/gateway.db")
        return {
            "label": label,
            "delegated_status": delegated[0],
            "delegated_body": _json(delegated[1]),
            "model_calls": ModelHandler.state["calls"],
            "cpms_calls": CpmsHandler.calls,
            "node": (PlatformHandler.state["node_results"] or [{}])[-1],
            "rows": [{k: row.get(k) for k in (
                "run_id", "state", "reason", "expected_agent_version", "observed_agent_version",
                "exec_config_state", "snapshot_json", "completed_at")} for row in rows],
        }
    finally:
        _apply_probe_events(label, manifest_path, case_root, probe)
        _stop(process)


def _apply_probe_events(label: str, manifest_path: Path, case_root: Path,
                        probe: Any) -> None:
    """在节点跑完之后、进程停掉之前不支持的操作，一律放到这里（目前只有边界用例）。

    `revoked` / `expired` 需要「节点已取数、尚未确认」这个瞬间，而那一段在节点进程里。
    这里只在**节点已经退出**（无论成败）之后对库里那条记录做撤销/拨时钟 ——
    这样测到的是「完成通知在终态之后一定被拒」，不是被打桩出来的行为。
    """
    if probe is None:
        return
    kind = probe.get("after")
    database = probe.get("database")
    if not kind or database is None:
        return
    rows = _rows(database)
    if not rows:
        return
    run_id = str(rows[0]["run_id"])
    connection = sqlite3.connect(database)
    try:
        if kind == "revoke":
            connection.execute(
                "UPDATE agent_runs SET state='revoked', reason='revoked_by_caller', "
                "completed_at=? WHERE run_id=? AND state IN ('issued','running','succeeded')",
                (datetime.now(UTC).isoformat(), run_id),
            )
        elif kind == "expire":
            connection.execute(
                "UPDATE agent_runs SET expires_at='2000-01-01T00:00:00+00:00' WHERE run_id=?",
                (run_id,),
            )
        connection.commit()
        if probe.get("gateway") and kind in {"revoke", "expire"}:
            identity = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            status, text = http(
                f"{probe['gateway']}/v1/internal/charging-status/completions/{run_id}",
                method="POST", token=NODE_TOKEN,
                body={"status": "completed"},
                headers={"X-AI-Ops-Exec-Config": identity},
            )
            probe["completion_after"] = {"status": status, "body": _json(text)}
            probe["state_after"] = _rows(database)[0].get("state")
    finally:
        connection.close()


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:200]}


MANIFEST_CLASSES = ""
MANIFEST_DEPS = ""
MANIFEST_JDK = ""
#: 编译后的类目录，供平台替身挂给节点容器。与 ``MANIFEST_CLASSES`` 同一份。
CLASSES = ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    deps = os.environ.get("M3NODE_DEPS", "")
    jdk = os.environ.get("M3NODE_JDK_IMAGE", "")
    if not deps or not Path(deps, "lib").is_dir() or not jdk:
        print("missing M3NODE_DEPS (with lib/) or M3NODE_JDK_IMAGE", file=sys.stderr)
        return 2

    global MANIFEST_CLASSES, MANIFEST_DEPS, MANIFEST_JDK, CLASSES
    MANIFEST_CLASSES, MANIFEST_DEPS, MANIFEST_JDK = "", deps, jdk

    root = Path(tempfile.mkdtemp(prefix="m3-node-link-"))
    classes = root / "classes"
    classes.mkdir()
    rc, log = _compile(classes, Path(deps), jdk)
    if rc != 0:
        print("compile failed", file=sys.stderr)
        print(log[-4000:], file=sys.stderr)
        return 2
    MANIFEST_CLASSES = str(classes)
    CLASSES = str(classes)

    report: dict[str, Any] = {
        "kind": "m3_node_link_e2e",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "synthetic_data": True,
        "real_company_services": False,
        "substituted": {
            "model_upstream": "loopback OpenAI-compatible synthetic server",
            "cpms": "loopback stub",
            "orchestration_platform": "loopback stub that starts one real Java node per dispatch",
            "caller_identity": "fixed local token (gateway_launcher)",
            "order_authorization": "synthetic_orders table",
        },
        "real": [
            "delegated / claim / completion routes (real Gateway process)",
            "AgentRunStore lifecycle, manifest load, identity comparison",
            "node assembly, ControlledChargingStatusTool, LoopTerminationGuard, "
            "ReportingChatSessionRuntime",
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
        # LINK-01 正常两轮
        normal = _scenario(root, "normal", mode="normal", scenario="normal", manifest=True,
                           platform=platform, model=model, cpms=cpms)
        report["scenarios"]["normal"] = normal
        row = (normal.get("rows") or [{}])[0]
        check("LINK-01 正常两轮：一次业务", normal.get("cpms_calls") == 1, normal.get("cpms_calls"))
        check("LINK-01 正常两轮：两次模型", normal.get("model_calls") == 2, normal.get("model_calls"))
        check("LINK-01 正常两轮：succeeded", row.get("state") == "succeeded", row.get("state"))
        check("LINK-01 正常两轮：回执带 agent_version 且 gap 为空",
              ((normal.get("delegated_body") or {}) if isinstance(normal.get("delegated_body"), dict) else {})
              .get("agent_version") == row.get("expected_agent_version")
              and ((normal.get("delegated_body") or {}).get("agent_version_gap") is None),
              normal.get("delegated_body"))
        check("LINK-01 正常两轮：节点报告完成",
              "STATUS=PASS" in str((normal.get("node") or {}).get("log")), normal.get("node"))

        # LINK-02 第二轮 500
        bad = _scenario(root, "round2-500", mode="round2-500", scenario="round2-500",
                        manifest=True, platform=platform, model=model, cpms=cpms)
        report["scenarios"]["round2-500"] = bad
        row = (bad.get("rows") or [{}])[0]
        check("LINK-02 第二轮失败：记录 failed", row.get("state") == "failed", row.get("state"))
        check("LINK-02 第二轮失败：无成功回执",
              bad.get("delegated_status") != 200, bad.get("delegated_status"))

        # LINK-03 末轮截断
        cut = _scenario(root, "truncated", mode="truncated", scenario="truncated",
                        manifest=True, platform=platform, model=model, cpms=cpms)
        report["scenarios"]["truncated"] = cut
        row = (cut.get("rows") or [{}])[0]
        check("LINK-03 截断：绝无 succeeded", row.get("state") != "succeeded", row.get("state"))

        # LINK-04 受控领取被拒（manifest 用错误 endpoint 使 model 配置漂移并触发节点拒绝）
        # LINK-04 受控 claim 被拒：另一个执行者已 claim（running）但没写快照 ⇒ 422/409，
        # 记录仍非终态。这是唯一能区分「抛异常中止整轮」与「ERROR 文本继续跑」的形态。
        busy_probe: dict[str, Any] = {}
        busy = _scenario(root, "claim-busy", mode="normal", scenario="normal", manifest=True,
                         platform=platform, model=model, cpms=cpms, probe=busy_probe,
                         pre_claim_without_snapshot=True)
        report["scenarios"]["claim-busy"] = busy
        row = (busy.get("rows") or [{}])[0]
        check("LINK-04 受控 claim 被拒：记录绝不 succeeded",
              row.get("state") != "succeeded", row.get("state"))
        check("LINK-04 受控 claim 被拒：整轮不再往下跑（模型只调 1 次）",
              busy.get("model_calls") == 1, busy.get("model_calls"))
        check("LINK-04 受控 claim 被拒：派发侧不返回成功",
              busy.get("delegated_status") != 200, busy.get("delegated_status"))

        # 身份不一致（Gateway 侧清单与节点侧不同）——另一条独立的拒绝路径。
        refused = _scenario(root, "claim-refused", mode="normal", scenario="normal",
                            manifest=True, platform=platform, model=model, cpms=cpms,
                            manifest_mutation="gateway-side-different-manifest")
        report["scenarios"]["claim-refused"] = refused
        row = (refused.get("rows") or [{}])[0]
        check("LINK-04 身份不一致（受控领取被拒）：记录落终态且非 succeeded",
              row.get("state") == "failed" and row.get("reason") == "exec_config_drift",
              {"state": row.get("state"), "reason": row.get("reason")})
        check("LINK-04 领取被拒：节点让整轮失败（不回成功）",
              "completion=false" in str((refused.get("node") or {}).get("log")),
              (refused.get("node") or {}).get("rc"))
        check("LINK-04 领取被拒：派发侧不返回成功",
              refused.get("delegated_status") != 200, refused.get("delegated_status"))

        # LINK-05 取消：框架不给 cancel 回调 ⇒ 记录停在 running（已取数），由 TTL 收敛。
        cancel_probe: dict[str, Any] = {}
        cancel = _scenario(root, "cancel", mode="slow", scenario="cancel", manifest=True,
                           platform=platform, model=model, cpms=cpms, probe=cancel_probe)
        report["scenarios"]["cancel"] = cancel
        row = (cancel.get("rows") or [{}])[0]
        check("LINK-05 取消：业务已读但记录不 succeeded",
              row.get("state") == "running" and bool(row.get("snapshot_json")),
              {"state": row.get("state"), "snapshot": bool(row.get("snapshot_json"))})
        check("LINK-05 取消：派发侧不返回成功",
              cancel.get("delegated_status") != 200, cancel.get("delegated_status"))

        # LINK-07 领取重试：业务仍只读一次
        retry = _scenario(root, "retry", mode="normal", scenario="retry", manifest=True,
                          platform=platform, model=model, cpms=cpms)
        report["scenarios"]["retry"] = retry
        row = (retry.get("rows") or [{}])[0]
        check("LINK-07 重试：业务只读一次", retry.get("cpms_calls") == 1, retry.get("cpms_calls"))
        check("LINK-07 重试：最终 succeeded", row.get("state") == "succeeded", row.get("state"))
        check("LINK-07 重试：节点两次都拿到结果",
              "PASS retry:second-completed" in str((retry.get("node") or {}).get("log")),
              (retry.get("node") or {}).get("rc"))

        # LINK-09 撤销边界：节点跑完后撤销，再发完成通知必须被拒、不复活。
        revoke_probe: dict[str, Any] = {"after": "revoke"}
        revoked = _scenario(root, "revoked", mode="normal", scenario="normal", manifest=True,
                            platform=platform, model=model, cpms=cpms, probe=revoke_probe)
        report["scenarios"]["revoked"] = revoked
        after = revoke_probe.get("completion_after") or {}
        check("LINK-09 撤销后完成通知被拒", after.get("status") == 403, after)
        check("LINK-09 撤销后记录仍是 revoked", revoke_probe.get("state_after") == "revoked",
              revoke_probe.get("state_after"))

        # LINK-06 两个 run 各自归属：分两次派发，各自的快照与身份互不串。
        first = _scenario(root, "two-runs-a", mode="normal", scenario="normal", manifest=True,
                          platform=platform, model=model, cpms=cpms)
        second = _scenario(root, "two-runs-b", mode="normal", scenario="normal", manifest=True,
                           platform=platform, model=model, cpms=cpms)
        report["scenarios"]["two-runs"] = {"a": first, "b": second}
        rows_a = first.get("rows") or [{}]
        rows_b = second.get("rows") or [{}]
        check("LINK-06 两个 run：都 succeeded",
              rows_a[0].get("state") == "succeeded" and rows_b[0].get("state") == "succeeded",
              [rows_a[0].get("state"), rows_b[0].get("state")])
        check("LINK-06 两个 run：run_id 不同且各自快照独立",
              rows_a[0].get("run_id") != rows_b[0].get("run_id")
              and rows_a[0].get("snapshot_json") and rows_b[0].get("snapshot_json"),
              [rows_a[0].get("run_id"), rows_b[0].get("run_id")])
        report["notes"] = {
            "two_run_interleaving": (
                "真正的**交错**归属由节点侧既有 cross-run 场景证明"
                "（tools/m2_close/m3node/NodeGuardAcceptance 的 cross-run，run.sh 覆盖）；"
                "本层证明的是每个 run 各自的通知只作用于自己的记录。"
            ),
            "ttl_convergence": (
                "取消后的 running→expired 由 Gateway 侧既有 TTL 语义覆盖"
                "（tests/test_m3_node_link_state.py::test_complete_loop_refuses_after_expiry "
                "与 completion 的到期边界用例）；本层只证「取消不会被报成成功」。"
            ),
        }

        # LINK-10 未启用清单：行为与今天逐字一致
        legacy = _scenario(root, "legacy", mode="normal", scenario="normal", manifest=False,
                           platform=platform, model=model, cpms=cpms)
        report["scenarios"]["legacy"] = legacy
        row = (legacy.get("rows") or [{}])[0]
        check("LINK-10 未启用：领取即 succeeded（旧语义）", row.get("state") == "succeeded",
              row.get("state"))
        check("LINK-10 未启用：无期望身份", row.get("expected_agent_version") is None,
              row.get("expected_agent_version"))
        check("LINK-10 未启用：回执键集合不变",
              set((legacy.get("delegated_body") or {}).keys())
              == {"run_id", "state", "reason", "operation", "scope_fingerprint",
                  "expected_environment", "expected_app_id", "expected_agent_id",
                  "expected_node_id", "actual_node_id", "node_build", "gateway_version",
                  "agent_version", "agent_version_gap", "result_sha256", "created_at",
                  "expires_at", "claimed_at", "completed_at"},
              sorted((legacy.get("delegated_body") or {}).keys()))
    finally:
        for server in (cpms, model, platform):
            server.shutdown()
        shutil.rmtree(root, ignore_errors=True)

    failures = [c for c in report["checks"] if not c["ok"]]
    report["failures"] = len(failures)
    report["status"] = "PASS" if not failures else "FAIL"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
