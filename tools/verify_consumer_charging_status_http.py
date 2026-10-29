#!/usr/bin/env python3
"""充电状态直读端点的 HTTP 面证据：真 `aiops-gateway serve` + 一次性数据根（本机，合成依赖）。

与 `tests/consumer/test_consumer_charging_status.py` 的分工：用例走 TestClient（另有一个
TestClient 级的真 HTTP 用例）；本脚本补的是**实际 CLI 装配** ——
`python -m aiops_diagnostics.gateway_server serve` 在自己的进程里起监听，
再用真 HTTP 打它。默认关闭、鉴权、拒绝、no-store 都在这一段取证。

三段都用同一个回环 introspection 桩（生产代码路径 `IntrospectionCallerResolver`），
所以「未认证 401 / 认证后仍被拒」取的是真解析结果，不是伪造的 `ScopeContext`。

三段，各自说明边界
------------------
A 段：**默认关闭**。不配 `AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` ⇒ 路由不存在（404），
`/health` 不带 `Cache-Control`；同一段里先过一次回环 introspection 认证，
证明 404 是「路由不存在」而不是「身份没通过」。
B 段：**真 CLI + 真实 introspection 解析器**。调用者身份走生产代码路径
`IntrospectionCallerResolver`，introspection 端点与 CPMS 是回环替身；订单授权源没有
（本机没有生产订单库），所以订单授权按 fail-closed 给 503 —— 这一段证明**拒绝方向**。
C 段：**同一个 app 工厂 + 回环订单授权源**，补齐 200 / 他人 404 / 上游坏 502。
这一步替换了订单授权源（唯一一处），所以它是「路由与读取行为」的证据，
**不是**「正式装配完整成功链路」。

任何一段都不能被读成业务验收。不接公司服务、不读生产数据根、不写生产配置。

用法（从仓库根）：
  uv run python tools/verify_consumer_charging_status_http.py --out /tmp/a-http.json
退出码 0=PASS，1=有判据失败，2=前置缺失（脚本自己报哪一条）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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

REPO = Path(__file__).resolve().parents[1]
TOKEN = "a-http-token"
TENANT = "TENANT-A"
C_USER = "USER-A"
ORDERS = {"ORDER-A": C_USER, "ORDER-C": C_USER, "ORDER-B": "USER-B"}
CPMS_CALLS: list[str] = []
CPMS_MODE = {"value": "ok"}
DEFAULT_PORT = 9310


class _QuietHandler(BaseHTTPRequestHandler):
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


class IntrospectionHandler(_QuietHandler):
    """生产代码路径上的 introspection 端点；只认一个令牌，其余一律 inactive。"""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        token = self.rfile.read(length).decode().removeprefix("token=")
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


class CpmsHandler(_QuietHandler):
    """桩 CPMS。计数放在 handler 之外（服务器按请求实例化 handler）。"""

    def do_GET(self) -> None:  # noqa: N802
        prefix = "/charging-pile/chOrderInfo/chargingInfo/"
        order_no = self.path[len(prefix) :] if self.path.startswith(prefix) else ""
        CPMS_CALLS.append(self.path)
        if CPMS_MODE["value"] == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if CPMS_MODE["value"] == "garbage":
            self._json({"ok": True, "data": {"orderNo": order_no, "status": "0"}})
            return
        self._json(
            {
                "code": 0,
                "msg": "success",
                "data": {
                    "orderNo": order_no,
                    "status": "0",
                    "plateNo": "DO-NOT-RETURN",
                    "userId": "DO-NOT-RETURN",
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
        # HTTPError 是 URLError 的子类，必须先接住 —— 否则每个 4xx/5xx 都变成 status=0。
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"raw": raw[:200].decode("utf-8", "replace")}
        return exc.code, payload, dict(exc.headers)
    except urllib.error.URLError as exc:
        return 0, {"error": "unreachable", "reason": str(exc.reason)}, {}


def _serve(handler_cls: type[BaseHTTPRequestHandler], port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _env(root: Path, port: int) -> dict[str, str]:
    return {
        **os.environ,
        "AIOPS_DATA_HOME": str(root / "data"),
        "AIOPS_CONFIG_HOME": str(root / "config"),
        "AIOPS_GATEWAY_DATA_HOME": str(root / "data"),
        "AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(root / "config/production.env"),
        "AIOPS_GATEWAY_DATABASE_FILE": str(root / "data/gateway.db"),
        "AIOPS_GATEWAY_BIND": "127.0.0.1",
        "AIOPS_GATEWAY_PORT": str(port),
        "AIOPS_GATEWAY_INTROSPECTION_URL": f"http://127.0.0.1:{port + 1}/introspect",
        "AIOPS_GATEWAY_INTROSPECTION_CLIENT_ID": "a-http",
        "AIOPS_GATEWAY_INTROSPECTION_CLIENT_SECRET": "a-http-secret",
        "AIOPS_GATEWAY_STANDARD_API_AUDIENCE": "aiops-api",
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
    }


def _root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="a-http-"))
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    config = root / "config/production.env"
    config.write_text("# synthetic verification only\n")
    config.chmod(0o600)
    return root


def _wait_ready(base: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if http(f"{base}/health")[0] == 200:
            return True
        time.sleep(0.3)
    return False


def _start(env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "aiops_diagnostics.gateway_server", "serve"],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


HEADERS = {"X-Business-Entry": "consumer"}


def _no_store(response: tuple[int, dict[str, Any], dict[str, str]]) -> str | None:
    return {k.lower(): v for k, v in response[2].items()}.get("cache-control")


def phase_a(report: dict[str, Any], port: int) -> None:
    """默认关闭：没有业务接口地址时路由 404，且这不依赖任何外部依赖是否可达。"""
    root = _root()
    base = f"http://127.0.0.1:{port}"
    process = _start(_env(root, port))
    try:
        if not _wait_ready(base, 60):
            report["error"] = "阶段 A：gateway 未就绪"
            report["gateway_output_tail"] = (process.stdout.read() if process.stdout else "")[-2000:]
            return
        health = http(f"{base}/health")
        _check(
            report,
            "A/health 200 且 business_mutations=disabled",
            health[0] == 200 and health[1].get("business_mutations") == "disabled",
            f"status={health[0]} body={health[1]}",
        )
        _check(
            report,
            "A//health 不带 no-store（缓存声明只作用于本切片）",
            _no_store(health) is None,
            str(_no_store(health)),
        )
        # 认证确实在这一段成立：既有订单端点给出 503（授权源缺席）而不是 401。
        # 这让下面的 404 可以被读成「路由不存在」，而不是「身份没过」。
        authenticated = http(f"{base}/v1/orders/ORDER-A/access", token=TOKEN)
        _check(
            report,
            "A/同一令牌在既有端点上通过认证（503 = 授权源缺席，不是 401）",
            authenticated[0] == 503,
            f"status={authenticated[0]}",
        )
        disabled = http(f"{base}/v1/orders/ORDER-A/charging-status", token=TOKEN, headers=HEADERS)
        _check(report, "A/未配置业务接口时 404（默认关闭）", disabled[0] == 404, f"status={disabled[0]}")
        _check(
            report,
            "A/默认关闭的 404 带 no-store",
            _no_store(disabled) == "no-store",
            str(_no_store(disabled)),
        )
    finally:
        _stop(process)
        shutil.rmtree(root, ignore_errors=True)


def phase_b(report: dict[str, Any], port: int) -> None:
    """真 CLI + 真实 introspection 解析器；订单授权源缺席 ⇒ 拒绝方向。"""
    root = _root()
    base = f"http://127.0.0.1:{port}"
    env = {
        **_env(root, port),
        "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": f"http://127.0.0.1:{port + 2}/charging-pile",
        "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": "env-local",
    }
    process = _start(env)
    try:
        if not _wait_ready(base, 60):
            report["error"] = "阶段 B：gateway 未就绪"
            report["gateway_output_tail"] = (process.stdout.read() if process.stdout else "")[-2000:]
            return
        before = len(CPMS_CALLS)
        unauthenticated = http(f"{base}/v1/orders/ORDER-A/charging-status", headers=HEADERS)
        _check(report, "B/无凭据 401", unauthenticated[0] == 401, f"status={unauthenticated[0]}")
        _check(
            report,
            "B/401 带 no-store",
            _no_store(unauthenticated) == "no-store",
            str(_no_store(unauthenticated)),
        )
        bad_token = http(f"{base}/v1/orders/ORDER-A/charging-status", token="not-a-token", headers=HEADERS)
        _check(
            report,
            "B/无效令牌 401（introspection 判 inactive）",
            bad_token[0] == 401,
            f"status={bad_token[0]}",
        )
        operator = http(
            f"{base}/v1/orders/ORDER-A/charging-status",
            token=TOKEN,
            headers={"X-Business-Entry": "operator"},
        )
        _check(report, "B/非 consumer 入口 403（不落到授权）", operator[0] == 403, f"status={operator[0]}")
        missing_entry = http(f"{base}/v1/orders/ORDER-A/charging-status", token=TOKEN)
        _check(
            report,
            "B/缺 X-Business-Entry 403（不默认成本人）",
            missing_entry[0] == 403,
            f"status={missing_entry[0]}",
        )
        unavailable = http(f"{base}/v1/orders/ORDER-A/charging-status", token=TOKEN, headers=HEADERS)
        _check(
            report,
            "B/订单授权源缺席时 503（fail closed，不是放行）",
            unavailable[0] == 503,
            f"status={unavailable[0]}",
        )
        _check(report, "B/503 带 no-store", _no_store(unavailable) == "no-store", str(_no_store(unavailable)))
        _check(report, "B/拒绝方向全程未读 CPMS", len(CPMS_CALLS) == before, f"{before} -> {len(CPMS_CALLS)}")
    finally:
        _stop(process)
        shutil.rmtree(root, ignore_errors=True)


def phase_c(report: dict[str, Any], port: int) -> None:
    """同一 app 工厂 + 回环订单授权源：补齐 200 / 404 / 502 / 302。

    唯一替换的是订单授权源（生产订单库的替身，判据同为「订单 owner == 主体 C 端用户」）。
    调用者身份仍走真实 `IntrospectionCallerResolver`。
    """
    import socket

    import uvicorn

    from aiops_diagnostics.caller_auth import IntrospectionCallerResolver, IntrospectionSettings
    from aiops_diagnostics.gateway_api import create_gateway_app
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway_store import GatewayStore
    from aiops_diagnostics.scope_context import ScopeContext

    class Orders:
        def can_access(self, context: ScopeContext, order_no: str) -> bool:
            return context.subject.c_user_id is not None and ORDERS.get(order_no) == context.subject.c_user_id

    root = _root()
    settings = GatewayServerSettings(
        data_home=root / "data",
        database_file=root / "data/gateway.db",
        server_config_file=root / "config/production.env",
        charging_status_base_url=f"http://127.0.0.1:{port + 2}/charging-pile",
        execution_environment="env-local",
    )
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        caller_resolver=IntrospectionCallerResolver(
            IntrospectionSettings(
                url=f"http://127.0.0.1:{port + 1}/introspect",
                client_id="a-http",
                client_secret="a-http-secret",
                audience="aiops-api",
            )
        ),
        order_authorizer=Orders(),
    )
    # 用临时端口：前两段刚在同一个号上起过真进程，固定号会被 TIME_WAIT 咬住（实测踩过一次）。
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 20
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not server.started:
                report["error"] = "阶段 C：app 未就绪"
                return
            base = f"http://127.0.0.1:{listener.getsockname()[1]}"
            before = len(CPMS_CALLS)
            ok = http(f"{base}/v1/orders/ORDER-A/charging-status", token=TOKEN, headers=HEADERS)
            _check(report, "C/本人订单 200", ok[0] == 200, f"status={ok[0]}")
            _check(report, "C/成功响应带 no-store", _no_store(ok) == "no-store", str(_no_store(ok)))
            body = ok[1]
            _check(
                report,
                "C/响应字段恰好七个且不含车牌/用户标识",
                set(body)
                == {
                    "order_no",
                    "status_code",
                    "status_role",
                    "observed_at",
                    "source",
                    "environment",
                    "gateway_version",
                }
                and "DO-NOT-RETURN" not in json.dumps(body, ensure_ascii=False),
                json.dumps(sorted(body), ensure_ascii=False),
            )
            _check(
                report,
                "C/成功读取恰好读一次 CPMS",
                len(CPMS_CALLS) == before + 1,
                f"{before} -> {len(CPMS_CALLS)}",
            )
            other = http(f"{base}/v1/orders/ORDER-B/charging-status", token=TOKEN, headers=HEADERS)
            missing = http(f"{base}/v1/orders/ORDER-MISSING/charging-status", token=TOKEN, headers=HEADERS)
            _check(report, "C/他人订单 404", other[0] == 404, f"status={other[0]}")
            _check(report, "C/不存在订单 404（与越权同形）", missing[0] == 404, f"status={missing[0]}")
            _check(report, "C/两条 404 都不读 CPMS", len(CPMS_CALLS) == before + 1, f"{len(CPMS_CALLS)}")
            CPMS_MODE["value"] = "garbage"
            bad = http(f"{base}/v1/orders/ORDER-A/charging-status", token=TOKEN, headers=HEADERS)
            _check(
                report,
                "C/信封不对 502（不透传上游正文）",
                bad[0] == 502 and "ok" not in bad[1],
                f"status={bad[0]}",
            )
            CPMS_MODE["value"] = "redirect"
            redirect = http(f"{base}/v1/orders/ORDER-A/charging-status", token=TOKEN, headers=HEADERS)
            _check(
                report,
                "C/上游 302 被拒（不把身份头带去别处）→ 502",
                redirect[0] == 502,
                f"status={redirect[0]}",
            )
            CPMS_MODE["value"] = "ok"
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            shutil.rmtree(root, ignore_errors=True)


def _check(report: dict[str, Any], name: str, ok: bool, detail: str) -> None:
    report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    report: dict[str, Any] = {
        "kind": "a_consumer_charging_status_http",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "status": "FAIL",
        "synthetic_data": True,
        "real_company_services": False,
        "phases": {
            "A": "real `aiops-gateway serve`, charging disabled, loopback introspection + CPMS",
            "B": (
                "real `aiops-gateway serve`, real IntrospectionCallerResolver, "
                "loopback introspection + CPMS, no order source"
            ),
            "C": (
                "same create_gateway_app factory, real introspection resolver, "
                "loopback synthetic order authorizer"
            ),
        },
        "substituted": {
            "A": ["introspection endpoint (loopback)", "CPMS (loopback)"],
            "B": [
                "introspection endpoint (loopback)",
                "CPMS (loopback)",
                "order authorization source (absent by design)",
            ],
            "C": [
                "introspection endpoint (loopback)",
                "CPMS (loopback)",
                "order authorization source (synthetic table)",
            ],
        },
        "checks": [],
    }
    introspection = _serve(IntrospectionHandler, args.port + 1)
    cpms = _serve(CpmsHandler, args.port + 2)
    try:
        phase_a(report, args.port)
        if not report.get("error"):
            phase_b(report, args.port)
        if not report.get("error"):
            phase_c(report, args.port)
    except Exception as exc:  # noqa: BLE001 - 报告记录失败原因
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for server in (introspection, cpms):
            server.shutdown()
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        if not report.get("error"):
            report["status"] = "PASS" if all(item["ok"] for item in report["checks"]) else "FAIL"
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"{report['status']}: {args.out}")
    for item in report["checks"]:
        if not item["ok"]:
            print(f"  - {item['name']}: {item['detail']}", file=sys.stderr)
    if report.get("error"):
        print(f"  error: {report['error']}", file=sys.stderr)
        return 2
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
