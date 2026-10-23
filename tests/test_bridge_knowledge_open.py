"""桥的知识库入口（#984 / PRD #981）。

这条路由与发布那条**不是同一种门**，所以测试也不是同一套：

| | 发布 | 知识库入口 |
|---|---|---|
| 传输 | 同源 POST（`Origin` + CORS + CSRF 双提交） | **顶层 GET**（新标签导航） |
| 改变状态 | 是（把 app 发出去） | 否（只回答"往哪跳"） |
| 判据 | Dify 会话 + 发布权限码 | Dify 会话**经登录代理验过的身份** |

**核心判据只有一条：租户来自验过的会话，不来自 URL。** 带 `?workspace=` / `?tenant_id=`
的请求结果必须与不带**逐字相同** —— 不是"被忽略"这种含糊说法，是同一条代码路径。
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from aiops_diagnostics.dify_console_publish import BridgeSettings, make_server

PUBLISH_KEY = "p" * 40
INTERNAL_TOKEN = "i" * 40
ORIGIN = "https://dify.example"
TENANT = "1942105476598861824"
WORKSPACE_ID = "c3f871e1-ffd0-4a10-a84a-b8696f7acd25"


class _Upstream:
    """两个东西合成一个假上游：登录代理与网关。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        #: 登录代理回的租户。``None`` ⇒ 身份不全（模拟没有映射）。
        self.company_tenant = TENANT
        #: 网关回的 target 与 cookie。
        self.target = "https://kb.example/"
        self.cookies = [
            {
                "name": "session",
                "value": "s-1",
                "domain": ".mall.qushiyun.com",
                "path": "/",
                "httponly": True,
                "samesite": "lax",
            }
        ]
        self.gateway_status = 200
        self.gateway_body: Any = None
        #: workspace 反查（桥用它把租户换成 workspace）。
        self.workspace_status = 200
        self.workspace_body: Any = None
        self.workspace_id = WORKSPACE_ID
        self.base = ""

    def start(self) -> None:
        src = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def _reply(self, status: int, payload: Any) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802 - 登录代理
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                src.calls.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(raw) if raw else None,
                    }
                )
                if self.path != "/__aiops/publish/check":
                    self._reply(404, {})
                    return
                tenant = src.company_tenant
                self._reply(
                    200,
                    {
                        "allowed": False,  # **没有发布权** —— 知识库入口不该在意这个
                        "reason": "MISSING_PUBLISH_PERMISSION",
                        "company_user_id": "B-1" if tenant else "",
                        "company_username": "ulink",
                        "dify_account_id": "acct-1",
                        "company_tenant_id": tenant or "",
                        "company_user_type": "1",
                    },
                )

            def do_GET(self) -> None:  # noqa: N802 - 网关
                src.calls.append(
                    {
                        "method": "GET",
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                if self.path.startswith("/console/api/workspaces/current/summary"):
                    if src.workspace_body is not None:
                        self._reply(src.workspace_status, src.workspace_body)
                    else:
                        self._reply(200, {"id": src.workspace_id, "name": "aiops-41-Ulink"})
                    return
                if not self.path.startswith("/v1/internal/knowledge/open"):
                    self._reply(404, {})
                    return
                if src.gateway_body is not None:
                    self._reply(src.gateway_status, src.gateway_body)
                    return
                self._reply(200, {"target": src.target, "tenant_id": TENANT, "cookies": src.cookies})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        host, port = self._server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def upstream() -> Any:
    up = _Upstream()
    up.start()
    yield up
    up.close()


def _bridge(upstream: _Upstream, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "dify_base_url": upstream.base,
        "gateway_base_url": upstream.base,
        "publish_key": PUBLISH_KEY,
        "console_origin": ORIGIN,
        "workspace_id": "migration-era-placeholder",
        "login_proxy_url": upstream.base,
        "gateway_internal_token": INTERNAL_TOKEN,
    }
    values.update(overrides)
    server = make_server(BridgeSettings(**values))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _get(server: Any, path: str, *, cookie: str = "access_token=a-1") -> tuple[int, dict[str, str], str]:
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        response = conn.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        body = response.read(2048).decode("utf-8", "replace")
        return response.status, headers, body
    finally:
        conn.close()


def _gateway_calls(upstream: _Upstream) -> list[dict[str, Any]]:
    return [c for c in upstream.calls if c["path"].startswith("/v1/internal/knowledge/open")]


# ── 正向：验过的会话换出一个跳转 ──────────────────────────────────────────


def test_a_valid_session_gets_a_303_with_its_tenants_workspace(upstream: _Upstream) -> None:
    server = _bridge(upstream)
    try:
        status, headers, _body = _get(server, "/__aiops/knowledge-open?workspace=" + WORKSPACE_ID)
    finally:
        server.shutdown()
        server.server_close()

    assert status == 303, headers
    assert headers["location"] == "https://kb.example/"
    calls = _gateway_calls(upstream)
    assert len(calls) == 1
    # 网关收到的是**内部令牌**，不是浏览器带来的任何东西
    assert calls[0]["headers"].get("x-internal-token")
    assert calls[0]["headers"].get("x-request-timestamp")


def test_the_gateway_session_cookie_is_relayed_to_the_browser(upstream: _Upstream) -> None:
    """网关给的 cookie 必须原样到浏览器 —— 桥是服务端，它自己不存 cookie。"""
    server = _bridge(upstream)
    try:
        _status, headers, _body = _get(server, "/__aiops/knowledge-open?workspace=" + WORKSPACE_ID)
    finally:
        server.shutdown()
        server.server_close()

    cookie = headers.get("set-cookie", "")
    assert "session=s-1" in cookie
    assert "Domain=.mall.qushiyun.com" in cookie
    assert "HttpOnly" in cookie


# ── 核心判据：URL 里的参数不参与任何判定 ──────────────────────────────────


@pytest.mark.parametrize("param", ["workspace=other-ws", "tenant_id=999", "workspace=x&tenant_id=y"])
def test_url_parameters_cannot_change_the_tenant(upstream: _Upstream, param: str) -> None:
    """带别人的 workspace / tenant 与不带**逐字相同**。

    这不是"被忽略"的宽松说法：这条路由里根本没有读 query 的代码。这条用例钉住的是
    那个事实 —— 哪天有人加了 `request.query_params.get("workspace")`，它会红。
    """
    server = _bridge(upstream)
    try:
        bare = _get(server, "/__aiops/knowledge-open?workspace=" + WORKSPACE_ID)
        with_param = _get(server, f"/__aiops/knowledge-open?workspace={WORKSPACE_ID}&{param}")
    finally:
        server.shutdown()
        server.server_close()

    assert bare[0] == with_param[0] == 303
    assert bare[1]["location"] == with_param[1]["location"]


# ── 会话与身份 ────────────────────────────────────────────────────────────


def test_no_dify_cookie_is_401_and_never_asks_the_proxy(upstream: _Upstream) -> None:
    server = _bridge(upstream)
    try:
        status, _headers, body = _get(server, "/__aiops/knowledge-open?workspace=" + WORKSPACE_ID, cookie="")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 401
    assert "登录" in body
    assert [c for c in upstream.calls if c["path"] == "/__aiops/publish/check"] == []


def test_a_session_the_proxy_does_not_know_is_401(upstream: _Upstream) -> None:
    upstream.company_tenant = None  # 代理解不出身份
    server = _bridge(upstream)
    try:
        status, _headers, _body = _get(server, "/__aiops/knowledge-open?workspace=" + WORKSPACE_ID)
    finally:
        server.shutdown()
        server.server_close()

    assert status == 401
    assert _gateway_calls(upstream) == []


def test_no_publish_permission_still_opens_the_knowledge_base(upstream: _Upstream) -> None:
    """**没有发布权不该挡住知识库**。两件事的授权不同（ADR-0087 的同一思路）。

    `_Upstream` 永远回 `allowed: false` —— 上面那条正向用例已经证明了这一点，这里把它
    单独写成一条，免得以后有人把判据改成"能发布才能管知识库"。
    """
    server = _bridge(upstream)
    try:
        status, _headers, _body = _get(server, "/__aiops/knowledge-open?workspace=" + WORKSPACE_ID)
    finally:
        server.shutdown()
        server.server_close()

    assert status == 303


# ── 未配置即 404 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("missing", ["login_proxy_url", "gateway_internal_token"])
def test_the_route_is_absent_without_its_configuration(upstream: _Upstream, missing: str) -> None:
    server = _bridge(upstream, **{missing: ""})
    try:
        status, _headers, _body = _get(server, "/__aiops/knowledge-open?workspace=" + WORKSPACE_ID)
    finally:
        server.shutdown()
        server.server_close()

    assert status == 404


# ── 上游失败要说人话，不白屏 ──────────────────────────────────────────────


def test_a_gateway_refusal_is_a_readable_page_not_a_json_dump(upstream: _Upstream) -> None:
    """失败时新标签里是**一句话**，不是 JSON。这条会被直接渲染成一个标签页。"""
    upstream.gateway_status = 502
    upstream.gateway_body = {"error": {"code": "KNOWLEDGE_UPSTREAM_UNAVAILABLE"}}
    server = _bridge(upstream)
    try:
        status, headers, body = _get(server, "/__aiops/knowledge-open?workspace=" + WORKSPACE_ID)
    finally:
        server.shutdown()
        server.server_close()

    assert status == 502
    assert headers["content-type"].startswith("text/html")
    assert "无法打开知识库" in body
    assert "{" not in body.split("<body")[0]


def test_a_gateway_answer_without_a_target_is_a_failure(upstream: _Upstream) -> None:
    """网关 200 但没给 target ⇒ 失败。**不**发一个空 Location 出去。"""
    upstream.gateway_body = {"target": "", "cookies": []}
    server = _bridge(upstream)
    try:
        status, _headers, _body = _get(server, "/__aiops/knowledge-open?workspace=" + WORKSPACE_ID)
    finally:
        server.shutdown()
        server.server_close()

    assert status == 502


def test_a_malformed_cookie_is_dropped_rather_than_widened(upstream: _Upstream) -> None:
    """形状不对的 cookie 项被丢掉，**不补一个默认 Domain** —— 那会放大会话范围。"""
    upstream.cookies = [{"name": "session", "value": "", "domain": ".mall.qushiyun.com"}]
    server = _bridge(upstream)
    try:
        status, headers, _body = _get(server, "/__aiops/knowledge-open?workspace=" + WORKSPACE_ID)
    finally:
        server.shutdown()
        server.server_close()

    assert status == 303  # 跳转本身仍成立
    assert "set-cookie" not in headers


# ── workspace 由页面带上来 ───────────────────────────────────────────────


def test_the_workspace_comes_from_the_page_and_reaches_the_gateway(upstream: _Upstream) -> None:
    """页面把它当前的 workspace 带上来，桥原样转给网关。

    真机（2026-10-23）连着纠正了两次才定到这里：

    1. 桥拿**公司身份**推 workspace —— 用 Dify 的是平台账号（`tenant_id=-1`），
       推不出来，真机上就是 409「还没有可用的工作区」；
    2. 桥**自己**去问 Dify —— 顶层 GET 不带 CSRF，Dify 回 401。

    只有页面知道"我此刻停在哪"：它有会话，也能从 cookie 里读 CSRF 发出去。
    """
    server = _bridge(upstream)
    try:
        status, _headers, _body = _get(server, "/__aiops/knowledge-open?workspace=" + WORKSPACE_ID)
    finally:
        server.shutdown()
        server.server_close()

    assert status == 303
    opens = _gateway_calls(upstream)
    assert len(opens) == 1
    assert f"workspace={WORKSPACE_ID}" in opens[0]["path"]


def test_a_missing_workspace_is_a_400_not_a_guess(upstream: _Upstream) -> None:
    """没有 workspace 就不猜 —— 猜一个等于把 A 的会话发给 B。"""
    server = _bridge(upstream)
    try:
        status, _headers, body = _get(server, "/__aiops/knowledge-open")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 400
    assert "工作区" in body
    assert _gateway_calls(upstream) == []


def test_the_page_supplied_workspace_is_still_gated_by_a_dify_session(
    upstream: _Upstream,
) -> None:
    """带了 workspace **不等于**有身份：没有 Dify 会话照样 401。

    这是"参数来自前端"这条设计能成立的前提 —— 前端能决定的只是"我这页看着是哪个
    workspace"，而桥仍然要求一个**属于这个浏览器**的 Dify 会话，网关仍然只认内部令牌。
    """
    server = _bridge(upstream)
    try:
        status, _headers, _body = _get(server, "/__aiops/knowledge-open?workspace=" + WORKSPACE_ID, cookie="")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 401
    assert _gateway_calls(upstream) == []
