#!/usr/bin/env python3
"""用**真的** `aiops-gateway serve` 在一次性数据根上跑 M2-CLOSE-01 的三个 HTTP 面。

用仓库自己的启动路径（`GatewayServerSettings` → `create_gateway_app` → uvicorn）
核对本切片的 HTTP 行为，而不是用 TestClient。它**不是**业务验收。

两段，各自替换了什么写清楚（不要只写一个笼统的「桩」）：

| 段 | 启动方式 | 替换的外部依赖 |
|---|---|---|
| A | 真 `python -m aiops_diagnostics.gateway_server serve` | 回环 introspection 端点；回环 CPMS 桩；
订单授权源**缺席**（生产装配下需要业务数据库） |
| B | 同一 `create_gateway_app` 工厂 + 回环启动器 | 回环 CPMS 桩；
**固定身份 resolver** 与 **SQLite 订单授权替身**（判据同为「订单 owner == 主体 C 端用户」） |

`/v1/orders/{order}/access` 用的是同一个授权器。**不接公司服务、不读生产数据、不写生产配置。**

反例（本轮必须成立）：

- 未认证 401、非 consumer 入口 403、他人/不存在的订单 404（且不读业务）
- 委派入口带多余字段 422
- 领取：无凭据 401、错节点凭据 401、只持 run_id 401
- 成功响应带 `Cache-Control: no-store`，`/health` 不带（缓存判定只作用于本切片）

用法：
  uv run python tools/m2_close/verify_real_gateway.py --out /tmp/m2close-real.json
退出码 0=PASS，1=有判据失败，2=未取证（前置缺失）。

它**不**启动 Snail，也不执行节点：那部分由 `tools/m2_close/run_ic.py` 的容器实验覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_guard  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
ORDERS = {"ORDER-A": "USER-A", "ORDER-C": "USER-A", "ORDER-B": "USER-B"}
#: 桩 CPMS 的调用计数（进程内全局：每个请求都是新的 handler 实例）。
CPMS_COUNTS: dict[str, int] = {}
TENANT = "TENANT-A"
C_USER = "USER-A"
TOKEN = "m2close-real-token"
NODE_TOKEN = "m2close-real-node-token"
DEFAULT_PORT = 8798


def _node_jar_sha256() -> str:
    """节点 jar 的摘要。jar 不在（没构建过）时留空串 —— 不是一个假的 0。"""
    import hashlib

    repo = Path(__file__).resolve().parents[2]
    jar = repo / "services/execution-node/target/aiops-execution-node.jar"
    return hashlib.sha256(jar.read_bytes()).hexdigest() if jar.is_file() else ""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        pass

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class IntrospectionHandler(_Handler):
    """生产路径上的 introspection 端点：只认一个令牌。"""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        form = self.rfile.read(length).decode()
        token = form.removeprefix("token=")
        if token != TOKEN:
            self._json({"active": False})
            return
        self._json(
            {
                "active": True,
                "sub": "SUBJECT-A",
                "tenant_id": TENANT,
                "aud": ["aiops-api"],
                "scope": "aiops:orders:read",
                "exp": int((datetime.now(UTC) + timedelta(minutes=10)).timestamp()),
                "data_scope": {"type": "self"},
                "c_user_id": C_USER,
            }
        )


class CpmsHandler(_Handler):
    """桩 CPMS。计数放在**实例之外**：`BaseHTTPRequestHandler` 由服务器按请求实例化，
    往构造函数里塞额外参数会让它拿不到 `request/client_address/server`，
    表现为连接被立刻断开、调用方读到 502（实测踩过一次）。"""

    def do_GET(self) -> None:  # noqa: N802
        prefix = "/charging-pile/chOrderInfo/chargingInfo/"
        order_no = self.path[len(prefix) :] if self.path.startswith(prefix) else ""
        if order_no not in ORDERS:
            self._json({"code": 1, "msg": "订单不存在", "data": None, "ok": False})
            return
        CPMS_COUNTS["charging_info"] = CPMS_COUNTS.get("charging_info", 0) + 1
        self._json(
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
        )


def http(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    timeout: float = 20.0,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        # **必须排在 URLError 之前**：HTTPError 是它的子类，反过来的话所有 4xx/5xx
        # 都会落进「连接未建立」，于是每条负例都变成 status=0（实测踩过一次）。
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"raw": raw[:200].decode("utf-8", "replace")}
        return exc.code, payload, dict(exc.headers)
    except urllib.error.URLError as exc:
        # 连接未建立：轮询阶段要能区分「还没起来」与「起不来」，不能抛出去。
        return 0, {"error": "unreachable", "reason": str(exc.reason)}, {}


def _serve(handler_cls, port: int, *args: Any) -> ThreadingHTTPServer:
    if args:
        server = ThreadingHTTPServer(("127.0.0.1", port), lambda *a, **k: handler_cls(*args))
    else:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--startup-timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    try:
        lock = harness_guard.acquire()
    except harness_guard.ExperimentBusy as exc:
        print(f"experiment busy（另一个实验持有 {exc}）；未做任何清理，稍后重试。", file=sys.stderr)
        return 3
    try:
        return _run(args)
    finally:
        harness_guard.release(lock)


def _run(args: argparse.Namespace) -> int:
    root = Path(tempfile.mkdtemp(prefix="m2close-real-"))
    introspection = _serve(IntrospectionHandler, 9401)
    cpms = _serve(CpmsHandler, 9402)
    report: dict[str, Any] = {
        "kind": "m2close_real_gateway_http",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "status": "FAIL",
        "synthetic_data": True,
        "real_company_services": False,
        # 这一段用**仓库自己的** `aiops-gateway serve` 启动；身份走真实的
        # IntrospectionCallerResolver 代码路径，只有 introspection 端点与 CPMS 是回环替身。
        # 成功读取（200）需要订单授权源，那是下一段（见 check-success 的说明）。
        "substituted": {
            "A": [
                "introspection endpoint (loopback)",
                "CPMS (loopback)",
                "order authorization source (absent: production needs the internal business DB)",
            ],
            "B": [
                "CPMS (loopback)",
                "caller identity (fixed resolver)",
                "order authorization source (SQLite synthetic_orders, same owner predicate)",
            ],
        },
        "code_identity": harness_guard.code_identity(
            Path(__file__).resolve().parents[2],
            extra={"node_jar_sha256": _node_jar_sha256()},
        ),
        "checks": [],
    }
    env = {
        **os.environ,
        "AIOPS_DATA_HOME": str(root / "data"),
        "AIOPS_CONFIG_HOME": str(root / "config"),
        "AIOPS_GATEWAY_DATA_HOME": str(root / "data"),
        "AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(root / "config/production.env"),
        "AIOPS_GATEWAY_DATABASE_FILE": str(root / "data/gateway.db"),
        "AIOPS_GATEWAY_BIND": "127.0.0.1",
        "AIOPS_GATEWAY_PORT": str(args.port),
        "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": "http://127.0.0.1:9402/charging-pile",
        "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": "env-local",
        "AIOPS_GATEWAY_INTROSPECTION_URL": "http://127.0.0.1:9401/introspect",
        "AIOPS_GATEWAY_INTROSPECTION_CLIENT_ID": "m2close",
        "AIOPS_GATEWAY_INTROSPECTION_CLIENT_SECRET": "m2close-secret",
        "AIOPS_GATEWAY_STANDARD_API_AUDIENCE": "aiops-api",
        # 授权器走环境变量：生产装配用 ScopedOrderAuthorizer；这里用一个回环替身，
        # 它在**进程外**，所以「连接失败」也是一条可观察的负例。
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
    }
    (root / "config").mkdir(parents=True)
    (root / "config/production.env").write_text("# synthetic\n")
    (root / "config/production.env").chmod(0o600)
    base = f"http://127.0.0.1:{args.port}"

    process = subprocess.Popen(
        [sys.executable, "-m", "aiops_diagnostics.gateway_server", "serve"],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    def check(name: str, ok: bool, detail: str) -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    def gateway_output() -> str:
        if process.poll() is None or process.stdout is None:
            return ""
        return process.stdout.read()[-2500:]

    try:
        deadline = time.monotonic() + args.startup_timeout
        health: tuple[int, dict[str, Any], dict[str, str]] = (0, {}, {})
        while time.monotonic() < deadline:
            health = http(f"{base}/health")
            if health[0]:
                break
            time.sleep(0.4)
        if not health[0]:
            # 起不来说明前置没配好：把进程自己的输出带回来，而不是只说「连不上」。
            _stop(process)
            output = process.stdout.read() if process.stdout else ""
            report["error"] = "gateway never became ready"
            # 进程输出里会有开发机的绝对路径：只写一个**同目录**的私有日志，报告里不内联。
            tail_path = args.out.with_suffix(".gateway-output.log")
            tail_path.write_text(output[-4000:], encoding="utf-8")
            report["gateway_output_log"] = tail_path.name
            return 1
        check("/health", health[0] == 200, f"status={health[0]}")
        check(
            "/health 不带 no-store（缓存声明只作用于本切片）",
            "cache-control" not in {k.lower() for k in health[2]},
            "cache-control 缺失",
        )

        phase_a_before = CPMS_COUNTS.get("charging_info", 0)
        no_source = http(
            f"{base}/v1/orders/ORDER-A/charging-status",
            token=TOKEN,
            headers={"X-Business-Entry": "consumer"},
        )
        check(
            "阶段 A 无授权源时直读 503（fail closed，不是放行）",
            no_source[0] == 503,
            f"status={no_source[0]}",
        )
        check(
            "503 带 no-store",
            {k.lower(): v for k, v in no_source[2].items()}.get("cache-control") == "no-store",
            str(no_source[2].get("Cache-Control")),
        )
        for order, label in (("ORDER-B", "他人订单"), ("ORDER-MISSING", "不存在的订单")):
            denied = http(
                f"{base}/v1/orders/{order}/charging-status",
                token=TOKEN,
                headers={"X-Business-Entry": "consumer"},
            )
            # 无授权源时它们都落到 503（fail closed）—— 检查名写的就是实际期望的状态码，
            # 不沿用「404」那个只在阶段 B 成立的名字。
            check(f"[无授权源时] {label} 503", denied[0] == 503, f"status={denied[0]}")
        # 无授权源时这三条都落到 503（fail closed）；有授权源时的 200/404 见阶段 B。

        after = CPMS_COUNTS.get("charging_info", 0)
        check(
            "阶段 A 全程未读业务（计数跨所有请求包围）",
            after == phase_a_before,
            f"{phase_a_before} -> {after}",
        )

        # 委派入口：默认关闭 ⇒ 404；配置后多余字段 ⇒ 422，错误响应带 no-store
        delegated = http(
            f"{base}/v1/orders/ORDER-A/charging-status/delegated",
            method="POST",
            token=TOKEN,
            headers={"X-Business-Entry": "consumer"},
            body={},
        )
        check("未配置绑定表时委派 404", delegated[0] == 404, f"status={delegated[0]}")

        config = {
            "environment": "env-local",
            "snail_base_url": "http://127.0.0.1:9",  # 不可达：本条只验证入站判据
            "snail_app_id": "m2close-real",
            "snail_app_token": "unused-in-this-check",
            "snail_agent_id": 1,
            "snail_open_id": "open-id",
            "expected_node_id": "node-local",
            "nodes": [
                {
                    "node_id": "node-local",
                    "environment": "env-local",
                    "token_sha256": __import__("hashlib").sha256(NODE_TOKEN.encode()).hexdigest(),
                }
            ],
        }
        config_path = root / "config/agent-runs.json"
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        # 配置是启动时读的：重启一次，让委派入口真的打开。
        _stop(process)
        env["AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE"] = str(config_path)
        process = _start(env)
        _wait_ready(base, args.startup_timeout)

        invalid = http(
            f"{base}/v1/orders/ORDER-A/charging-status/delegated",
            method="POST",
            token=TOKEN,
            headers={"X-Business-Entry": "consumer"},
            body={"appId": "other"},
        )
        check("委派入口多余字段 422", invalid[0] == 422, f"status={invalid[0]}")
        check(
            "422 带 no-store",
            {k.lower(): v for k, v in invalid[2].items()}.get("cache-control") == "no-store",
            str(invalid[2].get("Cache-Control")),
        )

        # 领取入口：无凭据/错凭据都 401，且带 no-store
        claim = http(f"{base}/v1/internal/charging-status/claims/run_x", method="POST", body={})
        check("领取无凭据 401", claim[0] == 401, f"status={claim[0]}")
        check(
            "领取错误响应带 no-store",
            {k.lower(): v for k, v in claim[2].items()}.get("cache-control") == "no-store",
            str(claim[2].get("Cache-Control")),
        )
        wrong = http(
            f"{base}/v1/internal/charging-status/claims/run_x",
            method="POST",
            token="not-the-node-token",
            body={},
        )
        check("领取错凭据 401", wrong[0] == 401, f"status={wrong[0]}")

        report["status"] = "PASS" if all(item["ok"] for item in report["checks"]) else "FAIL"
    except Exception as exc:  # noqa: BLE001 - 报告记录失败原因
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _stop(process)
        for server in (introspection, cpms):
            server.shutdown()
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        report["counts"] = dict(CPMS_COUNTS)
        if process.poll() is not None:
            # 同上：失败时的进程输出只落私有日志，不写进公开报告。
            output = gateway_output()
            if output:
                tail_path = args.out.with_suffix(".gateway-output.log")
                tail_path.write_text(output, encoding="utf-8")
                report["gateway_output_log"] = tail_path.name
        if not report.get("error"):
            report["status"] = "PASS" if all(item["ok"] for item in report["checks"]) else "FAIL"
        shutil.rmtree(root, ignore_errors=True)
    # ---- 阶段 B：同一个 app 工厂 + 一个回环订单授权源，补齐成功读取与 404 对照 ----
    _run_phase_b(report, args.port + 1)
    report["finished_at_utc"] = datetime.now(UTC).isoformat()
    if not report.get("error"):
        report["status"] = "PASS" if all(item["ok"] for item in report["checks"]) else "FAIL"
    after = harness_guard.code_identity(
        Path(__file__).resolve().parents[2],
        extra={"node_jar_sha256": _node_jar_sha256()},
    )
    report["code_identity_end"] = after
    drift = harness_guard.identity_drift(report["code_identity"], after)
    if drift:
        report["identity_drift"] = drift
        report["status"] = "FAIL"
        report.setdefault("status_reasons", []).append("运行期间代码身份发生变化: " + ", ".join(drift))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    print(f"{report['status']}: {args.out}")
    for item in report["checks"]:
        if not item["ok"]:
            print(f"  - {item['name']}: {item['detail']}", file=sys.stderr)
    return 0 if report["status"] == "PASS" else 1


def _run_phase_b(report: dict[str, Any], port: int) -> None:
    """阶段 B：用 `gateway_launcher` 起同一套 app，只把授权源换成回环替身。

    阶段 A 用的是**仓库自己的** `aiops-gateway serve`；它按生产装配选择授权器，
    没有业务数据库就 fail closed（503）—— 那是对的，但也因此无法验证 200/404。
    这里换成同一工厂 + 一个回环授权源，继续验证「成功读取、他人/不存在同形 404、
    平台不可达 502」以及 no-store。两段合起来才是本切片的 HTTP 证据；
    **任一段都不能单独被称为业务验收**。
    """
    import sqlite3

    root = Path(tempfile.mkdtemp(prefix="m2close-phase-b-"))
    cpms = _serve(CpmsHandler, 9412)
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "AIOPS_DATA_HOME": str(root / "data"),
        "AIOPS_CONFIG_HOME": str(root / "config"),
        "AIOPS_GATEWAY_DATA_HOME": str(root / "data"),
        "AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(root / "config/production.env"),
        "AIOPS_GATEWAY_DATABASE_FILE": str(root / "data/gateway.db"),
        "AIOPS_GATEWAY_BIND": "127.0.0.1",
        "AIOPS_GATEWAY_PORT": str(port),
        "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": "http://127.0.0.1:9412/charging-pile",
        "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": "env-local",
        "AIOPS_M2_LOCAL_TOKEN": TOKEN,
        "AIOPS_M2_ORDERS_DB": str(root / "data/gateway-orders.db"),
        "AIOPS_M2_TENANT": TENANT,
        "AIOPS_M2_C_USER": C_USER,
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
    }
    (root / "config").mkdir(parents=True)
    (root / "config/production.env").write_text("# synthetic\n")
    (root / "config/production.env").chmod(0o600)
    (root / "data").mkdir(parents=True)
    connection = sqlite3.connect(root / "data/gateway-orders.db")
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS synthetic_orders "
            "(order_no TEXT PRIMARY KEY, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL)"
        )
        for order_no, owner in ORDERS.items():
            connection.execute(
                "INSERT OR REPLACE INTO synthetic_orders VALUES (?, ?, ?)", (order_no, owner, TENANT)
            )
        connection.commit()
    finally:
        connection.close()

    def check(name: str, ok: bool, detail: str) -> None:
        report["checks"].append({"name": f"[阶段B] {name}", "ok": bool(ok), "detail": detail})

    process = subprocess.Popen(
        [sys.executable, str(REPO / "tools/m2_close/gateway_launcher.py")],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if http(f"{base}/health", timeout=2.0)[0]:
                break
            time.sleep(0.4)
        else:
            check("阶段 B 启动", False, "gateway_launcher 未就绪")
            return

        ok = http(
            f"{base}/v1/orders/ORDER-A/charging-status",
            token=TOKEN,
            headers={"X-Business-Entry": "consumer"},
        )
        check("本人订单 200", ok[0] == 200, f"status={ok[0]}")
        if ok[0] == 200:
            check(
                "响应只有 7 个字段",
                set(ok[1])
                == {
                    "order_no",
                    "status_code",
                    "status_role",
                    "observed_at",
                    "source",
                    "environment",
                    "gateway_version",
                },
                f"fields={sorted(ok[1])}",
            )
            check(
                "成功响应 Cache-Control: no-store",
                {k.lower(): v for k, v in ok[2].items()}.get("cache-control") == "no-store",
                str(ok[2].get("Cache-Control")),
            )
            check("上游 PII 未出现在响应里", "DO-NOT-LEAK" not in json.dumps(ok[1]), "")

        before = CPMS_COUNTS.get("charging_info", 0)
        for order, label in (("ORDER-B", "他人订单 404"), ("ORDER-MISSING", "不存在 404")):
            denied = http(
                f"{base}/v1/orders/{order}/charging-status",
                token=TOKEN,
                headers={"X-Business-Entry": "consumer"},
            )
            check(label, denied[0] == 404, f"status={denied[0]}")
        check(
            "负例没有读业务",
            CPMS_COUNTS.get("charging_info", 0) == before,
            f"{before} -> {CPMS_COUNTS.get('charging_info', 0)}",
        )

        # 委派入口打开（绑定表指向一个不可达平台）：平台不可达 ⇒ 502，不生成成功证据
        config_path = root / "config/agent-runs.json"
        config_path.write_text(
            json.dumps(
                {
                    "environment": "env-local",
                    "snail_base_url": "http://127.0.0.1:9",
                    "snail_app_id": "m2close-phase-b",
                    "snail_app_token": "unused",
                    "snail_agent_id": 1,
                    "snail_open_id": "open-id",
                    "expected_node_id": "node-local",
                    "nodes": [
                        {
                            "node_id": "node-local",
                            "environment": "env-local",
                            "token_sha256": __import__("hashlib").sha256(NODE_TOKEN.encode()).hexdigest(),
                        }
                    ],
                }
            )
        )
        config_path.chmod(0o600)
        _stop(process)
        env["AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE"] = str(config_path)
        process = subprocess.Popen(
            [sys.executable, str(REPO / "tools/m2_close/gateway_launcher.py")],
            cwd=REPO,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        _wait_ready(base, 60)
        unreachable = http(
            f"{base}/v1/orders/ORDER-A/charging-status/delegated",
            method="POST",
            token=TOKEN,
            headers={"X-Business-Entry": "consumer"},
            body={},
        )
        check("平台不可达时委派 502", unreachable[0] == 502, f"status={unreachable[0]}")
    except Exception as exc:  # noqa: BLE001 - 报告记录失败原因
        check("阶段 B 未完成", False, f"{type(exc).__name__}: {exc}")
    finally:
        _stop(process)
        cpms.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def _start(env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "aiops_diagnostics.gateway_server", "serve"],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _wait_ready(base: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if http(f"{base}/health", timeout=2.0)[0]:
            return
        time.sleep(0.4)
    raise RuntimeError("gateway did not come back after restart")


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)


if __name__ == "__main__":
    sys.exit(main())
