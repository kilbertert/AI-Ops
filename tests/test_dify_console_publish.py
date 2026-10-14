"""控制台发布桥：身份不可伪造、app 不可指定、失败不透内部。

这一层是**注入到 Dify 界面里那颗按钮**的后端。它的安全主张只有一句：

> 唯一的凭据是运营**自己的 Dify 会话**；桥不引入新的权限，也不把它自己那把
> 服务端密钥交给浏览器。

所以这里的用例分两类：**正向**（一次真的点击把 app 发出去），以及**每一处可伪造的
输入各一条负向**（伪装会话、指定别人的 app、Referer 缺失/畸形）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from aiops_diagnostics.dify_console_publish import (
    BridgeSettings,
    ConsolePublishError,
    _console_session_from_cookie,
    app_id_from_referer,
    make_server,
    publish_current_app,
)

APP_ID = "8e36f0a6-324c-4003-b573-7444cc649411"
SESSION = "a-dify-session-cookie"


class _Upstream:
    """同时扮演 Dify 与网关的极简上游，记录它被怎么问的。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        #: Dify 对 ``GET /console/api/apps/<id>`` 的回答码。401 = 会话无效。
        self.dify_status = 200
        self.dify_sees_app = True
        self.publish_status = 200
        self.publish_body: dict[str, Any] = {"action": "synced", "version_no": 7}


def _serve(upstream: _Upstream) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            upstream.calls.append({"method": "GET", "path": self.path, "cookie": self.headers.get("Cookie")})
            status = upstream.dify_status if upstream.dify_sees_app else 404
            self._reply(status, {"id": APP_ID})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            upstream.calls.append(
                {
                    "method": "POST",
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "entry": self.headers.get("X-Business-Entry"),
                    "body": json.loads(raw or b"{}"),
                }
            )
            self._reply(upstream.publish_status, upstream.publish_body)

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


@pytest.fixture
def upstream() -> Any:
    up = _Upstream()
    server, base = _serve(up)
    up.base = base  # type: ignore[attr-defined]
    yield up
    server.shutdown()
    server.server_close()


def _publish(upstream: _Upstream, **overrides: Any):
    kwargs: dict[str, Any] = {
        "dify_base_url": upstream.base,  # type: ignore[attr-defined]
        "gateway_base_url": upstream.base,  # type: ignore[attr-defined]
        "publish_key": "server-side-publish-key",
        "console_session": SESSION,
        "referer": f"http://console.local/app/{APP_ID}/configuration",
    }
    kwargs.update(overrides)
    return publish_current_app(**kwargs)


# ── app_id 的来源：只能来自页面 URL ──────────────────────────────────────────


def test_the_app_id_comes_from_the_page_url() -> None:
    assert app_id_from_referer(f"http://x/app/{APP_ID}/configuration") == APP_ID
    assert app_id_from_referer(f"http://x/app/{APP_ID}/configuration?tab=1#frag") == APP_ID


@pytest.mark.parametrize(
    "referer",
    [
        None,
        "",
        "http://x/",
        "http://x/apps",  # 不是 /app/
        "http://x/app/",  # 没有 id
        "http://x/app/settings/configuration",  # 路由名不是 id
        "not a url at all",
    ],
)
def test_a_referer_that_does_not_name_an_app_is_refused(referer: str | None) -> None:
    """取不到 app ⇒ 拒绝。**不猜默认值** —— 猜在这里是最坏的做法。"""
    assert app_id_from_referer(referer) is None


# ── 正向：一次真的点击 ───────────────────────────────────────────────────────


def test_a_click_publishes_the_app_the_page_is_showing(upstream: _Upstream) -> None:
    result = _publish(upstream)

    assert result.published is True
    assert result.action == "synced"
    assert "7" in result.reason
    # 第一步问 Dify「这个会话是谁、看不看得见这个 app」
    assert [c for c in upstream.calls if c["method"] == "GET"][0]["path"] == f"/console/api/apps/{APP_ID}"
    # 第二步用**服务端**密钥发布，入口是 operator
    post = [c for c in upstream.calls if c["method"] == "POST"][0]
    assert post["path"] == "/v1/operator/publish"
    assert post["auth"] == "Bearer server-side-publish-key"
    assert post["entry"] == "operator"
    assert post["body"] == {"app_id": APP_ID}


def test_the_browser_never_learns_the_publish_key(upstream: _Upstream) -> None:
    """密钥只出现在**桥到网关**那一跳，不出现在结果里。"""
    result = _publish(upstream)
    blob = json.dumps(result.public(), ensure_ascii=False)
    assert "server-side-publish-key" not in blob
    # 而它确实被用过了 —— 否则上面那条会假通过
    assert any(c.get("auth") == "Bearer server-side-publish-key" for c in upstream.calls)


def test_unchanged_is_reported_as_success_without_a_version(upstream: _Upstream) -> None:
    upstream.publish_body = {"action": "unchanged", "version_no": 7}
    result = _publish(upstream)
    assert result.published is True
    assert result.action == "unchanged"


# ── 负向：每一处可伪造的输入 ─────────────────────────────────────────────────


def test_an_expired_console_session_is_refused_before_any_publish(upstream: _Upstream) -> None:
    """会话失效 ⇒ 拒绝，且**根本没打发布那一跳**。"""
    upstream.dify_status = 401
    result = _publish(upstream)

    assert result.published is False
    assert [c["method"] for c in upstream.calls] == ["GET"], "被拒就不该走到发布实现"


def test_a_session_that_cannot_see_the_app_is_refused(upstream: _Upstream) -> None:
    """Dify 说"没有这个 app"（404）⇒ 同一处置，不发。"""
    upstream.dify_sees_app = False
    result = _publish(upstream)
    assert result.published is False
    assert [c["method"] for c in upstream.calls] == ["GET"]


def test_a_missing_referer_never_reaches_dify(upstream: _Upstream) -> None:
    """Referer 缺失 ⇒ 连 Dify 都不用问。"""
    result = _publish(upstream, referer=None)
    assert result.published is False
    assert upstream.calls == []


def test_a_gateway_refusal_is_not_reported_as_success(upstream: _Upstream) -> None:
    """发布门拒了（403）⇒ 不成功，且**不把网关的内部错误码透给浏览器**。"""
    upstream.publish_status = 403
    result = _publish(upstream)
    assert result.published is False
    # 内部码（比如 DIFY_APP_NOT_BOUND）不该出现在给运营看的那句话里
    assert "DIFY_" not in result.reason


def test_an_upstream_outage_is_a_plain_failure_not_a_traceback(upstream: _Upstream) -> None:
    """桥自己那一步失败（连不上）⇒ 抛我们自己的错误类型，不是 urllib 的。"""
    with pytest.raises(ConsolePublishError):
        _publish(upstream, dify_base_url="http://127.0.0.1:1", timeout=0.5)


# ── Cookie 解析 ──────────────────────────────────────────────────────────────


def test_the_session_cookie_is_read_from_among_others() -> None:
    assert _console_session_from_cookie("a=1; access_token=xyz; b=2") == "xyz"
    assert _console_session_from_cookie("access_token=xyz") == "xyz"


@pytest.mark.parametrize("header", [None, "", "other=1", "access_token=", "access_token"])
def test_a_missing_session_cookie_is_none(header: str | None) -> None:
    """畸形输入的正确处置是"当作没有"（→ 拒绝），不是抛异常。"""
    assert _console_session_from_cookie(header) is None


# ── HTTP 面 ──────────────────────────────────────────────────────────────────


def _bridge_client(upstream: _Upstream, **overrides: Any):
    import http.client

    settings = BridgeSettings(
        listen_host="127.0.0.1",
        listen_port=0,
        dify_base_url=upstream.base,  # type: ignore[attr-defined]
        gateway_base_url=upstream.base,  # type: ignore[attr-defined]
        publish_key="server-side-publish-key",
        **overrides,
    )
    server = make_server(settings)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, http.client.HTTPConnection(host, port)


def test_the_endpoint_publishes_when_the_cookie_is_present(upstream: _Upstream) -> None:
    server, conn = _bridge_client(upstream)
    try:
        conn.request(
            "POST",
            "/__aiops/publish",
            headers={"Cookie": f"access_token={SESSION}", "Referer": f"http://x/app/{APP_ID}/configuration"},
        )
        response = conn.getresponse()
        body = json.loads(response.read())
        assert response.status == 200
        assert body["published"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_the_endpoint_refuses_without_a_cookie(upstream: _Upstream) -> None:
    server, conn = _bridge_client(upstream)
    try:
        conn.request("POST", "/__aiops/publish", headers={"Referer": f"http://x/app/{APP_ID}/configuration"})
        response = conn.getresponse()
        assert response.status == 401
        assert upstream.calls == [], "没有会话就不该去问任何人"
    finally:
        server.shutdown()
        server.server_close()


def test_the_endpoint_is_not_a_proxy(upstream: _Upstream) -> None:
    """除了那一个路径，桥什么都不服务 —— 它不是转发器。"""
    server, conn = _bridge_client(upstream)
    try:
        conn.request("POST", "/console/api/apps", headers={"Cookie": "access_token=x"})
        response = conn.getresponse()
        assert response.status == 404
        assert upstream.calls == []
    finally:
        server.shutdown()
        server.server_close()


def test_the_bridge_does_not_log_the_session_cookie(upstream: _Upstream, caplog: Any) -> None:
    """会话是运营的登录态，不进日志 —— 默认的 ``log_message`` 会打整行请求头。"""
    server, conn = _bridge_client(upstream)
    try:
        with caplog.at_level("INFO"):
            conn.request(
                "POST",
                "/__aiops/publish",
                headers={
                    "Cookie": f"access_token={SESSION}",
                    "Referer": f"http://x/app/{APP_ID}/configuration",
                },
            )
            conn.getresponse().read()
        assert SESSION not in caplog.text
    finally:
        server.shutdown()
        server.server_close()
