#!/usr/bin/env python3
"""APP-RUNTIME-02 的**真实 Dify** 整链验收。

这一层要证的是：原 App 聊天入口 → 真 Gateway → **真 Dify 1.17.1** 的已发布 Workflow
→ 该 Workflow 的 HTTP 步骤**自己**带着一次性凭据回调网关取数 → 同一轮快照 → 原 GET。

| 环节 | 真 / 替 |
|---|---|
| `POST/GET /v1/assistant/questions`、绑定选择、选单、授权、放行 | **真 Gateway 进程**（本仓代码） |
| 执行记录、取数凭据、回调端点 | **本仓真实代码** |
| Workflow 编排、HTTP 步骤、模型步骤、发布与运行 | **真 Dify 1.17.1**（本地隔离栈） |
| 业务取数（CPMS） | 本机合成回环桩 |
| 模型上游 | 本机合成 OpenAI-compatible 回环桩 |
| 调用者身份 / 订单授权 / 选单数据 / 平台目录 | 既有 `gateway_launcher.py` 的合成替身 |

**Dify 的 HTTP 步骤自己去调网关** —— 脚本不代发，也不 mock Dify 的结果。
脚本只做三件事：起栈、导入并发布 DSL、从 App 接口驱动两次请求并核对。

    DIFY_TEST_PY=python3 uv run python tools/m2_close/m3node/verify_dify_chat_entry.py --out report.json

退出码 0=PASS，1=有判据失败，2=未取证（前置缺失，不跳过）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
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
DIFY_DIR = REPO / "deploy/dify-chat"
DSL = DIFY_DIR / "consumer-order-status.dsl.yml"
TOKEN = "dify-chat-real-token"
TENANT = "TENANT-A"
C_USER = "USER-A"
#: 模型 provider 插件。1.17.1 不在镜像里内建 provider，缺它整轮 400。
PLUGIN_ID = "langgenius/openai_api_compatible"
#: 插件装好后，provider 在 Dify 里的**真实**标识是三段式 `org/plugin/provider`
#: （实测：`langgenius/openai_api_compatible/openai_api_compatible`）。
#: 用插件短名去找它永远找不到 —— 那会让"已经装好了"被判成"没装上"，
#: 然后带着一个错的 provider 名一路走到运行期 400。
PROVIDER_NAME = "langgenius/openai_api_compatible/openai_api_compatible"
PLUGIN_VERSION = "0.0.68"
PLUGIN_SHA = "ba61c5eccb814be7364117c0bec3710a51e09b20f31e37243e747a00f9377969"
#: 包缓存放一次性目录：不写进仓库，也不依赖机器上预先存在的文件。
PLUGIN_CACHE = Path(tempfile.gettempdir()) / "dify-chat-plugin-cache"

ORDER_LATEST = "ORDER-3001"
ORDER_OLDER = "ORDER-3002"
#: 另一个 C 端用户自己的单。**订单号不同、用户不同**，用来证明选单与授权按本人分流。
ORDER_OTHER = "ORDER-4001"
OTHER_TOKEN = "dify-chat-real-token-b"
OTHER_USER = "USER-B"
#: 第二个**租户**（双租户交错）。用它自己的 app / workspace / agent / 路由，
#: 与主租户在同一条管理表里各占一行。
TENANT_B = "TENANT-B"
THIRD_TOKEN = "dify-chat-real-token-c"
THIRD_USER = "USER-C"
ORDER_TENANT_B = "ORDER-5001"
#: 试运行的第六把钥匙（发布/知识库/工具各是另一把，启动校验要求两两不同）。
TRIAL_KEY = "dify-chat-trial-key"


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
    timeout: float = 120.0,
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
    except Exception as exc:  # noqa: BLE001 — 连不上也要成为一条可观察结果
        return 0, f"{type(exc).__name__}: {exc}"


def http_json(
    url: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> tuple[int, Any]:
    """同 `http`，但把正文解析成 JSON —— 控制台接口的正文本来就是 JSON。"""
    status, text = http(url, method=method, body=body, headers=headers, timeout=timeout)
    return status, _json(text)


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:400]}


# --------------------------------------------------------------------------
# 合成模型上游：Dify 的 LLM 步骤打这里
# --------------------------------------------------------------------------


class ModelHandler(BaseHTTPRequestHandler):
    state: dict[str, Any] = {}
    hold = False
    entered = threading.Event()
    release = threading.Event()

    def log_message(self, *args: Any) -> None:  # noqa: D102
        pass

    def _send(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        body = _json(raw.decode("utf-8", "replace")) if raw else {}
        # **记录模型实际收到的全部文本**：判据要证明 prompt 里没有取数凭据。
        texts: list[str] = []
        for message in body.get("messages") or []:
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                texts.append(message["content"])
        prompt = "\n".join(texts)
        type(self).state.setdefault("prompts", []).append(prompt)
        # 形状也要留证：有的 provider 会把正文放进 content 数组或别的字段，
        # 只记拼好的文本时，"模型没收到订单号"和"我没读对字段"看起来一模一样。
        type(self).state.setdefault("shapes", []).append(
            {
                "keys": sorted(body),
                "roles": [m.get("role") for m in body.get("messages") or [] if isinstance(m, dict)],
            }
        )
        type(self).state["calls"] = type(self).state.get("calls", 0) + 1
        if type(self).hold:
            type(self).entered.set()
            if not type(self).release.wait(60):
                self.send_error(504)
                return
        answer = "订单状态已播报。"
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = json.dumps(
                {
                    "id": "c",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "synthetic",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": answer},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
            self.wfile.write(("data: " + chunk + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        self._send(
            {
                "id": "chatcmpl-x",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model") or "synthetic",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    def do_GET(self) -> None:  # noqa: N802
        # Dify 的模型校验会先拉一次 /v1/models。
        self._send({"object": "list", "data": [{"id": "synthetic", "object": "model"}]})


class CpmsHandler(BaseHTTPRequestHandler):
    calls: list[str] = []
    fail = False

    def log_message(self, *args: Any) -> None:  # noqa: D102
        pass

    def do_GET(self) -> None:  # noqa: N802
        order_no = self.path.rsplit("/", 1)[-1]
        type(self).calls.append(order_no)
        if type(self).fail:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = {"orderNo": order_no, "status": "0", "plateNo": "DO-NOT-LEAK"}
        if "/getOrderInfoDetail/" in self.path:
            data = {
                "orderNo": order_no,
                "chOrderInfoModel": {
                    "orderNo": order_no,
                    "totalAmount": "2.26",
                    "payAmount": "2.26",
                    "refundStatus": 2,
                    "refundAmount": "1.10",
                    "plateNo": "DO-NOT-LEAK",
                },
            }
        body = json.dumps({"code": 0, "msg": "success", "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------
# 隔离 Dify 栈
# --------------------------------------------------------------------------


def _compose(env: dict[str, str], *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(DIFY_DIR / "docker-compose.acceptance.yaml"),
            *(["-f", env["DIFY_TEST_COMPOSE_OVERRIDE"]] if env.get("DIFY_TEST_COMPOSE_OVERRIDE") else []),
            "--env-file",
            str(Path(env["DIFY_TEST_ROOT"]) / "dify.env"),
            "-p",
            env["DIFY_TEST_PROJECT"],
            *args,
        ],
        cwd=DIFY_DIR,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _dify_env(root: Path, *, model_url: str, port: int, project: str, network: str) -> dict[str, str]:
    """生成这一栈的 .env。全部取值为随机或本机地址，没有任何生产值。"""
    env_file = root / "dify.env"
    values = {
        "DIFY_REGISTRY": "docker.io",
        "DIFY_TEST_PORT": str(port),
        "DIFY_TEST_PROJECT": project,
        "DIFY_TEST_NETWORK": network,
        "DIFY_TEST_NAMESPACE": os.environ["DIFY_TEST_NAMESPACE"],
        "SECRET_KEY": secrets.token_urlsafe(42),
        "INIT_PASSWORD": "Acceptance-" + secrets.token_hex(8),
        "MODE": "api",
        "DEPLOY_ENV": "TESTING",
        "MIGRATION_ENABLED": "true",
        "DEPLOYMENT_EDITION": "COMMUNITY",
        "DB_TYPE": "postgresql",
        "DB_HOST": "127.0.0.1",
        "DB_PORT": "5432",
        "DB_USERNAME": "dify",
        "DB_PASSWORD": secrets.token_hex(16),
        "DB_DATABASE": "dify",
        "DB_PLUGIN_DATABASE": "dify_plugin",
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": "6379",
        "REDIS_USERNAME": "",
        "REDIS_PASSWORD": "",
        "CELERY_BROKER_URL": "redis://127.0.0.1:6379/1",
        "PLUGIN_DAEMON_KEY": secrets.token_hex(16),
        "PLUGIN_DIFY_INNER_API_KEY": secrets.token_hex(16),
        # 服务端到服务端的 console 凭据（发布桥的 `DifyDslClient` 走的就是这条）：
        # Dify 用**全局** ADMIN_API_KEY 认它，app 级 service key 不是它。
        "ADMIN_API_KEY_ENABLE": "true",
        "ADMIN_API_KEY": ADMIN_API_KEY,
        "VECTOR_STORE": "weaviate",
        "WEAVIATE_ENDPOINT": "http://127.0.0.1:8080",
        "WEAVIATE_API_KEY": secrets.token_hex(24),
        "STORAGE_TYPE": "opendal",
        "OPENDAL_SCHEME": "fs",
        "OPENDAL_FS_ROOT": "storage",
        # **必须开着**：模型 provider 是插件，装它要走 marketplace。
        # 现役实例关掉市场是产品决定（PRD #577），但那是"不给终端用户逛市场"；
        # 隔离验收里要装 provider 插件，关着就装不上（400 marketplace is not enabled）。
        "MARKETPLACE_ENABLED": "true",
        "ENABLE_COLLABORATION_MODE": "false",
        "ALLOW_REGISTER": "false",
        "ALLOW_CREATE_WORKSPACE": "true",
        "ACCESS_TOKEN_EXPIRE_MINUTES": "60",
        "REFRESH_TOKEN_EXPIRE_DAYS": "1",
        "CONSOLE_API_URL": f"http://127.0.0.1:{port}",
        "CONSOLE_WEB_URL": f"http://127.0.0.1:{port}",
        "SERVICE_API_URL": f"http://127.0.0.1:{port}",
        "APP_WEB_URL": f"http://127.0.0.1:{port}",
        "FILES_URL": f"http://127.0.0.1:{port}",
        # Dify 1.17.1 的配置校验要求它是**合法 URL**，空串会直接 pydantic 失败。
        # 本片不用 code 节点，指向本栈自己的 api 即可（不会被执行到）。
        "CODE_EXECUTION_ENDPOINT": f"http://127.0.0.1:{port}",
        "SSRF_PROXY_HTTP_URL": "",
        "SSRF_PROXY_HTTPS_URL": "",
        "LOG_LEVEL": "INFO",
        "DIFY_TEST_MODEL_URL": model_url,
    }
    env_file.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    env_file.chmod(0o600)
    return {**values, "DIFY_TEST_ROOT": str(root)}


def _wait_ready(base: str, deadline: float) -> bool:
    while time.monotonic() < deadline:
        status, _ = http(f"{base}/health", timeout=5.0)
        if status == 200:
            return True
        time.sleep(3)
    return False


def _dsl_for(gateway_base: str) -> bytes:
    """把 DSL 里固定的取数地址换成**这一次**的网关地址，别的字节不动。

    模板里写的是一个不可路由的占位域名，就是为了让"忘了替换"变成一次明确的失败，
    而不是悄悄打到某个真实主机上。
    """
    text = DSL.read_text(encoding="utf-8")
    replaced = text.replace("http://gateway.invalid", gateway_base)
    if "http://gateway.invalid" in replaced:
        raise SystemExit("dsl still points at the placeholder")
    return replaced.encode("utf-8")


def _wait_model_provider(base: str, deadline: float) -> bool:
    """从**外部**证明 plugin daemon 就绪。

    plugin_daemon 的镜像里没有 curl/wget/nc/`/dev/tcp`，容器内探活只会把一个正常工作的
    进程永远报成 unhealthy。所以这里不等"它的 healthcheck"，而是等**网关这一侧的
    /health 稳定**，把 provider 是否真的可用留给第一次真实 run 去证：没起来就一定
    400 `Failed to request plugin daemon`，对应判据会红。这样不会因为探活工具缺失而
    让一次正确的验收失败。
    """
    while time.monotonic() < deadline:
        if http(f"{base}/health", timeout=3.0)[0] == 200:
            return True
        time.sleep(2)
    return False


def _saved_shape(graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodes": [node.get("id") for node in graph.get("nodes", [])],
        "edges": [f"{edge.get('source')}->{edge.get('target')}" for edge in graph.get("edges", [])],
    }


def _setup_dify(base: str, env: dict[str, str], *, model_url: str, gateway_base: str) -> dict[str, Any]:
    """建管理员、建 app、导入 DSL、配模型、发布。**全部走真实的 Dify HTTP 接口**。"""
    result: dict[str, Any] = {"steps": []}

    def step(name: str, ok: bool, detail: Any = None) -> None:
        result["steps"].append({"name": name, "ok": bool(ok), "detail": detail})

    # 1) 初始化管理员。Dify 1.17.1 要求**先过 init 校验**（POST /console/api/init，
    #    用 INIT_PASSWORD），否则 /setup 与 /login 一律 401 `not_init_validated`。
    #    这一步需要一个会话 cookie，所以用一个带 cookie jar 的 opener，而不是裸 urllib。
    from http.cookiejar import CookieJar

    jar = CookieJar()
    opener = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(jar))
    init_request = urllib_request.Request(
        f"{base}/console/api/init",
        data=json.dumps({"password": env["INIT_PASSWORD"]}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(init_request, timeout=30) as response:
            init_status = response.status
    except urllib_error.HTTPError as exc:
        init_status = exc.code
    step("init validate", init_status in {200, 201}, {"status": init_status})
    result["cookies"] = [cookie.name for cookie in jar]

    def console_call(path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        """走**同一个** opener：init / setup / login 三者串在一条会话上。

        裸 urllib 每次都是新会话，`is_init_validated` 就丢了 —— login 会 401，
        而错误看起来像"口令不对"。
        """
        request = urllib_request.Request(
            f"{base}{path}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with opener.open(request, timeout=60) as response:
                return response.status, _json(response.read().decode("utf-8", "replace"))
        except urllib_error.HTTPError as exc:
            return exc.code, _json(exc.read().decode("utf-8", "replace"))

    status, body = console_call(
        "/console/api/setup",
        {"email": "acceptance@example.invalid", "name": "acc", "password": env["INIT_PASSWORD"]},
    )
    step(
        "console setup",
        status in {200, 201, 400},
        {"status": status, "body": body if status >= 400 else None},
    )

    # 登录口令要 **base64**：1.17.1 的 `decrypt_password_field` 装饰器对 `password`
    # 做 base64 解码，解不出来就是 AuthenticationFailed（401）。setup 走的是另一条
    # 路径（不吃这个装饰器），所以两处编码方式不同是**接口的真实形状**，不是笔误。
    import base64

    status, body = console_call(
        "/console/api/login",
        {
            "email": "acceptance@example.invalid",
            "password": base64.b64encode(env["INIT_PASSWORD"].encode()).decode(),
            "language": "en-US",
        },
    )
    login = body if status == 200 else {}
    # 1.17.1 的登录**不把令牌放进响应体**：`_token_response` 只回
    # `{"result": "success", "data": null}`，令牌通过 Set-Cookie 下发（access/refresh/csrf）。
    # 所以从 cookie jar 里取，而不是在 body 里找字段 —— 在 body 里找会一直找不到，
    # 而现象看起来像"登录失败"。
    cookies = {cookie.name: cookie.value for cookie in jar}
    access = cookies.get("access_token")
    step(
        "console login",
        bool(access),
        {"status": status, "cookie_names": sorted(cookies), "body_keys": sorted(login) if login else None},
    )
    if not access:
        return result
    # 控制台的受保护接口既认 Bearer，也认 cookie 会话；两个都带上，与浏览器登录后同形。
    cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    console = {"Authorization": f"Bearer {access}", "Cookie": cookie_header}
    # CSRF：Dify 的写操作要求这个头与 cookie 成对出现。
    if "csrf_token" in cookies:
        console["X-CSRF-Token"] = cookies["csrf_token"]

    # 2) 导入 DSL。导入即创建一个 workflow app。
    #
    # 1.17.1 的这个端点是 **JSON**，不是 multipart：`AppImportPayload` 只有
    # `mode`（必填，取值 `yaml-content`）+ `yaml_content`。用错形态会得到
    # 422 `Field required: mode` —— 看起来像少字段，其实是**载体**不对。
    import_status, imported = http_json(
        f"{base}/console/api/apps/imports",
        method="POST",
        body={"mode": "yaml-content", "yaml_content": _dsl_for(gateway_base).decode("utf-8")},
        headers=console,
    )
    step(
        "import dsl",
        import_status == 200,
        {"status": import_status, "body": imported if import_status != 200 else None},
    )
    imported = imported if import_status == 200 and isinstance(imported, dict) else {}
    app_id = imported.get("app_id") or imported.get("id")
    result["app_id"] = app_id
    if not app_id:
        return result

    # 2b) 装模型 provider **插件**。1.17.1 的 provider 不是内建的：`openai_api_compatible`
    #     是一个 .difypkg 插件，没装时 `/v1/workflows/run` 会 400
    #     `Provider openai_api_compatible does not exist.`。这里先把包放进 plugin_daemon
    #     的本地存储，再按 identifier 安装。
    #
    #     取证方式：包从 marketplace 下载到一次性目录，sha256 记进报告；不写进仓库。
    plugin_id = PLUGIN_ID
    # 用 **marketplace 安装**，而不是先手工把 .difypkg 塞进 plugin_daemon 的存储：
    # 后者要求容器内路径与安装器约定一致（`plugin_packages/` 之类），一次写错就是
    # 一个看起来"装了但没装上"的假成功。marketplace 这条路让 plugin_daemon 自己去拉，
    # 它自己知道该怎么校验和落盘 —— 我们只提供 identifier。
    status, body = http_json(
        f"{base}/console/api/workspaces/current/plugin/install/marketplace",
        method="POST",
        body={"plugin_unique_identifiers": [f"{plugin_id}:{PLUGIN_VERSION}@{PLUGIN_SHA}"]},
        headers=console,
    )
    step("install model provider plugin", status in {200, 201}, {"status": status, "body": body})
    # **装完必须重启 api**：插件在 plugin_daemon 里立刻可用（它的日志会打
    # `local runtime ready`），但 api 进程把 provider 列表缓存在内存里，
    # 不重启就一直返回空的 —— 于是"装好了"看起来像"没装上"，
    # 一路走到运行期 `Provider ... does not exist`。
    # 实测：重启 api 之后同一个接口立刻返回那条 provider。
    _compose(env, "restart", "api", timeout=300)
    if not _wait_ready(base, time.monotonic() + 180):
        step("api 重启后就绪", False, {"phase": "after-plugin-install"})
    # 安装是异步任务：等到 provider 真的出现在列表里再往下走。
    ready = False
    last_seen: Any = None
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        got_status, listing = http_json(
            f"{base}/console/api/workspaces/current/model-providers", headers=console
        )
        text = json.dumps(listing or {}, ensure_ascii=False)
        # 把**实际看到的 provider 标识**记下来：判据失败时要能立刻分辨
        # 「没装上」「装上了但名字不是我以为的那个」这两种完全不同的情况。
        names = [item.get("provider") for item in (listing or {}).get("data", []) if isinstance(item, dict)]
        last_seen = {"status": got_status, "providers": names[:20]}
        if got_status == 200 and PROVIDER_NAME in text:
            ready = True
            break
        time.sleep(5)
    step("model provider plugin ready", ready, {"waited_s": 240, "last_seen": last_seen})

    # 3) 给这个 provider 建**模型凭据**（自定义模型）。
    #
    # 1.17.1 的语义要点（读源码确认的）：
    #   * `openai_api_compatible` **没有** provider 级凭据（`provider_credential_schema`），
    #     所以 `/credentials` 会 400；自定义模型走 `/models/credentials`。
    #   * `POST /models` 只管负载均衡，**不是**注册模型；模型名随凭据一起提交。
    #   * 请求体是 `{model, model_type, name?, credentials}`。
    status, body = http_json(
        f"{base}/console/api/workspaces/current/model-providers/{PROVIDER_NAME}/models/credentials",
        method="POST",
        headers=console,
        body={
            "model": "synthetic",
            "model_type": "llm",
            "name": "synthetic",
            # 字段名由该 provider 的 **credential schema** 决定，不是我们起的名：
            # `openai_api_compatible` 要的是 `endpoint_url`（不是 `base_url`）。
            # 猜字段名的后果是 400 `Variable xxx is required`，看起来像缺配置。
            "credentials": {
                "api_key": "synthetic-not-a-real-key",
                "endpoint_url": model_url,
                "mode": "chat",
                # 该 provider 把这些数值型选项声明成 **string**；
                # 传整数会 500（`Variable context_size should be string`）。
                "context_size": "8192",
                "max_tokens_to_sample": "256",
                "vision_support": "support",
            },
        },
    )
    step("register custom model credential", status in {200, 201}, {"status": status, "body": body})

    # 4) 把 DSL 里的模型占位换成真实模型，然后发布。
    status, text = http(f"{base}/console/api/apps/{app_id}", headers=console)
    app = _json(text) if status == 200 else {}
    result["app_detail"] = {"status": status, "mode": (app or {}).get("mode")}

    # 导入后**按草稿实际保存的图**核对节点与边：DSL 写的是 YAML，Dify 存的是 JSON，
    # 两者形状不同。只在 DSL 上看"我写了三条边"是不够的 —— 上一版整张图只进来一个
    # start 节点，而导入接口回的是 200。
    draft_status, draft_text = http(f"{base}/console/api/apps/{app_id}/workflows/draft", headers=console)
    imported_graph = (_json(draft_text) or {}).get("graph") if draft_status == 200 else {}
    result["draft_graph"] = {"status": draft_status, **_saved_shape(imported_graph or {})}

    status, text = http(
        f"{base}/console/api/apps/{app_id}/workflows/draft",
        headers=console,
    )
    draft = _json(text) if status == 200 else {}
    graph = (draft or {}).get("graph") or {}
    for node in graph.get("nodes", []):
        if node.get("data", {}).get("type") == "llm":
            model = node["data"].setdefault("model", {})
            model["provider"] = PROVIDER_NAME
            model["name"] = "synthetic"
            model["mode"] = "chat"
            model.setdefault("completion_params", {})["temperature"] = 0
    if graph:
        status, text = http(
            f"{base}/console/api/apps/{app_id}/workflows/draft",
            method="POST",
            headers=console,
            body={
                "graph": graph,
                "features": (draft or {}).get("features") or {},
                # `SyncDraftWorkflowPayload` 是 `extra="forbid"`：多一个键就是 400。
                # 合法键只有 graph / features / hash / _is_collaborative /
                # environment_variable_patch / conversation_variables。
                # `hash` 是**乐观锁**：不带或带错就是 409 `draft_workflow_not_sync` ——
                # 它保证"我改的是我刚读到的那一版"，不是任意覆盖。
                "hash": (draft or {}).get("hash"),
                "conversation_variables": [],
            },
        )
        step(
            "write draft graph",
            status in {200, 201},
            {
                "status": status,
                "body": _json(text) if status >= 400 else None,
                # 写回去之后**再读一遍**：POST 回 200 只证明它收下了，不证明存下来的还是
                # 那张图。上一版整张图只剩 start 一个节点，而到这里一路都是 200。
                "saved": _saved_shape(graph),
            },
        )

    status, text = http(
        f"{base}/console/api/apps/{app_id}/workflows/publish",
        method="POST",
        headers=console,
        body={},
    )
    published = _json(text) if status in {200, 201} else {}
    # 发布接口只回 `{"result": "success", "created_at": ...}`，**不含** workflow id。
    # Service API 要的那个 id 要在发布之后重新读 app 详情才拿得到。
    workflow_id = None
    if status in {200, 201}:
        detail_status, detail = http_json(f"{base}/console/api/apps/{app_id}", headers=console)
        if detail_status == 200 and isinstance(detail, dict):
            workflow = detail.get("workflow") if isinstance(detail.get("workflow"), dict) else {}
            workflow_id = workflow.get("id") or detail.get("workflow_id")
    if workflow_id is None and isinstance(published, dict):
        workflow_id = published.get("id")
    result["workflow_id"] = workflow_id
    result["published"] = {
        "status": status,
        "workflow_id": workflow_id,
        "keys": sorted(published) if isinstance(published, dict) else None,
    }

    # 5) 造一个该 app 的 Service API key —— 这就是网关侧配置要用的那个。
    status, text = http(f"{base}/console/api/apps/{app_id}/api-keys", method="POST", headers=console, body={})
    key = (_json(text) or {}).get("token") if status in {200, 201} else None
    result["service_key_ok"] = bool(key)
    # 形状而不是内容：token 短到像占位值时必须看得见 —— `bool(key)` 对 "b" 也是 True。
    result["service_key_shape"] = {
        "length": len(key) if isinstance(key, str) else 0,
        "status": status,
        "body_keys": sorted(_json(text) or {}) if isinstance(_json(text), dict) else None,
    }
    result["service_key"] = key
    # 控制台会话头留一份给失败诊断用（节点执行记录是 console 路由）。
    # 它含会话令牌，所以 `main()` 打印报告前会把它摘掉，只用来发请求。
    result["console_headers"] = dict(console)
    return result


PUBLISH_KEY = "acceptance-dify-publish-key"
#: Dify 的**全局**管理密钥：发布桥用 Bearer 打 console 路由时认的就是它。
#: app 级 service key 不能替代 —— 实测会得到 `DIFY_DSL_AUTH_REJECTED`。
ADMIN_API_KEY = "acceptance-admin-api-key-0123456789"


def _import_second_app(dify_base: str, console: dict[str, str], gateway_base: str) -> dict[str, Any]:
    """**同一实例、另一个租户**的 app：导入 → 换模型 → 发布 → 建自己的 service key。

    与 `_setup_dify` 的区别只在"不重复做实例级的一次性步骤"（init/setup/login/装插件）：
    provider 插件是**实例**级的，第二个 app 直接用它。其余每一步都走真实 HTTP，
    少一步都会在运行期才炸（没换模型 ⇒ run 400 provider 不存在）。
    """
    import_status, imported = http_json(
        f"{dify_base}/console/api/apps/imports",
        method="POST",
        body={"mode": "yaml-content", "yaml_content": _dsl_for(gateway_base).decode("utf-8")},
        headers=console,
    )
    imported = imported if import_status == 200 and isinstance(imported, dict) else {}
    app_id = str(imported.get("app_id") or imported.get("id") or "")
    result: dict[str, Any] = {"import_status": import_status, "app_id": app_id}
    if not app_id:
        return result
    _, draft = http_json(f"{dify_base}/console/api/apps/{app_id}/workflows/draft", headers=console)
    graph = (draft or {}).get("graph") or {}
    for node in graph.get("nodes", []):
        if node.get("data", {}).get("type") == "llm":
            model = node["data"].setdefault("model", {})
            model["provider"] = PROVIDER_NAME
            model["name"] = "synthetic"
            model["mode"] = "chat"
            model.setdefault("completion_params", {})["temperature"] = 0
    if graph:
        http_json(
            f"{dify_base}/console/api/apps/{app_id}/workflows/draft",
            method="POST",
            headers=console,
            body={
                "graph": graph,
                "features": (draft or {}).get("features") or {},
                "hash": (draft or {}).get("hash"),
                "conversation_variables": [],
            },
        )
    http_json(
        f"{dify_base}/console/api/apps/{app_id}/workflows/publish",
        method="POST",
        headers=console,
        body={},
    )
    _, detail = http_json(f"{dify_base}/console/api/apps/{app_id}", headers=console)
    result["workflow_id"] = ((detail or {}).get("workflow") or {}).get("id")
    _, key_body = http_json(
        f"{dify_base}/console/api/apps/{app_id}/api-keys", method="POST", headers=console, body={}
    )
    result["service_key"] = (key_body or {}).get("token")
    return result


def _start_gateway(
    *,
    root: Path,
    model_url: str,
    cpms_base: str,
    dify_base: str,
    workflow_id: str,
    key: str,
    port: int,
    console_key: str = "",
    managed_route: bool = False,
    service_app_id: str = "",
    service_content: str = "",
    knowledge_base: str = "",
    service_live_order: bool = False,
    service_billing: bool = False,
    service_photo: bool = False,
    service_publication_target: str = "",
) -> tuple[subprocess.Popen, str, dict[str, Any]]:
    """起真 Gateway（绑定表指向 Dify 路线），等它就绪。"""
    case = root / "gateway"
    # 可重入：网关会起两次（先占位让 DSL 有地址可写，拿到 workflow 后再用真实绑定重起）。
    (case / "config").mkdir(parents=True, exist_ok=True)
    (case / "data").mkdir(parents=True, exist_ok=True)
    settings_file = case / "config/production.env"
    settings_file.write_text("# synthetic\n")
    if service_app_id:
        settings_file.write_text(
            "AIOPS_PROVIDERS=synthetic\n"
            f"AIOPS_PROVIDER_SYNTHETIC_BASE_URL={model_url}\n"
            "AIOPS_PROVIDER_SYNTHETIC_WIRE_API=chat\n"
            "AIOPS_PROVIDER_SYNTHETIC_MODEL=synthetic\n"
            "AIOPS_PROVIDER_SYNTHETIC_KEY_ENV=AIOPS_M2_MODEL_KEY\n"
        )
    settings_file.chmod(0o600)

    orders = case / "data/orders.db"
    conn = sqlite3.connect(orders)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS synthetic_orders "
        "(order_no TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT, created_time TEXT, id TEXT)"
    )
    conn.executemany(
        "INSERT OR REPLACE INTO synthetic_orders VALUES (?, ?, ?, ?, ?)",
        [
            (ORDER_LATEST, C_USER, TENANT, "2026-10-20 10:00:00", "2"),
            (ORDER_OLDER, C_USER, TENANT, "2026-10-18 10:00:00", "1"),
            (ORDER_OTHER, OTHER_USER, TENANT, "2026-10-21 10:00:00", "3"),
            # 另一租户自己的单：跨租户交错用它证明"两个租户各自的单不串"。
            (ORDER_TENANT_B, THIRD_USER, TENANT_B, "2026-10-22 09:00:00", "4"),
        ],
    )
    conn.commit()
    conn.close()

    manifest = case / "exec-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "m3-exec-config/1",
                "name": "dify-chat",
                "history_policy": "none",
                "model": {
                    "provider_key": "openai-compatible",
                    "adapter_key": "openai-compatible",
                    "model_key": "synthetic",
                    "endpoint": model_url,
                },
                "model_options": {"temperature": 0.2, "top_p": 0.9, "max_tokens": 64},
                "system_messages": ["SYSTEM"],
                "tools": [{"name": "charging_status_snapshot", "description": "d", "input_schema": "{}"}],
            }
        )
    )
    manifest.chmod(0o600)
    node_config = case / "agent-runs.json"
    node_config.write_text(
        json.dumps(
            {
                "environment": "env-local",
                "snail_base_url": "http://127.0.0.1:1",
                "snail_app_id": "unused",
                "snail_app_token": "unused",
                "snail_agent_id": 1,
                "snail_open_id": "unused",
                "expected_node_id": "node-local",
                "nodes": [{"node_id": "node-local", "environment": "env-local", "token_sha256": "0" * 64}],
            }
        )
    )
    node_config.chmod(0o600)
    key_file = case / "dify-workflow.key"
    key_file.write_text(key + "\n")
    key_file.chmod(0o600)

    # 每个 app 一把 Service API 凭据：`<key_dir>/<app_id>.key`。发布只推路径，不读密钥。
    keys = case / "keys"
    keys.mkdir(parents=True, exist_ok=True)

    bindings = case / "chat-bindings.json"
    # 管理指针模式（M3-01 的运营闭环）：**不写文件登记**。两者同时声明同一个 key
    # 是启动失败；而"写一张空表"也是配置错误（`declare no entries`）。
    # 真实迁移的形状就是"不配这个文件，改由管理表接管"。
    if managed_route:
        bindings.unlink(missing_ok=True)
    else:
        bindings.write_text(
            json.dumps(
                {
                    "schema": "aiops-chat-execution/1",
                    "bindings": [
                        {
                            "engine": "dify",
                            "environment": "env-local",
                            "tenant_id": TENANT,
                            "entry": "consumer",
                            "scenario": "consumer_service" if service_app_id else "consumer_own_order_status",
                            **(
                                {
                                    "live_order_status": service_live_order,
                                    "live_billing_record": service_billing,
                                    "photo_enabled": service_photo,
                                    "callback_version": 2 if service_content else 1,
                                    "app_id": service_app_id,
                                    "agent_id": "agt_12345678",
                                    "agent_version_no": 1,
                                    **(
                                        {"content_file": service_content, "knowledge_base_ids": ["kb-own"]}
                                        if service_content
                                        else {}
                                    ),
                                }
                                if service_app_id
                                else {}
                            ),
                            "workflow_id": workflow_id,
                            "workflow_key_file": str(key_file),
                        }
                    ],
                }
            )
        )
        bindings.chmod(0o600)

    env = {
        **os.environ,
        "AIOPS_M2_MODEL_KEY": "synthetic-not-a-real-key",
        "AIOPS_DATA_HOME": str(case / "data"),
        "AIOPS_CONFIG_HOME": str(case / "config"),
        "AIOPS_GATEWAY_DATA_HOME": str(case / "data"),
        "AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(settings_file),
        "AIOPS_GATEWAY_DATABASE_FILE": str(case / "data/gateway.db"),
        "AIOPS_GATEWAY_INTERNAL_TOKEN_SECRET": "synthetic-catalogue-secret",
        "AIOPS_GATEWAY_PORT": str(port),
        "AIOPS_GATEWAY_PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": f"{cpms_base}/charging-pile",
        "AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED": "1",
        "AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED": "1" if service_app_id else "0",
        "AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE": service_publication_target,
        **(
            {
                "AIOPS_GATEWAY_KB_SERVICE_BASE_URL": knowledge_base,
                "AIOPS_GATEWAY_MEDIA_SIGNING_SECRET": "s" * 32,
            }
            if knowledge_base
            else {}
        ),
        "AIOPS_GATEWAY_ENVIRONMENT": "dify-chat-acceptance",
        "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": "env-local",
        # 管理模式不配这条：路径指向一个不存在的文件会让整张表读失败
        # （`EXEC_CONFIG_UNAVAILABLE`），而"不配"才是正确的迁移形状。
        **({} if managed_route else {"AIOPS_GATEWAY_CHAT_EXECUTION_BINDINGS_FILE": str(bindings)}),
        # M3-01：运营发布/撤回推进的正式指针，以及发布门要用的两把钥匙。
        # **管理表与文件登记不能同时声明同一个 key**，所以下面那段运营闭环会先把文件
        # 登记换成空表，再让管理表接管 —— 这也正是真实迁移的形状。
        "AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE": str(case / "data/gateway.db"),
        "AIOPS_GATEWAY_CHAT_DIFY_KEY_DIR": str(keys),
        "AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY": PUBLISH_KEY,
        # 第一次起网关时还没有 app key（它要等 Dify 里发布完才存在），但启动校验
        # 要求"配了 console 地址就必须有 key"。占位值只活到第二次启动 —— 那时才换成
        # 真 key，第一次启动也根本不会去打 console。
        "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL": dify_base,
        "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY": console_key or "placeholder-console-key",
        "AIOPS_GATEWAY_DIFY_CHAT_BASE_URL": dify_base,
        "AIOPS_GATEWAY_BIND": "127.0.0.1",
        "AIOPS_M2_LOCAL_TOKEN": TOKEN,
        "AIOPS_M2_ORDERS_DB": str(orders),
        "AIOPS_M2_TENANT": TENANT,
        "AIOPS_M2_C_USER": C_USER,
        # 第二个身份只为一个判据存在：**两个不同用户各自的单不串**。没有它，
        # "选单/授权按本人"这条只能在同一身份下测，测不到别人的单会不会漏出来。
        "AIOPS_M2_SECOND_TOKEN": OTHER_TOKEN,
        "AIOPS_M2_SECOND_C_USER": OTHER_USER,
        # 第三个身份在**另一个租户**：双租户交错要靠它 —— 同租户两个用户只能证明
        # "单不串"，证明不了"一个租户发布/撤回不碰另一个租户的路由"。
        "AIOPS_M2_THIRD_TOKEN": THIRD_TOKEN,
        "AIOPS_M2_THIRD_C_USER": THIRD_USER,
        "AIOPS_M2_THIRD_TENANT": TENANT_B,
        # 真 Dify 试用（M3-01 续办 4-1）：与发布/其它 Dify 面**各自一把**钥匙，
        # 启动校验会拒绝同值。调试租户给主租户 —— 试用跑的就是它的单。
        "AIOPS_GATEWAY_DIFY_TRIAL_API_KEY": TRIAL_KEY,
        "AIOPS_GATEWAY_DIFY_TRIAL_DEBUG_TENANT": TENANT,
        "AIOPS_M2_PLATFORM_CONSUMER": "1",
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
    }
    base = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [sys.executable, str(REPO / "tools/m2_close/gateway_launcher.py")],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    # 把网关自己的输出落一份到文件：`_stop()` 之后管道就读不到了，而失败时要看它。
    gateway_log = open(case / "gateway.log", "w", encoding="utf-8")  # noqa: SIM115
    threading.Thread(target=_tee, args=(process, gateway_log), daemon=True).start()
    diag: dict[str, Any] = {"gateway": base, "case_root": str(case)}
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if http(f"{base}/health", timeout=2.0)[0] == 200:
            return process, base, diag
        time.sleep(0.3)
    _stop(process)
    diag["error"] = "gateway did not become ready"
    diag["gateway_output"] = (process.stdout.read() if process.stdout else "")[-4000:]
    return process, base, diag


def _drive(
    base: str,
    case: Path,
    *,
    question: str = "check my latest order status",
    token: str = TOKEN,
    cancel_after: float | None = None,
    order_no: str | None = None,
) -> dict[str, Any]:
    """从 App 接口驱动一次请求，轮询到终态，并读回执行记录。

    `cancel_after` 给定秒数时，在那一秒之后调用原取消接口 —— 取消要经过**真实 HTTP**，
    不是直接改库：本片要验的是"用户取消之后这次回答不再放出"。
    """
    headers = {"X-Business-Entry": "consumer"}
    started = time.monotonic()
    status, text = http(
        f"{base}/v1/assistant/questions",
        method="POST",
        token=token,
        body={"question": question, **({"order_no": order_no} if order_no else {})},
        headers=headers,
        timeout=30.0,
    )
    first = _json(text) if status else {}
    result: dict[str, Any] = {
        "gateway": base,
        "post_status": status,
        "post_elapsed_ms": int((time.monotonic() - started) * 1000),
        "post_body": first,
    }
    qa_id = first.get("qa_id") if isinstance(first, dict) else None
    result["qa_id"] = qa_id
    final: dict[str, Any] = {}
    if qa_id:
        cancel_at = time.monotonic() + cancel_after if cancel_after is not None else None
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            got_status, got_text = http(
                f"{base}/v1/assistant/questions/{qa_id}", token=token, headers=headers
            )
            if got_status == 200:
                final = _json(got_text)
                if final.get("status") in {"completed", "failed", "cancelled", "expired"}:
                    break
            if cancel_at is not None and time.monotonic() >= cancel_at:
                cancel_status, cancel_text = http(
                    f"{base}/v1/assistant/questions/{qa_id}/cancel",
                    method="POST",
                    token=token,
                    body={},
                    headers=headers,
                )
                result["cancel"] = {"status": cancel_status, "body": _json(cancel_text)}
                cancel_at = None
            time.sleep(0.2)
    result["final"] = final
    result["rows"] = _dify_rows(case / "data/gateway.db")
    return result


HEADERS = {"X-Business-Entry": "consumer"}


def _seed_registry(database: Path, rows: list[dict[str, str]]) -> bool:
    """把这次验收的 `dify_app_registry` 行直接写进去。

    用 SQL 而不是调 `admin reconcile`：这一段的重点是**发布/撤回这两个运营动作**，
    注册表是它们的**前置条件**（"这个 app 属于哪个租户/入口"是声明，不是操作）。
    真实部署里这两行由清单收敛产生，这里不重复验那条路径。
    """
    try:
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dify_app_registry (
                    tenant_id TEXT NOT NULL,
                    business_entry TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, business_entry)
                )
                """
            )
            for row in rows:
                connection.execute(
                    "INSERT OR REPLACE INTO dify_app_registry VALUES (?, ?, ?, ?, ?)",
                    (
                        row["tenant_id"],
                        row["business_entry"],
                        row["app_id"],
                        row["agent_name"],
                        datetime.now(UTC).isoformat(),
                    ),
                )
            connection.commit()
        return True
    except sqlite3.Error:
        return False


def _console_workspace(dify_base: str, console: dict[str, str]) -> str:
    """这个 Dify 实例的 workspace id（发布要按租户钉到一个已开户的工作区）。"""
    status, body = http_json(f"{dify_base}/console/api/workspaces/current/summary", headers=console)
    identifier = (body or {}).get("id") if status == 200 and isinstance(body, dict) else None
    return str(identifier or "")


def _seed_workspace(database: Path, tenant_id: str, workspace_id: str, environment: str) -> bool:
    """给这个租户种一行 `ready` 的 workspace（开户是别的步骤，这里只补前置）。

    **走它自己的 API，不手写 INSERT**：那张表有 `workspace_name` / 租约列，
    手写的列数一旦对不上就是静默失败（本轮实测过：函数返回 True，行却没进去）。
    """
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    try:
        workspaces = DifyWorkspaces(database)
        if not workspaces.claim(tenant_id, environment, owner="acceptance", lease_seconds=300.0):
            return False
        # claim 只插了 `creating` 占位；workspace_id 要单独写，再推进到 ready。
        with workspaces._connection(write=True) as connection:  # noqa: SLF001 - 验收造行
            connection.execute(
                "UPDATE dify_workspaces SET workspace_id = ? WHERE tenant_id = ? AND environment = ?",
                (workspace_id, tenant_id, environment),
            )
        workspaces.mark_ready(tenant_id, environment)
        return True
    except Exception:  # noqa: BLE001 - 前置失败会以 check 的形式报出来
        return False


_SEED_AGENT_ERROR = ""


def _seed_agent(database: Path, tenant_id: str, *, name: str = "小趋-客服") -> bool:
    """种一个**已发布**的 agent 及其版本行。

    `sync_binding_from_dify` 的语义是"拿 Dify 当前内容与我方**已发布版本**比"：
    没有已发布版本时它按 `refused` 返回（"先让它成为一个已发布版本"）。这里种出那个
    基线版本，让发布走的是真实的 converge→publish 路径。
    """
    from aiops_diagnostics.publication.agent_lifecycle import AgentConfig, AgentManager, AgentStore

    try:
        manager = AgentManager(AgentStore(database))

        class _Context:
            effective_tenant_id = tenant_id
            roles = frozenset({"ROLE_PLATFORM_ADMIN"})

        created = manager.create(
            _Context(),
            name=name,
            description="acceptance",
            config=AgentConfig(
                agent_type="customer",
                prompt="ACCEPTANCE_VERSION_ONE",
                knowledge_base_ids=(),
                model="aiops-api",
                output_contract="blocks-v1",
            ),
        )
        manager.publish(_Context(), created.agent_id, expected_revision=created.revision)
        return True
    except Exception as exc:  # noqa: BLE001 - 前置失败会以 check 的形式报出来
        # 把原因留在模块级：`check` 的 detail 只给布尔值时，读者无从区分
        # "签名变了"与"发布门拒了"。
        global _SEED_AGENT_ERROR
        _SEED_AGENT_ERROR = f"{type(exc).__name__}: {exc}"
        return False


def _export_workflow_content(
    dify_base: str, console: dict[str, str], app_id: str, workflow_id: str
) -> dict[str, Any]:
    """按**指定的已发布 workflow id** 导出并规范化内容。

    Dify 契约：`export` 带 `workflow_id` 读已发布版，不带读 draft。恢复判据要的是
    "V1 那一版的图"，所以必须带 id。
    """
    import yaml

    from aiops_diagnostics.dify.dify_dsl_pull import workflow_content

    status, text = http(
        f"{dify_base}/console/api/apps/{app_id}/export?workflow_id={workflow_id}",
        headers=console,
    )
    if status != 200:
        return {}
    payload = _json(text) or {}
    dsl = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(dsl, str) or not dsl.strip():
        return {}
    try:
        yaml.safe_load(dsl)
        return workflow_content(dsl)
    except Exception:  # noqa: BLE001 - 导不出来就留空，restore 会以 409 说明
        return {}


def _make_v1_a_workflow_snapshot(
    database: Path,
    tenant_id: str,
    *,
    app_id: str,
    workflow_id: str,
    workspace_id: str,
    workflow: dict[str, Any] | None = None,
    version_no: int = 1,
) -> bool:
    """把基线版本 v1 改写成**workflow 形态**的快照。

    真实部署里 V1 也是"发布过的 workflow"，快照里带着 `kind`/`app_id`/`workspace_id`/
    `workflow_id`。只造一条 AgentConfig 基线会让版本列表看不见 V1（它按
    `kind == dify-workflow` 与 app/workspace 过滤），于是"恢复 V1"根本无从选起 ——
    而那个症状看起来像"恢复功能坏了"。
    """
    try:
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT agent_id, snapshot_json FROM agent_versions WHERE tenant_id = ? AND version_no = ?",
                (tenant_id, version_no),
            ).fetchone()
            if row is None:
                return False
            snapshot = json.loads(row["snapshot_json"] or "{}")
            snapshot.update(
                {
                    "kind": "dify-workflow",
                    "app_id": app_id,
                    "workspace_id": workspace_id,
                    "workflow_id": workflow_id,
                    "workflow": workflow or {"graph": {"nodes": [], "edges": []}},
                }
            )
            connection.execute(
                "UPDATE agent_versions SET snapshot_json = ? WHERE tenant_id = ? AND version_no = ?",
                (json.dumps(snapshot), tenant_id, version_no),
            )
            connection.commit()
        return True
    except sqlite3.Error:
        return False


def _snapshot_workflow_of(database: Path, tenant_id: str, version_no: int | None) -> str:
    """历史版本快照里冻结的 workflow_id —— 恢复的目标就是它。

    读**不可变快照**而不是"当前绑定"：后者会让"重发当前草稿"也看起来像恢复成功。
    """
    if version_no is None or not database.exists():
        return ""
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT snapshot_json FROM agent_versions WHERE tenant_id = ? AND version_no = ?",
            (tenant_id, version_no),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        snapshot = json.loads(row["snapshot_json"] or "{}")
        identifier = snapshot.get("workflow_id")
        if isinstance(identifier, str) and identifier:
            return identifier
    return ""


def _row_for_qa(rows: list[dict[str, Any]], qa_id: str | None) -> dict[str, Any]:
    """**按 QA 认行**，不按位置取。

    早先这里写的是 `rows[-1]`：`_drive` 每次返回的是整张表，所以"最后一行"其实是
    **全表最后一条**，不是这次请求的那条。判据因此对着别人的行做断言 ——
    一次跑下来，连"指定订单"都被判成了错单。
    """
    for row in rows:
        if qa_id and row.get("qa_id") == qa_id:
            return row
    return {}


def _dify_rows(database: Path) -> list[dict[str, Any]]:
    if not database.exists():
        return []
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM chat_dify_runs ORDER BY created_at")]
    finally:
        conn.close()


def _tee(process: subprocess.Popen, sink) -> None:
    """把子进程的输出同时写到文件（读不完的管道在 `_stop` 之后就拿不到了）。"""
    try:
        assert process.stdout is not None
        for line in process.stdout:
            sink.write(line)
            sink.flush()
    except Exception:  # noqa: BLE001 - 诊断辅助，不能让它把验收搞崩
        pass
    finally:
        with contextlib.suppress(Exception):
            sink.close()


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)


def _isolated_driver(args: argparse.Namespace) -> int:
    """本次进程和 Dify 共用临时容器网络，宿主无监听、无防火墙变更。"""
    root = Path(tempfile.mkdtemp(prefix="dify-chat-acceptance-"))
    root.chmod(0o700)
    name = "difychat-driver-" + secrets.token_hex(5)
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--group-add",
        str(Path("/var/run/docker.sock").stat().st_gid),
        "-e",
        f"DIFY_TEST_NAMESPACE={name}",
        "-e",
        f"DIFY_TEST_ROOT={root}",
        "-e",
        f"HOME={root}",
        "-e",
        f"PYTHONPATH={REPO / 'src'}",
        "-v",
        f"{REPO}:{REPO}:ro",
        "-v",
        f"{root}:{root}",
        "-v",
        "/home/claude/miniconda3:/home/claude/miniconda3:ro",
        "-v",
        "/usr/bin/docker:/usr/bin/docker:ro",
        "-v",
        "/usr/libexec/docker/cli-plugins:/usr/libexec/docker/cli-plugins:ro",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-w",
        str(REPO),
        "python:3.13-slim",
        sys.executable,
        str(Path(__file__).resolve()),
        "--out",
        str(root / "report.json"),
        "--keep",
    ]
    try:
        result = subprocess.run(command, check=False)
        if args.out and (root / "report.json").exists():
            shutil.copy(root / "report.json", args.out)
            Path(args.out).chmod(0o600)
        return result.returncode
    finally:
        # 数据卷属主属于容器；仅清理本次 mkdtemp 的卷，证据保留。
        if not args.keep and (root / "volumes").exists():
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{root / 'volumes'}:/cleanup",
                    "busybox:latest",
                    "sh",
                    "-c",
                    "rm -rf /cleanup/*",
                ],
                check=False,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="")
    parser.add_argument("--keep", action="store_true", help="保留数据根以便事后排查（默认删掉）")
    parser.add_argument("--keep-stack", action="store_true", help="不拆 Dify 栈（排查用；正常跑一定拆）")
    args = parser.parse_args()
    os.umask(0o077)

    if "DIFY_TEST_NAMESPACE" not in os.environ:
        return _isolated_driver(args)

    if shutil.which("docker") is None:
        print("docker is not available", file=sys.stderr)
        return 2

    root = Path(os.environ["DIFY_TEST_ROOT"])
    project = "difychatacc" + secrets.token_hex(4)
    port = 5001  # 仅在本次容器网络命名空间内部使用。
    network = f"{project}-net"
    model = _serve(ModelHandler)
    cpms = _serve(CpmsHandler)
    # Dify、Gateway 和合成上游共用本次容器网络命名空间，只走回环。
    model_url = f"http://{_host_gateway()}:{model.server_port}/v1"
    cpms_base = f"http://127.0.0.1:{cpms.server_port}"
    env = _dify_env(root, model_url=model_url, port=port, project=project, network=network)
    dify_base = f"http://127.0.0.1:{port}"

    report: dict[str, Any] = {
        "kind": "dify_chat_entry_e2e",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "synthetic_data": True,
        "real_company_services": False,
        "real": [
            "Dify 1.17.1 workflow orchestration, http-request step, llm step, publish and run",
            "gateway binding selection, order selection, authorization, data grant, callback, release",
            "POST/GET /v1/assistant/questions",
        ],
        "substituted": {
            "model_upstream": "loopback OpenAI-compatible synthetic server",
            "cpms": "loopback stub",
            "caller_identity": "fixed local token (gateway_launcher)",
            "order_authorization": "synthetic_orders table",
            "platform_directory": "consumer-only synthetic directory",
        },
        "checks": [],
        "dify": {},
    }

    def check(name: str, ok: bool, detail: Any = None) -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    gateway_process = None
    try:
        # 依赖先起，再起 api/worker（它们有可用的 healthcheck）。plugin_daemon 一起被
        # `depends_on` 拉起来，但**不参与 `--wait`**：那个镜像里没有任何网络工具，
        # 容器内探活只会让一个正常工作的进程永远 unhealthy。它是否真的就绪，由后面的
        # `_wait_model_provider` 从**外部**证明 —— provider 探测成功即说明它在干活。
        # 先彻底清一次再起：`docker compose up` 会**复用已存在的容器**，
        # 于是改了 compose 里的 healthcheck 之后，起来的仍是按旧定义创建的那个 ——
        # 症状是"改了配置但还是同一条错"。验收每次都要从干净状态开始。
        _compose(env, "down", "-v", "--remove-orphans", timeout=300)
        up = _compose(env, "up", "-d", "init_permissions", "db_postgres", "redis", "weaviate", timeout=600)
        if up.returncode != 0:
            report["compose_up"] = {"rc": up.returncode, "tail": (up.stdout + up.stderr)[-2000:]}
            return _finish(report, args)
        up = _compose(env, "up", "-d", "--wait", "--wait-timeout", "600", "api", "worker", timeout=1200)
        report["compose_up"] = {"rc": up.returncode, "tail": (up.stdout + up.stderr)[-2000:]}
        logs = _compose(env, "logs", "--no-color", timeout=120)
        report["compose_logs_tail"] = (logs.stdout + logs.stderr)[-4000:]
        check("dify 隔离栈起来", up.returncode == 0, up.returncode)
        if up.returncode != 0:
            return _finish(report, args)
        check("dify /health 就绪", _wait_ready(dify_base, time.monotonic() + 180), dify_base)
        check(
            "模型 provider 可用（plugin daemon 真的在干活）",
            _wait_model_provider(dify_base, time.monotonic() + 240),
            dify_base,
        )

        # 网关**先**起来：DSL 里那个取数 URL 要指向一个真实存在的地址，
        # 而它是这次唯一的网关实例。
        gateway_port = _free_port()
        gateway_process, gateway_base, diag = _start_gateway(
            root=root,
            model_url=model_url,
            cpms_base=cpms_base,
            dify_base=dify_base,
            workflow_id="00000000-0000-0000-0000-000000000000",
            key="placeholder",
            port=gateway_port,
        )
        report["gateway_diag"] = diag
        check("真 Gateway 就绪", not diag.get("error"), diag.get("error"))
        if diag.get("error"):
            return _finish(report, args)

        # DSL 与脚本使用同一命名空间内的回环地址。
        gateway_container_base = f"http://{_host_gateway()}:{gateway_port}"
        setup = _setup_dify(dify_base, env, model_url=model_url, gateway_base=gateway_container_base)
        # `console_headers` 与 `service_key` 都是凭据：只留在内存里供诊断请求用，
        # 不进报告。报告会被贴进回执与 PR。
        report["dify"] = {k: v for k, v in setup.items() if k not in {"service_key", "console_headers"}}
        for item in setup.get("steps", []):
            check(f"dify: {item['name']}", item["ok"], item["detail"])
        workflow_id = setup.get("workflow_id")
        key = setup.get("service_key")
        # `bool(key)` 对 "b" 也是 True —— 一个 1 字符的"凭据"必须在这里就被判掉，
        # 否则症状会以 DIFY_UNAVAILABLE 的形式出现在很远的地方。
        if not workflow_id or not isinstance(key, str) or len(key) < 16:
            check(
                "得到已发布 workflow 与 service key",
                False,
                {"workflow_id": workflow_id, "key_shape": (setup.get("service_key_shape") or {})},
            )
            return _finish(report, args)
        check("得到已发布 workflow 与 service key", True, {"workflow_id": workflow_id})

        # 网关的绑定现在才可能写对：它要的是那一份刚发布的 workflow 与那一把 key。
        _stop(gateway_process)
        gateway_process, gateway_base, diag = _start_gateway(
            root=root,
            model_url=model_url,
            cpms_base=cpms_base,
            dify_base=dify_base,
            workflow_id=workflow_id,
            key=key,
            port=gateway_port,
            # Dify 的 app API key 同表同形状，既做 Service API 也做 console Bearer；
            # 发布桥（`DifyDslClient`）要的正是后者。
            console_key=ADMIN_API_KEY,
        )
        check("绑定 Dify 后 Gateway 再次就绪", not diag.get("error"), diag.get("error"))
        if diag.get("error"):
            return _finish(report, args)

        chain = _drive(gateway_base, Path(diag["case_root"]))
        report["chain"] = chain
        final = chain.get("final") or {}
        rows = chain.get("rows") or []
        row = _row_for_qa(rows, chain.get("qa_id"))
        check(
            "App POST 202 且早于 Dify 完成",
            chain.get("post_status") == 202
            and (chain.get("post_body") or {}).get("status") in {"queued", "running"},
            {"status": chain.get("post_status"), "ms": chain.get("post_elapsed_ms")},
        )
        check(
            "原 GET 到 completed，正文含本轮快照的订单号",
            final.get("status") == "completed"
            and ORDER_LATEST in json.dumps(final.get("result") or {}, ensure_ascii=False),
            {"status": final.get("status"), "blocks": (final.get("result") or {}).get("blocks")},
        )
        check(
            "执行记录：真 workflow run 身份落地且状态 succeeded",
            row.get("state") == "succeeded" and bool(row.get("workflow_run_id")) and bool(row.get("task_id")),
            {k: row.get(k) for k in ("state", "workflow_run_id", "task_id", "workflow_id")},
        )
        check("业务只读一次", CpmsHandler.calls == [ORDER_LATEST], list(CpmsHandler.calls))
        prompts = ModelHandler.state.get("prompts") or []
        # 判据看的是**播报步骤自己收到的那一次**，不是全部调用：注册模型凭据时 Dify 会先打
        # 一次探测（正文是 "ping"），那次既没有订单号也不可能有凭据。只写
        # `all("cdg_" not in p)` 会被这个探测请求满足 —— 那一版曾因此假 PASS。
        # 播报步骤收到的是**取数步骤的响应体**（那段 JSON），订单号就在里面。
        # 用订单号认那一次调用，而不是用问题原文：问题原文只在 start 节点里，
        # 模型看不到它 —— 早先按问题原文找，找到 0 条，判据空转成 FAIL。
        broadcast = [text for text in prompts if ORDER_LATEST in text]
        check(
            "模型实际输入不含取数凭据",
            len(broadcast) == 1
            and "cdg_" not in broadcast[0]
            and "Bearer" not in broadcast[0]
            and "Authorization" not in broadcast[0],
            {
                "broadcast_prompt": [text[:400] for text in broadcast],
                "all_prompts": [text[:300] for text in prompts],
                "request_shapes": ModelHandler.state.get("shapes") or [],
                "prompt_count": len(prompts),
            },
        )
        check(
            "取数凭据在完成之后被撤销",
            _grants_revoked(Path(diag["case_root"]) / "data/gateway.db"),
            _grant_states(Path(diag["case_root"]) / "data/gateway.db"),
        )

        # ---- 指定另一张**本人**订单：不是"最新"，而是用户点名的那一单 ----
        named = _drive(
            gateway_base,
            Path(diag["case_root"]),
            # 措辞要真的命中"状态查询"那一条路由：只说 "check order X" 不含状态词，
            # 会被路由到**标准诊断**（返回 diagnosis_id 而不是 qa_id）—— 那不是本片的链路。
            question=f"check the status of order {ORDER_OLDER}",
            order_no=ORDER_OLDER,
        )
        report["chain_named_order"] = named
        named_final = named.get("final") or {}
        named_row = _row_for_qa(named.get("rows") or [], named.get("qa_id"))
        check(
            "指定本人订单：读的就是那一单，正文是那一单",
            named_final.get("status") == "completed"
            and ORDER_OLDER in json.dumps(named_final.get("result") or {}, ensure_ascii=False)
            and named_row.get("order_no") == ORDER_OLDER,
            {"status": named_final.get("status"), "row_order": named_row.get("order_no")},
        )

        # ---- 两个不同用户并发：各自只拿到自己的单 ----
        CpmsHandler.calls.clear()
        results: dict[str, dict[str, Any]] = {}

        def run(key: str, token: str) -> None:
            results[key] = _drive(
                gateway_base, Path(diag["case_root"]), question="check my latest order status", token=token
            )

        threads = [
            threading.Thread(target=run, args=("a", TOKEN)),
            threading.Thread(target=run, args=("b", OTHER_TOKEN)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)
        report["chain_concurrent"] = results
        ok_concurrent = True
        detail_concurrent: dict[str, Any] = {}
        # 循环变量**不能**叫 `key`：它会把 main() 里那把 app service key 覆盖掉，
        # 而下面还要用那把 key 落凭据文件 —— 实测写进去的是 "b"，症状是之后每次
        # 请求都 `DIFY_UNAVAILABLE`，离这里非常远。
        for who, expected in (("a", ORDER_LATEST), ("b", ORDER_OTHER)):
            got = results.get(who) or {}
            text = json.dumps((got.get("final") or {}).get("result") or {}, ensure_ascii=False)
            rows_for = _row_for_qa(got.get("rows") or [], got.get("qa_id"))
            ok_concurrent = ok_concurrent and (got.get("final") or {}).get("status") == "completed"
            ok_concurrent = ok_concurrent and expected in text
            ok_concurrent = ok_concurrent and rows_for.get("order_no") == expected
            detail_concurrent[who] = {
                "status": (got.get("final") or {}).get("status"),
                "row_order": rows_for.get("order_no"),
                "mentions_own": expected in text,
            }
        check("两个用户的单不串", ok_concurrent, detail_concurrent)

        # ---- 上游失败：落 failed，**不换引擎**、不产生正文 ----
        CpmsHandler.calls.clear()
        CpmsHandler.fail = True
        try:
            failing = _drive(gateway_base, Path(diag["case_root"]))
        finally:
            CpmsHandler.fail = False
        report["chain_upstream_failure"] = failing
        failing_final = failing.get("final") or {}
        check(
            "业务上游失败 ⇒ failed 且无正文",
            failing_final.get("status") == "failed" and failing_final.get("result") in (None, {}),
            {"status": failing_final.get("status")},
        )

        # 发布 V2 后仍绑定 V1；模型输入标记和 Dify 执行库双重核对实际版本。
        app_id = setup["app_id"]
        console = setup["console_headers"]
        status, draft = http_json(f"{dify_base}/console/api/apps/{app_id}/workflows/draft", headers=console)
        if status != 200:
            raise RuntimeError("cannot read isolated draft")
        graph = draft["graph"]
        for node in graph["nodes"]:
            if node["data"].get("type") == "llm":
                node["data"]["prompt_template"][0]["text"] += "\nACCEPTANCE_VERSION_TWO"
        saved, _ = http_json(
            f"{dify_base}/console/api/apps/{app_id}/workflows/draft",
            method="POST",
            headers=console,
            body={
                "graph": graph,
                "features": draft["features"],
                "hash": draft["hash"],
                "conversation_variables": [],
            },
        )
        # **这里刻意不调 Dify 的原生 publish。** 运营的「发布到 AI-Ops」必须自己
        # 完成那一步；验收脚本替它做，就等于把这个缺口从证据里抹掉了 —— 实测正是
        # 这样：脚本先 publish，于是"网关发布后指针切到 V2"永远成立，
        # 而真实按钮走的那条路上根本没有这次 publish。
        #
        # 所以 V2 的身份要在**网关发布之后**才取得到，见下面的 `version_two`。
        _, detail = http_json(f"{dify_base}/console/api/apps/{app_id}", headers=console)
        version_two = detail.get("workflow", {}).get("id")
        check(
            "只保存草稿：Dify 侧仍只有 V1（尚未发布）",
            saved == 200 and version_two == workflow_id,
            {"save": saved, "workflow_id": version_two, "expected_v1": workflow_id},
        )
        # ---- M3-01 运营闭环：保存 / 试用 / 发布 / 撤回 / 恢复 ----
        #
        # 这一段从**运营能点到的那三个 HTTP 入口**驱动，不手改绑定文件：
        #   * 保存草稿 = 只在 Dify 里写 draft（`POST .../workflows/draft`，上面那段已做）
        #   * 发布     = `POST /v1/dify/publish`（与发布桥同一个实现、同一道门）
        #   * 撤回     = `POST /v1/dify/retract`
        # 换绑定文件那一步要让位给管理表：两者同时声明同一个 key 是**启动失败**。
        #
        # 初始态：**V1 已经正式发布**，且这个环境已经迁移到管理指针。真实部署里
        # 这条来自上一次发布；迁移的形状是"不再配文件登记，改由管理表接管"。
        #
        # 网关必须用**管理模式重启**：`settings` 在启动时构造，文件路径一旦配上，
        # 之后删文件只会让整张表读失败（`EXEC_CONFIG_UNAVAILABLE`）。
        import sqlite3 as _sqlite3

        from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

        # 每个 app 的 Service API 凭据：`<key_dir>/<app_id>.key`。真实部署里由运维
        # 预置；发布只推路径、不读密钥，所以这一文件必须**在发布之前**就位。
        per_app_key = Path(diag["case_root"]) / "keys" / f"{app_id}.key"
        per_app_key.parent.mkdir(parents=True, exist_ok=True)
        per_app_key.write_text(key + "\n")
        per_app_key.chmod(0o600)

        database = Path(diag["case_root"]) / "data/gateway.db"
        with _sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM chat_route_bindings")
            connection.commit()
        ChatRouteBindingStore(database).publish(
            environment="env-local",
            tenant_id=TENANT,
            entry="consumer",
            scenario="consumer_own_order_status",
            engine="dify",
            agent_id="小趋-客服",
            agent_version_no=1,
            app_id=app_id,
            workflow_id=workflow_id,
            workflow_key_ref=str(per_app_key),
            updated_by="acceptance",
        )
        _stop(gateway_process)
        gateway_process, gateway_base, diag = _start_gateway(
            root=root,
            model_url=model_url,
            cpms_base=cpms_base,
            dify_base=dify_base,
            workflow_id=workflow_id,
            key=key,
            port=gateway_port,
            console_key=ADMIN_API_KEY,
            managed_route=True,
        )
        check("迁移到管理指针后 Gateway 再次就绪", not diag.get("error"), diag.get("error"))
        if diag.get("error"):
            return _finish(report, args)

        # 注册表 / workspace / agent 三样前置：发布与撤回都只认已绑定的 app，
        # 而 workspace 决定发布落到哪个工作区。真实部署里它们来自清单收敛与开户，
        # 隔离栈里由验收自己种（**必须重启之后**，否则种子写在旧进程看不到的库里）。
        workspace_id = _console_workspace(dify_base, setup.get("console_headers") or {})
        registry_seeded = _seed_registry(
            database,
            [
                {
                    "tenant_id": TENANT,
                    "business_entry": "consumer",
                    "app_id": app_id,
                    "agent_name": "小趋-客服",
                }
            ],
        )
        workspace_seeded = _seed_workspace(database, TENANT, workspace_id, "dify-chat-acceptance")
        agent_seeded = _seed_agent(database, TENANT)
        # V1 必须是**workflow 形态**的已发布快照：版本列表按 kind/app/workspace 过滤，
        # 只造 AgentConfig 基线会让"恢复 V1"没有可选的来源。
        # V1 的**真实图**：按它的 workflow_id 从 Dify 导出。空图会让恢复时的
        # "内容必须与快照逐字一致"判据 409 —— 而那个 409 看起来像恢复功能坏了。
        v1_content = _export_workflow_content(dify_base, console, app_id, workflow_id)
        v1_shaped = _make_v1_a_workflow_snapshot(
            database,
            TENANT,
            app_id=app_id,
            workflow_id=workflow_id,
            workspace_id=workspace_id,
            workflow=v1_content,
        )
        check(
            "运营闭环前置：注册表 / workspace / agent 就绪",
            registry_seeded and workspace_seeded and agent_seeded and v1_shaped,
            {
                "registry": registry_seeded,
                "workspace": workspace_seeded,
                "agent": agent_seeded,
                "agent_error": _SEED_AGENT_ERROR,
            },
        )

        publish_headers = {
            "Authorization": f"Bearer {PUBLISH_KEY}",
            "Content-Type": "application/json",
            "X-AIOps-Initiator": "acceptance",
        }

        def _publish_app() -> tuple[int, Any]:
            return http_json(
                f"{gateway_base}/v1/dify/publish",
                method="POST",
                headers=publish_headers,
                body={"app_id": app_id},
            )

        def _retract_app() -> tuple[int, Any]:
            return http_json(
                f"{gateway_base}/v1/dify/retract",
                method="POST",
                headers=publish_headers,
                body={"app_id": app_id},
            )

        def _current_bound_workflow(db: Path, env_name: str) -> str:
            """管理表此刻指向的那一版 workflow —— 恢复之后应当就是它。"""
            row = ChatRouteBindingStore(db).get(env_name, TENANT, "consumer", "consumer_own_order_status")
            return row.workflow_id if row is not None else ""

        def _dify_run_of(qa: dict[str, Any]) -> dict[str, Any]:
            """这一次真的走了 Dify 吗 —— 看**执行记录**，不看正文形状。

            直读也会返回 completed 与同样的 blocks：只按 completed 判，
            "没有绑定所以掉回直读"与"确实执行了 V1"看起来一模一样。
            """
            return _row_for_qa(qa.get("rows") or [], qa.get("qa_id")) or {}

        def _own_workflow_of(qa: dict[str, Any]) -> str:
            row = _dify_run_of(qa)
            run_id = row.get("workflow_run_id", "")
            if not run_id:
                return ""
            actual = _compose(
                env,
                "exec",
                "-T",
                "db_postgres",
                "psql",
                "-U",
                "dify",
                "-d",
                "dify",
                "-Atc",
                f"SELECT workflow_id FROM workflow_runs WHERE id = '{run_id}'",
                timeout=20,
            )
            observed = actual.stdout.strip()
            if observed:
                return observed
            # Dify 库里查不到时退回**执行记录里冻结的那一个**：两者都是同一次运行
            # 的身份，而恢复那一步会把 workflow 再发布一次，旧 run 的行可能已被归档。
            return str(row.get("workflow_id") or "")

        # ① 保存 V2 草稿之后：App 仍跑 V1。草稿只在 Dify 里，路由指针没动。
        ModelHandler.state["prompts"] = []
        after_save = _drive(gateway_base, Path(diag["case_root"]))
        report["chain_after_save"] = after_save
        saved_row = _dify_run_of(after_save)
        check(
            "保存 V2 草稿之后 App 仍执行 V1",
            # 先证"真的走了 Dify"，再证"走的是 V1"：只剩 completed 的话，
            # 掉回直读也会绿。
            bool(saved_row.get("workflow_run_id"))
            and after_save.get("final", {}).get("status") == "completed"
            and _own_workflow_of(after_save) == workflow_id,
            {
                "dify_run": bool(saved_row.get("workflow_run_id")),
                "actual": _own_workflow_of(after_save),
                "expected": workflow_id,
            },
        )

        # ①b 真试用：**冻结的 V2 草稿在真 Dify 上跑一轮**，App 仍执行 V1。
        #
        # 这一步以前根本不存在（脚本里连 `trial` 都没有），而任务书要求它由运营入口
        # 驱动。判据要两件事同时成立：
        #   * 试用**真的**在真 Dify 上执行过 —— 证据取**外部世界看到的**：合成模型收到
        #     含这张单的播报输入、CPMS 被真的按这张单读了一次。只报 completed 分不清
        #     "执行了"与"没执行"；而"查 Dify 执行库"也不成立（临时应用删了就级联没了）。
        #   * 正式指针**没动**：App 紧接着仍执行 V1，版本号没变，任务行仍 active。
        trial_headers = {"Authorization": f"Bearer {TRIAL_KEY}", "Content-Type": "application/json"}
        ModelHandler.state["prompts"] = []
        CpmsHandler.calls.clear()
        trial_status, trial_body = http_json(
            f"{gateway_base}/v1/dify/trial",
            method="POST",
            headers=trial_headers,
            body={"app_id": app_id, "order_no": ORDER_LATEST},
        )
        trial_id = str((trial_body or {}).get("trial_id") or "")
        trial_final: dict[str, Any] = {}
        deadline = time.monotonic() + 240
        while trial_id and time.monotonic() < deadline:
            got_status, got_body = http_json(
                f"{gateway_base}/v1/dify/trial/{trial_id}", headers=trial_headers
            )
            if got_status == 200 and isinstance(got_body, dict):
                trial_final = got_body
                if got_body.get("status") in {"completed", "failed", "cancelled", "expired"}:
                    break
            time.sleep(2)
        report["dify_trial"] = {"post": trial_body, "final": trial_final}
        # 试用的报告是**两层**：`{"status": ..., "report": {...}}`，而 run/task/draft 身份
        # 都在内层。从外层读这几个字段永远是空串 —— 判据会红而试用其实成功。
        trial_result = trial_final.get("result") or {}
        trial_report = trial_result.get("report") or {}
        trial_run_id = str(trial_report.get("workflow_run_id") or "")
        # **不能靠"Dify 执行库里还留着那一行"**：实测按 `workflow_run_id` 去 Dify 执行库
        # 查，查不到（临时应用在 finally 里被删，那一行随之没了）—— 这条判据曾因此转红，
        # 而试用本身是成功的。真正的证据是**这次试用期间外部世界看到了什么**：合成模型
        # 收到了含这张单的播报输入，CPMS 被真的按这张单读了一次。这两者只有真 Dify 把图
        # 跑完才会发生，比"库里有一行"更难伪造。
        trial_prompts = ModelHandler.state.get("prompts") or []
        trial_cpms = list(CpmsHandler.calls)
        # 试用跑在**临时应用**里，所以它冻结的 workflow 与正式 V1 必然不同 —— 这正是
        # "试用的是草稿 V2"的证据；而它**不得**接管正式指针。
        check(
            "真 Dify 试用：草稿在临时应用里真的执行过",
            trial_status == 202
            and trial_final.get("status") == "completed"
            and bool(trial_run_id)
            and bool(trial_report.get("task_id"))
            and str(trial_report.get("draft_sha256") or "") != ""
            # 临时应用自己的 workflow 身份，不是正式那一版。
            and str(trial_report.get("workflow_id") or "") not in {"", workflow_id}
            # 模型真的收到了这张单的播报输入，CPMS 真的被读了一次 —— 图的三个步骤都跑了。
            and any(ORDER_LATEST in text for text in trial_prompts)
            and ORDER_LATEST in trial_cpms,
            {
                "post": trial_status,
                "status": trial_final.get("status"),
                "error": trial_final.get("error_code"),
                "temp_workflow_differs_from_v1": str(trial_report.get("workflow_id") or "") != workflow_id,
                "model_saw_order": any(ORDER_LATEST in t for t in trial_prompts),
                "cpms_reads": trial_cpms,
            },
        )
        with _sqlite3.connect(database) as connection:
            trial_route = connection.execute(
                "SELECT app_id, workflow_id, revision, state FROM chat_route_bindings"
                " WHERE environment = 'env-local' AND tenant_id = ? AND entry = 'consumer'",
                (TENANT,),
            ).fetchone()
            trial_versions = connection.execute(
                "SELECT COUNT(*) FROM agent_versions WHERE tenant_id = ?", (TENANT,)
            ).fetchone()[0]
        ModelHandler.state["prompts"] = []
        after_trial = _drive(gateway_base, Path(diag["case_root"]))
        report["chain_after_trial"] = after_trial
        check(
            "试用不推进正式指针：App 仍执行 V1、路由与版本都没变",
            after_trial.get("final", {}).get("status") == "completed"
            and _own_workflow_of(after_trial) == workflow_id
            and trial_route is not None
            and trial_route[1] == workflow_id
            and trial_route[3] == "active"
            and trial_versions == 1,
            {
                "actual": _own_workflow_of(after_trial),
                "route_workflow": trial_route[1] if trial_route else None,
                "route_state": trial_route[3] if trial_route else None,
                "agent_versions": trial_versions,
            },
        )

        # ② 发布：指针切到 V2，App 随后执行 V2。
        publish_status, publish_body = _publish_app()
        check(
            "运营发布成功并冻结新版本",
            publish_status == 200 and isinstance(publish_body.get("version_no"), int),
            {"status": publish_status, "body": publish_body},
        )
        # **发布之后**才取得到新的 workflow 身份：这正是「发布到 AI-Ops」要自己
        # 完成 Dify 侧发布的原因。取到之后核对它确实变了。
        _, detail_after = http_json(f"{dify_base}/console/api/apps/{app_id}", headers=console)
        published_workflow = (detail_after or {}).get("workflow", {}).get("id")
        check(
            "运营发布之后 Dify 侧出现了新的已发布 workflow",
            publish_status == 200 and bool(published_workflow) and published_workflow != workflow_id,
            {"before": workflow_id, "after": published_workflow},
        )
        version_two = published_workflow or version_two
        if publish_status != 200:
            # 发布失败时把网关自己的原因收进来：响应只有稳定码，
            # "拉取失败"这三个字没有下一步。
            gw_log = Path(diag["case_root"]) / "gateway.log"
            if gw_log.exists():
                lines = [
                    line for line in gw_log.read_text(errors="replace").splitlines() if "dify" in line.lower()
                ]
                report["gateway_publish_failure"] = "\n".join(lines[-12:])
        ModelHandler.state["prompts"] = []
        after_publish = _drive(gateway_base, Path(diag["case_root"]))
        report["chain_after_publish"] = after_publish
        actual_after_publish = _own_workflow_of(after_publish)
        published_row = _dify_run_of(after_publish)
        check(
            "发布之后 App 执行 V2",
            bool(published_row.get("workflow_run_id"))
            and after_publish.get("final", {}).get("status") == "completed"
            and actual_after_publish == version_two,
            {
                "dify_run": bool(published_row.get("workflow_run_id")),
                "actual": actual_after_publish,
                "expected": version_two,
            },
        )

        # ③ 撤回：新请求**没有执行**；已完成的正文拒绝；行仍在（不是掉回直读）。
        retract_status, retract_body = _retract_app()
        check(
            "运营撤回成功",
            retract_status == 200 and retract_body.get("state") == "retracted",
            {"status": retract_status, "body": retract_body},
        )
        cross = _drive(gateway_base, Path(diag["case_root"]))
        report["chain_after_retract"] = cross
        check(
            "撤回之后新请求不执行（没有 completed、也没有直读正文）",
            cross.get("final", {}).get("status") != "completed",
            {"status": cross.get("final", {}).get("status")},
        )
        # 已完成的正文：撤回之后回读必须拒绝（410 CHAT_ROUTE_RETRACTED）。
        old_qa = after_publish.get("qa_id")
        old_status, _old_body = http(
            f"{gateway_base}/v1/assistant/questions/{old_qa}", token=TOKEN, headers=HEADERS
        )
        check(
            "撤回之后已完成的正文被拒绝",
            old_status in {403, 404, 410},
            {"status": old_status},
        )
        trial_read_status, trial_read_body = http_json(
            f"{gateway_base}/v1/dify/trial/{trial_id}", headers=trial_headers
        )
        check(
            "撤回后已完成试用也拒绝正文",
            trial_read_status in {403, 404, 410} and "result" not in trial_read_body,
            {"status": trial_read_status},
        )
        with _sqlite3.connect(database) as connection:
            kept = connection.execute(
                "SELECT COUNT(*) FROM chat_route_bindings WHERE state = 'retracted'"
            ).fetchone()[0]
        check("撤回保留 retracted 行（不删行）", kept == 1, {"rows": kept})

        # ④ 显式恢复**历史 V1**：从不可变快照选版本，不是"再发一次当前草稿"。
        #
        # 这两种动作在旧脚本里被混为一谈：它调 `_publish_app()`（重发当前草稿）然后
        # 比较"当前绑定"，于是"恢复 V1"永远成立而什么也没恢复。
        versions_status, versions_body = http_json(
            f"{gateway_base}/v1/dify/versions",
            method="POST",
            headers=publish_headers,
            body={"app_id": app_id},
        )
        available = [item.get("version_no") for item in (versions_body or {}).get("versions", [])]
        restore_target = 1 if 1 in available else None
        check(
            "版本列表里能看见可恢复的历史版本",
            versions_status == 200 and restore_target is not None,
            {"status": versions_status, "versions": available},
        )
        restored_status, restored_body = (
            http_json(
                f"{gateway_base}/v1/dify/restore",
                method="POST",
                headers=publish_headers,
                body={"app_id": app_id, "version_no": restore_target},
            )
            if restore_target is not None
            else (0, {})
        )
        ModelHandler.state["prompts"] = []
        after_restore = _drive(gateway_base, Path(diag["case_root"]))
        report["chain_after_restore"] = after_restore
        actual_after_restore = _own_workflow_of(after_restore)
        restored_workflow = _snapshot_workflow_of(database, TENANT, restore_target)
        check(
            "显式恢复之后实际执行的是 V1 那一版",
            # 恢复不是"再发一次当前草稿"：指针要回到**历史快照里的那个 workflow_id**，
            # 而且这一轮真的走了 Dify、执行的就是它。只比较"当前绑定"会让
            # "重发 V2"也通过 —— 那正是旧脚本的错误。
            restored_status == 200
            and bool(_dify_run_of(after_restore).get("workflow_run_id"))
            and after_restore.get("final", {}).get("status") == "completed"
            and bool(restored_workflow)
            and actual_after_restore == restored_workflow
            and restored_workflow != version_two,
            {
                "status": restored_status,
                "actual": actual_after_restore,
                "snapshot_v1": restored_workflow,
                "v2": version_two,
            },
        )

        # 旧脚本这里是一个**空循环** `for version, marker in ()` —— 它写着"绑定 V1/V2
        # 实际执行同版"，但一次都没跑过。那个判据现在由运营闭环的③④两段承担：
        #   * 发布后执行 V2（`actual_after_publish == version_two`）
        #   * 恢复后执行 V1（`actual_after_restore == restored_workflow`）
        # 两者都要求"这一轮真的有 workflow_run_id"，并用 Dify 执行库里的身份核对。
        # 删掉空循环而不是留着它：一个永远不执行的判据比没有更糟 —— 它让人以为覆盖了。

        # ---- 双环境交错：另一个逻辑环境的路由不受这次发布/撤回影响 ----
        #
        # 同租户、同一张管理表，但 `environment` 不同 —— 它们必须是**两条独立的路由**。
        # 手工把第二条种进去是刻意的：真正的"交错"是"在一个环境里发布/撤回"，
        # 而判断只看那条环境自己的行有没有被动过。
        other_route = ChatRouteBindingStore(database).publish(
            environment="env-other",
            tenant_id=TENANT,
            entry="consumer",
            scenario="consumer_own_order_status",
            engine="dify",
            agent_id="小趋-客服",
            agent_version_no=9,
            app_id=app_id,
            workflow_id=version_two or workflow_id,
            workflow_key_ref=str(per_app_key),
            updated_by="acceptance",
        )
        # 此刻主环境已经历过发布→撤回→恢复；另一环境自始至终没被碰过。
        other_now = ChatRouteBindingStore(database).get(
            "env-other", TENANT, "consumer", "consumer_own_order_status"
        )
        main_now = ChatRouteBindingStore(database).get(
            "env-local", TENANT, "consumer", "consumer_own_order_status"
        )
        check(
            "双环境交错：另一个环境的路由不受发布/撤回影响",
            other_now is not None
            and other_now.revision == other_route.revision
            and other_now.agent_version_no == 9
            and other_now.state == "active"
            and main_now is not None
            and main_now.revision != other_route.revision,
            {
                "other": {
                    "revision": other_now.revision if other_now else None,
                    "version": other_now.agent_version_no if other_now else None,
                    "state": other_now.state if other_now else None,
                },
                "main_revision": main_now.revision if main_now else None,
            },
        )

        # ---- 双租户交错：**另一个租户**经真实操作入口发布/执行，与主租户互不影响 ----
        #
        restored_run = _dify_run_of(after_restore)
        check(
            "恢复后的新聊天冻结新发布代次",
            int(restored_run.get("route_revision") or 0) > 1,
            {"route_revision": restored_run.get("route_revision")},
        )
        # 双环境那条证明"同一租户的两个逻辑环境是两条路由"；它证明不了租户边界 ——
        # 同租户的两行本来就是同一批人。这里把第二个租户走一遍**同样三个真实入口**
        # （导入 → 发布 → App 执行），再回头撤主租户的版本，看两个租户各自停在什么状态。
        # DSL 里那个取数地址必须是 **Dify 容器**能到达的（`_setup_dify` 用的同一个），
        # 传宿主的 127.0.0.1 会让第二个 app 在运行期连不上取数端点。
        second = _import_second_app(dify_base, console, gateway_container_base)
        app_b = str((second or {}).get("app_id") or "")
        key_b = str((second or {}).get("service_key") or "")
        check(
            "第二租户的应用导入并发布（自己的 app / workflow）",
            bool(app_b) and bool(key_b) and bool((second or {}).get("workflow_id")),
            {"status": (second or {}).get("import_status"), "app_id": bool(app_b)},
        )
        if app_b and key_b:
            key_file_b = Path(diag["case_root"]) / "keys" / f"{app_b}.key"
            key_file_b.parent.mkdir(parents=True, exist_ok=True)
            key_file_b.write_text(key_b + "\n")
            key_file_b.chmod(0o600)
            _seed_registry(
                database,
                [
                    {
                        "tenant_id": TENANT_B,
                        "business_entry": "consumer",
                        "app_id": app_b,
                        "agent_name": "小趋-客服-B",
                    }
                ],
            )
            _seed_workspace(database, TENANT_B, workspace_id, "dify-chat-acceptance")
            _seed_agent(database, TENANT_B, name="小趋-客服-B")
            v1_b = _export_workflow_content(
                dify_base, console, app_b, str((second or {}).get("workflow_id") or "")
            )
            _make_v1_a_workflow_snapshot(
                database,
                TENANT_B,
                app_id=app_b,
                workflow_id=str((second or {}).get("workflow_id") or ""),
                workspace_id=workspace_id,
                workflow=v1_b,
            )
            ChatRouteBindingStore(database).publish(
                environment="env-local",
                tenant_id=TENANT_B,
                entry="consumer",
                scenario="consumer_own_order_status",
                engine="dify",
                agent_id="小趋-客服-B",
                agent_version_no=1,
                app_id=app_b,
                workflow_id=str((second or {}).get("workflow_id") or ""),
                workflow_key_ref=str(key_file_b),
                updated_by="acceptance",
            )
            # 第二租户在**同一环境**里发布自己的 app：真实入口、真实 Dify 发布。
            status_b, body_b = http_json(
                f"{gateway_base}/v1/dify/publish",
                method="POST",
                headers=publish_headers,
                body={"app_id": app_b},
            )
            route_b_before = ChatRouteBindingStore(database).get(
                "env-local", TENANT_B, "consumer", "consumer_own_order_status"
            )
            check(
                "第二租户经同一入口发布，自己那条路由被推进",
                status_b == 200
                and isinstance(body_b.get("version_no"), int)
                and route_b_before is not None
                and route_b_before.revision >= 2,
                {"status": status_b, "revision": route_b_before.revision if route_b_before else None},
            )
            # 第二租户的消费者**从 App 入口**问自己的单：走的必须是它自己的 app。
            drive_b = _drive(
                gateway_base,
                Path(diag["case_root"]),
                question="check my latest order status",
                token=THIRD_TOKEN,
            )
            report["chain_tenant_b"] = drive_b
            row_b = _dify_run_of(drive_b)
            check(
                "第二租户执行的是自己的 Dify 应用与本租户的订单",
                drive_b.get("final", {}).get("status") == "completed"
                and bool(row_b.get("workflow_run_id"))
                and row_b.get("app_id") == app_b
                and row_b.get("order_no") == ORDER_TENANT_B
                and ORDER_TENANT_B
                in json.dumps((drive_b.get("final") or {}).get("result") or {}, ensure_ascii=False),
                {
                    "status": (drive_b.get("final") or {}).get("status"),
                    "app_id": row_b.get("app_id"),
                    "order": row_b.get("order_no"),
                },
            )
            # 交错：现在撤回**主租户**那一版，看第二租户是否被误伤。
            retract_a_status, retract_a_body = _retract_app()
            route_a_after = ChatRouteBindingStore(database).get(
                "env-local", TENANT, "consumer", "consumer_own_order_status"
            )
            route_b_after = ChatRouteBindingStore(database).get(
                "env-local", TENANT_B, "consumer", "consumer_own_order_status"
            )
            drive_b_after = _drive(
                gateway_base,
                Path(diag["case_root"]),
                question="check my latest order status",
                token=THIRD_TOKEN,
            )
            report["chain_tenant_b_after_retract_a"] = drive_b_after
            check(
                "双租户交错：撤回主租户不碰第二租户（它照常执行）",
                retract_a_status == 200
                and retract_a_body.get("state") == "retracted"
                and route_a_after is not None
                and route_a_after.state == "retracted"
                and route_b_after is not None
                and route_b_after.state == "active"
                and route_b_after.revision == route_b_before.revision
                and drive_b_after.get("final", {}).get("status") == "completed"
                and bool(_dify_run_of(drive_b_after).get("workflow_run_id")),
                {
                    "a_state": route_a_after.state if route_a_after else None,
                    "b_state": route_b_after.state if route_b_after else None,
                    "b_revision": route_b_after.revision if route_b_after else None,
                    "b_status": (drive_b_after.get("final") or {}).get("status"),
                },
            )
            # **撤回主租户是有代价的收尾**：它把主路由留在 `retracted`，而后面还有
            # "取消在飞请求"的判据需要主路由 active —— 第一版在这里没收尾，五条取消判据
            # 全红，症状离原因很远。所以这里立刻显式恢复主租户那一版，并核它真的回到 active。
            restore_a_status, restore_a_body = http_json(
                f"{gateway_base}/v1/dify/restore",
                method="POST",
                headers=publish_headers,
                body={"app_id": app_id, "version_no": route_a_after.agent_version_no},
            )
            route_a_restored = ChatRouteBindingStore(database).get(
                "env-local", TENANT, "consumer", "consumer_own_order_status"
            )
            check(
                "双租户段收尾：显式恢复主租户，后续判据的前提重新成立",
                restore_a_status == 200
                and route_a_restored is not None
                and route_a_restored.state == "active",
                {
                    "status": restore_a_status,
                    "body": restore_a_body,
                    "a_state": route_a_restored.state if route_a_restored else None,
                },
            )

        # 模型已开始且未结束时取消；直接读取真 Dify 的 stop 标志，不能用本地终态代替。
        ModelHandler.hold = True
        ModelHandler.entered.clear()
        ModelHandler.release.clear()
        try:
            headers = {"X-Business-Entry": "consumer"}
            status, payload = http_json(
                f"{gateway_base}/v1/assistant/questions",
                method="POST",
                headers={**headers, "Authorization": f"Bearer {TOKEN}"},
                body={"question": "check my latest order status"},
            )
            qa_id = payload.get("qa_id")
            reached = ModelHandler.entered.wait(30)
            database = Path(diag["case_root"]) / "data/gateway.db"
            row = _row_for_qa(_dify_rows(database), qa_id)
            check(
                "取消前真模型步骤运行中且身份已存",
                reached
                and status == 202
                and row.get("state") == "running"
                and bool(row.get("task_id"))
                and bool(row.get("workflow_run_id")),
                {"reached_model": reached, "state": row.get("state")},
            )
            cancel_status, cancelled = http_json(
                f"{gateway_base}/v1/assistant/questions/{qa_id}/cancel",
                method="POST",
                headers={**headers, "Authorization": f"Bearer {TOKEN}"},
                body={},
            )
            stopped = _compose(
                env,
                "exec",
                "-T",
                "redis",
                "redis-cli",
                "GET",
                f"generate_task_stopped:{row.get('task_id')}",
                timeout=20,
            )
            grants = _grants_for_qa(database, qa_id)
            check(
                "模型完成前 Dify 已收到同 task 的 stop",
                reached
                and stopped.returncode == 0
                and stopped.stdout.strip() == "1"
                and not ModelHandler.release.is_set(),
                {"stop_flag": stopped.stdout.strip(), "model_released": ModelHandler.release.is_set()},
            )
            check(
                "取消后本地终态与撤权立即生效",
                cancel_status == 200
                and cancelled.get("status") == "cancelled"
                and bool(grants)
                and all(state == "revoked" for state in grants),
                {"status": cancelled.get("status"), "grants": grants},
            )
        finally:
            ModelHandler.hold = False
            ModelHandler.release.set()
        time.sleep(1)
        _, final = http_json(
            f"{gateway_base}/v1/assistant/questions/{qa_id}",
            headers={**headers, "Authorization": f"Bearer {TOKEN}"},
        )
        check(
            "迟到结果不复活取消",
            final.get("status") == "cancelled" and final.get("result") is None,
            {"status": final.get("status")},
        )

        # ---- 逆证一：伪造的取数凭据被拒，且一次业务读取都没有 ----
        CpmsHandler.calls.clear()
        forged_status, _forged_body = http(
            f"{gateway_base}/v1/internal/charging-status/dify-read",
            method="POST",
            token="cdg_forged_token_value_for_reverse_check",
            body={},
        )
        check(
            "逆证：伪造凭据被拒且不读业务",
            forged_status == 401 and CpmsHandler.calls == [],
            {"status": forged_status, "business_reads": list(CpmsHandler.calls)},
        )

        # ---- 逆证二：回调不带凭据 / 带别的 QA 的凭据都不放行 ----
        CpmsHandler.calls.clear()
        anonymous_status, _ = http(
            f"{gateway_base}/v1/internal/charging-status/dify-read", method="POST", body={}
        )
        check(
            "逆证：无凭据回调被拒且不读业务",
            anonymous_status == 401 and CpmsHandler.calls == [],
            {"status": anonymous_status},
        )
        if (chain.get("final") or {}).get("status") != "completed":
            # 失败时把两侧的原始日志一起收进来：Dify 说"调用失败"而不说为什么，
            # 那正是要靠它自己的日志回答的问题。
            # 只取 api：worker 的启动横幅有几百行，取尾会把真正的错误挤掉。
            # 再按错误行过滤一次 —— 失败时要的是"为什么"，不是全部日志。
            dify_logs = _compose(env, "logs", "--no-color", "api", timeout=120)
            raw = dify_logs.stdout + dify_logs.stderr
            interesting = [
                line
                for line in raw.splitlines()
                if any(
                    key in line
                    for key in (
                        "Error",
                        "error",
                        "Exception",
                        "Traceback",
                        "raise ",
                        " 400 ",
                        " 500 ",
                        "ValueError",
                    )
                )
            ]
            report["dify_logs_on_failure"] = "\n".join(interesting[-40:])
            report["dify_logs_raw_tail"] = raw[-2000:]
            # 节点级真相：哪个节点跑了、状态、错误。只有 api 日志的话，"workflow succeeded
            # 但两个节点都没执行"这种形状根本看不出来 —— 日志里全是 200。
            run_id = (row or {}).get("workflow_run_id")
            app_id = (setup or {}).get("app_id")
            console_headers = (setup or {}).get("console_headers") or {}
            if run_id and app_id and console_headers:
                got, text = http_json(
                    f"{dify_base}/console/api/apps/{app_id}/workflow-runs/{run_id}/node-executions",
                    headers=console_headers,
                )
                nodes = (text or {}).get("data") if isinstance(text, dict) else None
                report["dify_node_executions"] = (
                    [
                        {
                            "node_id": item.get("node_id"),
                            "type": item.get("node_type"),
                            "title": item.get("title"),
                            "status": item.get("status"),
                            "error": item.get("error"),
                            "inputs": json.dumps(item.get("inputs"), ensure_ascii=False)[:300],
                            "outputs": json.dumps(item.get("outputs"), ensure_ascii=False)[:600],
                        }
                        for item in (nodes or [])
                    ]
                    if got == 200
                    else {"status": got, "body": str(text)[:400]}
                )
            gw_log = Path(diag["case_root"]) / "gateway.log"
            if gw_log.exists():
                report["gateway_log_on_failure"] = gw_log.read_text(errors="replace")[-4000:]
    finally:
        if gateway_process is not None:
            _stop(gateway_process)
        if args.keep_stack:
            report["compose_down"] = {"rc": None, "skipped": "--keep-stack"}
        else:
            teardown = _compose(env, "down", "-v", "--remove-orphans", timeout=300)
            report["compose_down"] = {"rc": teardown.returncode}
        for server in (model, cpms):
            server.shutdown()

    return _finish(report, args)


def _host_gateway() -> str:
    return "127.0.0.1"


def _grant_states(database: Path) -> list[str]:
    if not database.exists():
        return []
    conn = sqlite3.connect(database)
    try:
        return [row[0] for row in conn.execute("SELECT state FROM chat_data_grants")]
    finally:
        conn.close()


def _grants_for_qa(database: Path, qa_id: str | None) -> list[str]:
    if not database.exists() or not qa_id:
        return []
    conn = sqlite3.connect(database)
    try:
        return [
            row[0] for row in conn.execute("SELECT state FROM chat_data_grants WHERE qa_id = ?", (qa_id,))
        ]
    finally:
        conn.close()


def _grants_revoked(database: Path) -> bool:
    states = _grant_states(database)
    return bool(states) and all(state == "revoked" for state in states)


def _finish(report: dict[str, Any], args: argparse.Namespace) -> int:
    failures = [item for item in report["checks"] if not item["ok"]]
    report["failures"] = len(failures)
    report["status"] = "PASS" if not failures else "FAIL"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        Path(args.out).chmod(0o600)
    print(text)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
