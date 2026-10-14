"""控制台发布桥：授权来自 Dify、工作空间被钉住、失败不透内部、成功不可伪造。

这一层是**注入到 Dify 界面里那颗按钮**的后端。它的安全主张：

> 唯一的人类凭据是运营**自己的 Dify 会话**（access_token + csrf_token 双提交）；
> 该会话的当前工作空间必须是**配置钉住的那一个**，且在其中是**所有者/管理员**；
> 桥不把服务端密钥交给浏览器，也不跟随任何重定向（否则凭据会被带去别处）。

Referer / Origin 只校验**请求上下文**（是不是从我们的控制台页面点的），不是授权：
调用方可以让 Referer 指向任意 app，真正挡住越权的是 Dify 的角色与 app 回答。

用例分三类：**正向**（一次真的点击把 app 发出去）、**每一处调用方可控的输入各一条负向**
（来源、会话、工作空间、角色、app、请求体），以及**上游回答不可信时不报成功**。
负向用例一律断言"根本没打到网关"，而不只是"结果是 False"。
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from aiops_diagnostics.dify_console_publish import (
    DEFAULT_ALLOWED_ROLES,
    BridgeSettings,
    ConsolePublishError,
    ConsoleSession,
    app_id_from_referer,
    console_session_from_headers,
    make_server,
    normalize_origin,
    parse_allowed_roles,
    publish_current_app,
)

APP_ID = "8e36f0a6-324c-4003-b573-7444cc649411"
TENANT_ID = "1b2c3d4e-0000-4000-8000-000000000001"
SESSION = "a-dify-session-cookie"
CSRF = "a-dify-csrf-token"
ORIGIN = "http://console.local:10008"
REFERER = f"{ORIGIN}/app/{APP_ID}/configuration"
PUBLISH_KEY = "server-side-publish-key"
SUMMARY_PATH = "/console/api/workspaces/current/summary"
GATEWAY_PATH = "/v1/dify/publish"


class _Upstream:
    """同时扮演 Dify 与网关的极简上游，记录它被怎么问的（含**全部**请求头）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        #: ``GET /console/api/workspaces/current/summary`` 的回答（Dify 1.17.1 实际形状）。
        self.summary_status = 200
        self.summary_body: Any = {"id": TENANT_ID, "name": "ws", "role": "owner", "plan": None, "credits": -1}
        #: ``GET /console/api/apps/<id>`` 的回答。
        self.app_status = 200
        self.app_body: Any = {"id": APP_ID, "name": "agent"}
        #: ``POST /v1/dify/publish`` 的回答（与网关 ``_publish_bound_app`` 同形）。
        self.publish_status = 200
        self.publish_body: Any = {
            "app_id": APP_ID,
            "tenant_id": "t-1",
            "business_entry": "operator",
            "agent_name": "agent",
            "action": "synced",
            "version_no": 7,
            "note": "",
        }
        #: 非空时，对应路径前缀改为回 ``(status, location)`` 的重定向。
        self.redirects: dict[str, tuple[int, str]] = {}
        self.base = ""


def _serve(upstream: _Upstream) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _record(self, method: str) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            call = {
                "method": method,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(raw) if raw else None,
            }
            upstream.calls.append(call)
            return call

        def _route(self, method: str) -> None:
            self._record(method)
            for prefix, (status, location) in upstream.redirects.items():
                if self.path.startswith(prefix):
                    self.send_response(status)
                    self.send_header("Location", location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            if self.path == SUMMARY_PATH:
                self._reply(upstream.summary_status, upstream.summary_body)
            elif self.path.startswith("/console/api/apps/"):
                self._reply(upstream.app_status, upstream.app_body)
            elif self.path == GATEWAY_PATH and method == "POST":
                self._reply(upstream.publish_status, upstream.publish_body)
            else:
                self._reply(404, {"code": "not_found"})

        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

        def _reply(self, status: int, payload: Any) -> None:
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
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
    upstream.base = f"http://{host}:{port}"
    return server


def _fixture(up: _Upstream) -> Any:
    server = _serve(up)
    yield up
    server.shutdown()
    server.server_close()


@pytest.fixture
def upstream() -> Any:
    yield from _fixture(_Upstream())


@pytest.fixture
def elsewhere() -> Any:
    """重定向想把凭据带去的"别处" —— 它**不该**收到任何请求。"""
    yield from _fixture(_Upstream())


def _paths(upstream: _Upstream, prefix: str) -> list[dict[str, Any]]:
    return [c for c in upstream.calls if c["path"].startswith(prefix)]


def _gateway_calls(upstream: _Upstream) -> list[dict[str, Any]]:
    return _paths(upstream, GATEWAY_PATH)


def _publish(upstream: _Upstream, **overrides: Any):
    kwargs: dict[str, Any] = {
        "dify_base_url": upstream.base,
        "gateway_base_url": upstream.base,
        "publish_key": PUBLISH_KEY,
        "console_session": ConsoleSession(access_token=SESSION, csrf_token=CSRF),
        "console_origin": ORIGIN,
        "workspace_id": TENANT_ID,
        "referer": REFERER,
    }
    kwargs.update(overrides)
    return publish_current_app(**kwargs)


# ── 来源：配置的控制台公开源 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://console.local:10008", "http://console.local:10008"),
        ("http://console.local:10008/", "http://console.local:10008"),
        ("HTTPS://Console.Local", "https://console.local"),
        ("https://console.local:443", "https://console.local"),
        ("http://console.local:80", "http://console.local"),
    ],
)
def test_the_console_origin_is_normalized(raw: str, expected: str) -> None:
    assert normalize_origin(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "null",
        "console.local",
        "ftp://console.local",
        "http://",
        "http://console.local/app",
        "http://console.local?x=1",
        "http://user:pw@console.local",
        "http://console.local:notaport",
    ],
)
def test_a_malformed_origin_is_none(raw: str | None) -> None:
    assert normalize_origin(raw) is None


# ── app_id 的来源：只能来自**控制台本源**页面的 URL ─────────────────────────


def test_the_app_id_comes_from_the_page_url() -> None:
    assert app_id_from_referer(REFERER, console_origin=ORIGIN) == APP_ID
    assert app_id_from_referer(f"{REFERER}?tab=1#frag", console_origin=ORIGIN) == APP_ID
    assert app_id_from_referer(f"{ORIGIN}/app/{APP_ID}/workflow", console_origin=ORIGIN) == APP_ID


@pytest.mark.parametrize(
    "referer",
    [
        None,
        "",
        f"{ORIGIN}/",
        f"{ORIGIN}/apps",  # 不是 /app/
        f"{ORIGIN}/app/",  # 没有 id
        f"{ORIGIN}/app/{APP_ID}",  # 不在 app 的某一页里
        f"{ORIGIN}/app/settings/configuration",  # 路由名不是 id
        f"{ORIGIN}/app/not-a-uuid-at-all/configuration",
        f"{ORIGIN}/app/{APP_ID.replace('-', '')}/configuration",  # 非规范 uuid
        f"{ORIGIN}/app/{{{APP_ID}}}/configuration",
        f"{ORIGIN}/app/{APP_ID.upper()}/configuration",  # 规范形是小写
        f"{ORIGIN}/app/{APP_ID}%2F../configuration",
        "not a url at all",
        # 外来源：同一路径形状，但页面不在我们的控制台上
        f"http://evil.example/app/{APP_ID}/configuration",
        f"https://console.local:10008/app/{APP_ID}/configuration",  # 协议不同
        f"http://console.local:10009/app/{APP_ID}/configuration",  # 端口不同
        f"http://console.local.evil.example:10008/app/{APP_ID}/configuration",
        f"http://x@console.local:10008/app/{APP_ID}/configuration",  # userinfo
        f"/app/{APP_ID}/configuration",  # 相对 Referer
    ],
)
def test_a_referer_that_does_not_name_an_app_on_our_console_is_refused(referer: str | None) -> None:
    """取不到 app、或页面不在**配置的控制台公开源**上 ⇒ 拒绝。**不猜默认值。**"""
    assert app_id_from_referer(referer, console_origin=ORIGIN) is None


# ── 正向：一次真的点击 ───────────────────────────────────────────────────────


def test_a_click_publishes_the_app_the_page_is_showing(upstream: _Upstream) -> None:
    result = _publish(upstream)

    assert result.published is True
    assert result.status == 200
    assert result.action == "synced"
    assert "7" in result.reason
    # 先问 Dify「这个会话在当前工作空间里是什么角色」，再问「看不看得见这个 app」
    assert [c["path"] for c in upstream.calls] == [
        SUMMARY_PATH,
        f"/console/api/apps/{APP_ID}",
        GATEWAY_PATH,
    ]
    # 两跳 Dify 都带**完整**的控制台凭据：两枚 cookie + 双提交的 CSRF 头
    for call in _paths(upstream, "/console/api/"):
        assert call["method"] == "GET"
        assert call["headers"]["cookie"] == f"access_token={SESSION}; csrf_token={CSRF}"
        assert call["headers"]["x-csrf-token"] == CSRF
        assert "authorization" not in call["headers"]
    # 最后一跳用**服务端**密钥打机器门 `/v1/dify/publish`，body 只有 app_id
    post = _gateway_calls(upstream)[0]
    assert post["method"] == "POST"
    assert post["headers"]["authorization"] == f"Bearer {PUBLISH_KEY}"
    assert post["body"] == {"app_id": APP_ID}
    # 运营的 Dify 凭据**不**带去网关
    assert "cookie" not in post["headers"]
    assert "x-csrf-token" not in post["headers"]


def test_the_browser_never_learns_the_publish_key(upstream: _Upstream) -> None:
    """密钥只出现在**桥到网关**那一跳，不出现在结果里。"""
    result = _publish(upstream)
    blob = json.dumps(result.public(), ensure_ascii=False)
    assert PUBLISH_KEY not in blob
    assert set(result.public()) == {"published", "reason", "action"}
    # 而它确实被用过了 —— 否则上面那条会假通过
    assert _gateway_calls(upstream)[0]["headers"]["authorization"] == f"Bearer {PUBLISH_KEY}"


def test_unchanged_is_reported_as_success(upstream: _Upstream) -> None:
    upstream.publish_body = {**upstream.publish_body, "action": "unchanged", "version_no": 7}
    result = _publish(upstream)
    assert result.published is True
    assert result.action == "unchanged"


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_may_publish_by_default(upstream: _Upstream, role: str) -> None:
    upstream.summary_body = {**upstream.summary_body, "role": role}
    assert _publish(upstream).published is True


def test_the_allowed_roles_are_configurable(upstream: _Upstream) -> None:
    upstream.summary_body = {**upstream.summary_body, "role": "editor"}
    result = _publish(upstream, allowed_roles=frozenset({"owner", "admin", "editor"}))
    assert result.published is True


# ── 负向：身份与角色（每条都断言**没打到网关**）────────────────────────────


@pytest.mark.parametrize("role", ["editor", "normal", "dataset_operator", "", "OWNER", "root"])
def test_a_role_below_admin_is_refused_before_any_publish(upstream: _Upstream, role: str) -> None:
    """能 ``GET`` 到 app 不够 —— 只有所有者/管理员能让改动上线。"""
    upstream.summary_body = {**upstream.summary_body, "role": role}
    result = _publish(upstream)

    assert result.published is False
    assert result.status == 403
    assert _gateway_calls(upstream) == []
    assert _paths(upstream, "/console/api/apps/") == [], "角色不够就不必再问 app"


@pytest.mark.parametrize(
    "summary",
    [
        {"id": TENANT_ID},  # 没有 role
        {"id": TENANT_ID, "role": None},
        {"id": TENANT_ID, "role": ["owner"]},
        {"role": "owner"},  # 没有工作空间 id
        {"id": "", "role": "owner"},
        ["owner"],
        "owner",
    ],
)
def test_a_summary_without_a_proper_role_is_refused(upstream: _Upstream, summary: Any) -> None:
    upstream.summary_body = summary
    result = _publish(upstream)
    assert result.published is False
    assert _gateway_calls(upstream) == []


OTHER_WORKSPACE = "1b2c3d4e-0000-4000-8000-000000000002"


@pytest.mark.parametrize(
    "workspace",
    [
        OTHER_WORKSPACE,  # 运营在别的工作空间是 owner/admin
        TENANT_ID.upper(),  # 不做大小写归一 —— Dify 回的就是它存的那串
        f"{TENANT_ID} ",
        TENANT_ID[:-1],
    ],
)
def test_an_admin_of_another_workspace_is_refused_before_app_lookup(
    upstream: _Upstream, workspace: str
) -> None:
    """切工作空间的竞态：summary 说"你在 X 是 owner"，而 app 查询可能落在 W（在 W 只是 editor）。

    钉住工作空间后，summary 的 ``id`` 不是配置的那一个 ⇒ 直接拒绝：**既不查 app，也不打网关**。
    """
    upstream.summary_body = {**upstream.summary_body, "id": workspace, "role": "owner"}
    result = _publish(upstream)

    assert result.published is False
    assert result.status == 403
    assert [c["path"] for c in upstream.calls] == [SUMMARY_PATH]
    assert _paths(upstream, "/console/api/apps/") == []
    assert _gateway_calls(upstream) == []
    assert OTHER_WORKSPACE not in result.reason, "不把工作空间 id 透给浏览器"


def test_a_mismatched_workspace_is_refused_even_for_an_allowed_role_list(upstream: _Upstream) -> None:
    """放宽角色名单不能绕过工作空间钉扎。"""
    upstream.summary_body = {**upstream.summary_body, "id": OTHER_WORKSPACE, "role": "editor"}
    result = _publish(upstream, allowed_roles=frozenset({"owner", "admin", "editor"}))
    assert result.published is False
    assert _paths(upstream, "/console/api/apps/") == []
    assert _gateway_calls(upstream) == []


@pytest.mark.parametrize("status", [401, 403, 409, 500])
def test_a_rejected_console_session_is_refused_before_any_publish(upstream: _Upstream, status: int) -> None:
    """会话失效 / CSRF 不对（Dify 回 401）/ 工作空间已归档（409）⇒ 拒绝，且没打发布那一跳。"""
    upstream.summary_status = status
    upstream.summary_body = {"code": "unauthorized", "message": "CSRF token is missing or invalid."}
    result = _publish(upstream)

    assert result.published is False
    assert _gateway_calls(upstream) == []
    assert "CSRF" not in result.reason, "不把 Dify 的原话透给浏览器"


def test_a_session_that_cannot_see_the_app_is_refused(upstream: _Upstream) -> None:
    """Dify 说"当前工作空间里没有这个 app"（404）⇒ 同一处置，不发。"""
    upstream.app_status = 404
    result = _publish(upstream)
    assert result.published is False
    assert _gateway_calls(upstream) == []


@pytest.mark.parametrize("body", [{"id": "another-app"}, {"name": "no id"}, [APP_ID], "x"])
def test_an_app_answer_for_a_different_app_is_refused(upstream: _Upstream, body: Any) -> None:
    upstream.app_body = body
    result = _publish(upstream)
    assert result.published is False
    assert _gateway_calls(upstream) == []


@pytest.mark.parametrize(
    "referer",
    [None, f"http://evil.example/app/{APP_ID}/configuration", f"{ORIGIN}/app/not-a-uuid/configuration"],
)
def test_a_bad_referer_never_reaches_dify(upstream: _Upstream, referer: str | None) -> None:
    """Referer 缺失 / 外来源 / 非 uuid ⇒ 连 Dify 都不用问。"""
    result = _publish(upstream, referer=referer)
    assert result.published is False
    assert upstream.calls == []


# ── 负向：重定向不跟随、凭据不外带 ──────────────────────────────────────────


@pytest.mark.parametrize("prefix", [SUMMARY_PATH, "/console/api/apps/"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_dify_redirect_is_not_followed(
    upstream: _Upstream, elsewhere: _Upstream, prefix: str, status: int
) -> None:
    """Dify 那跳回 3xx ⇒ 不跟随：运营的 cookie 不被带去 Location，也不发布。"""
    upstream.redirects[prefix] = (status, f"{elsewhere.base}/steal")
    result = _publish(upstream)

    assert result.published is False
    assert elsewhere.calls == [], "重定向被跟随了：运营的会话被带去了别处"
    assert _gateway_calls(upstream) == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_gateway_redirect_does_not_leak_the_publish_key(
    upstream: _Upstream, elsewhere: _Upstream, status: int
) -> None:
    """网关那跳回 3xx ⇒ 不跟随：服务端密钥不被带去 Location，结果不是成功。"""
    upstream.redirects[GATEWAY_PATH] = (status, f"{elsewhere.base}/steal")
    result = _publish(upstream)

    assert result.published is False
    assert elsewhere.calls == []
    assert len(_gateway_calls(upstream)) == 1


# ── 负向：网关的回答不可信时不报成功 ────────────────────────────────────────


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500, 502])
def test_a_gateway_refusal_is_not_reported_as_success(upstream: _Upstream, status: int) -> None:
    """发布门拒了 ⇒ 不成功，且**不把网关的内部错误码透给浏览器**。"""
    upstream.publish_status = status
    upstream.publish_body = {"code": "DIFY_APP_NOT_BOUND", "message": "app is not a bound agent source"}
    result = _publish(upstream)
    assert result.published is False
    assert "DIFY_" not in result.reason


@pytest.mark.parametrize("action", ["refused", "failed"])
def test_a_200_that_says_refused_is_not_success(upstream: _Upstream, action: str) -> None:
    """网关的 ``SyncOutcome`` 有 ``refused`` / ``failed`` 两种**非成功** —— 它们也是 200。"""
    upstream.publish_body = {**upstream.publish_body, "action": action, "version_no": None}
    result = _publish(upstream)
    assert result.published is False
    assert result.status == 409
    assert result.action == ""


def _malformed(base: dict[str, Any]) -> list[Any]:
    def without(key: str) -> dict[str, Any]:
        return {k: v for k, v in base.items() if k != key}

    return [
        {},
        [],
        "synced",
        None,
        without("action"),
        without("version_no"),
        without("app_id"),
        {**base, "action": ""},
        {**base, "action": "SYNCED"},
        {**base, "action": "published"},
        {**base, "action": ["synced"]},
        {**base, "version_no": None},
        {**base, "version_no": "7"},
        {**base, "version_no": 7.0},
        {**base, "version_no": True},
        {**base, "version_no": 0},
        {**base, "version_no": -1},
        {**base, "app_id": "another-app"},
        {**base, "action": "unchanged", "version_no": None},
    ]


_GOOD_PUBLISH = _Upstream().publish_body


@pytest.mark.parametrize("body", _malformed(_GOOD_PUBLISH))
def test_a_malformed_success_body_is_never_published_true(upstream: _Upstream, body: Any) -> None:
    """2xx 但形状不对 ⇒ **不能**说发布成功（也不能断言失败 —— 结果未确认，502）。"""
    upstream.publish_body = body
    result = _publish(upstream)
    assert result.published is False
    assert result.status == 502
    assert result.action == ""


def test_a_non_json_success_body_is_a_bridge_failure(upstream: _Upstream) -> None:
    upstream.publish_body = b"<html>ok</html>"
    with pytest.raises(ConsolePublishError):
        _publish(upstream)


def test_an_upstream_outage_is_a_plain_failure_not_a_traceback(upstream: _Upstream) -> None:
    """桥自己那一步失败（连不上）⇒ 抛我们自己的错误类型，不是 urllib 的。"""
    with pytest.raises(ConsolePublishError):
        _publish(upstream, dify_base_url="http://127.0.0.1:1", timeout=0.5)
    assert _gateway_calls(upstream) == []


# ── Cookie / CSRF 解析 ──────────────────────────────────────────────────────


def test_the_session_is_read_from_among_other_cookies() -> None:
    session = console_session_from_headers(f"a=1; access_token={SESSION}; b=2; csrf_token={CSRF}", CSRF)
    assert session == ConsoleSession(access_token=SESSION, csrf_token=CSRF)
    # 只转发这两枚 cookie，其余（a / b）不带
    assert session.cookie_header() == f"access_token={SESSION}; csrf_token={CSRF}"


def test_the_session_repr_does_not_print_the_tokens() -> None:
    text = repr(ConsoleSession(access_token=SESSION, csrf_token=CSRF))
    assert SESSION not in text
    assert CSRF not in text


@pytest.mark.parametrize(
    ("cookie", "csrf"),
    [
        (None, CSRF),
        ("", CSRF),
        ("other=1", CSRF),
        (f"csrf_token={CSRF}", CSRF),  # 缺 access_token
        (f"access_token={SESSION}", CSRF),  # 缺 csrf_token cookie
        (f"access_token=; csrf_token={CSRF}", CSRF),
        (f"access_token; csrf_token={CSRF}", CSRF),
        (f"access_token={SESSION}; csrf_token={CSRF}", None),  # 缺 X-CSRF-Token 头
        (f"access_token={SESSION}; csrf_token={CSRF}", ""),
        (f"access_token={SESSION}; csrf_token={CSRF}", "another-token"),  # 双提交不一致
        # 同名 cookie 出现两次 ⇒ 不知道该信哪一个 ⇒ 当作没有
        (f"access_token={SESSION}; access_token=other; csrf_token={CSRF}", CSRF),
        (f"access_token={SESSION}; csrf_token={CSRF}; csrf_token={CSRF}", CSRF),
        # 不是合法的 cookie-octet：不能被拼进转发出去的头
        (f"access_token={SESSION}\r\nX-Evil: 1; csrf_token={CSRF}", CSRF),
        (f'access_token="{SESSION}"; csrf_token={CSRF}', CSRF),
        (f"access_token={SESSION},x; csrf_token={CSRF}", CSRF),
        (f"access_token={'a' * 8193}; csrf_token={CSRF}", CSRF),
        (f"access_token={SESSION}; csrf_token={CSRF} x", f"{CSRF} x"),
    ],
)
def test_an_incomplete_or_malformed_console_credential_is_none(cookie: str | None, csrf: str | None) -> None:
    """畸形输入的正确处置是"当作没有"（→ 拒绝），不是抛异常。"""
    assert console_session_from_headers(cookie, csrf) is None


# ── 配置 ────────────────────────────────────────────────────────────────────


def test_the_default_allowed_roles_are_owner_and_admin() -> None:
    assert frozenset({"owner", "admin"}) == DEFAULT_ALLOWED_ROLES
    assert parse_allowed_roles(None) == DEFAULT_ALLOWED_ROLES
    assert parse_allowed_roles("") == DEFAULT_ALLOWED_ROLES
    assert parse_allowed_roles(" owner , editor ") == frozenset({"owner", "editor"})


@pytest.mark.parametrize("raw", ["root", "owner,superuser", " , ", "Owner"])
def test_an_unknown_role_in_config_fails_fast(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_allowed_roles(raw)


_REQUIRED_ENV = {
    "AIOPS_CONSOLE_BRIDGE_DIFY_URL": "http://127.0.0.1:10018",
    "AIOPS_CONSOLE_BRIDGE_GATEWAY_URL": "https://gateway.example",
    "AIOPS_CONSOLE_BRIDGE_CONSOLE_ORIGIN": "http://console.local:10008/",
    "AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY": PUBLISH_KEY,
    "AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID": f"  {TENANT_ID}  ",
}


def test_settings_require_an_explicit_console_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AIOPS_CONSOLE_BRIDGE_ALLOWED_ROLES", raising=False)
    settings = BridgeSettings.from_env()
    assert settings.console_origin == "http://console.local:10008"
    assert settings.workspace_id == TENANT_ID
    assert settings.allowed_roles == DEFAULT_ALLOWED_ROLES

    monkeypatch.delenv("AIOPS_CONSOLE_BRIDGE_CONSOLE_ORIGIN")
    with pytest.raises(ValueError, match="AIOPS_CONSOLE_BRIDGE_CONSOLE_ORIGIN"):
        BridgeSettings.from_env()


@pytest.mark.parametrize("origin", ["console.local", "http://console.local/app", "null"])
def test_settings_reject_a_malformed_console_origin(monkeypatch: pytest.MonkeyPatch, origin: str) -> None:
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AIOPS_CONSOLE_BRIDGE_CONSOLE_ORIGIN", origin)
    with pytest.raises(ValueError, match="AIOPS_CONSOLE_BRIDGE_CONSOLE_ORIGIN"):
        BridgeSettings.from_env()


def test_settings_cannot_be_built_with_an_unnormalized_origin() -> None:
    with pytest.raises(ValueError):
        BridgeSettings(
            listen_host="127.0.0.1",
            listen_port=0,
            dify_base_url="http://127.0.0.1:1",
            gateway_base_url="http://127.0.0.1:1",
            publish_key=PUBLISH_KEY,
            console_origin="http://console.local:10008/",
            workspace_id=TENANT_ID,
        )


@pytest.mark.parametrize("value", [None, "", "   "])
def test_settings_require_a_pinned_workspace(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """不钉工作空间就不启动 —— 否则角色与 app 可以来自两个不同的工作空间。"""
    for name, env_value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, env_value)
    if value is None:
        monkeypatch.delenv("AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID")
    else:
        monkeypatch.setenv("AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID", value)
    with pytest.raises(ValueError, match="AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID"):
        BridgeSettings.from_env()


@pytest.mark.parametrize("workspace_id", ["", "   ", f" {TENANT_ID}"])
def test_settings_cannot_be_built_without_a_clean_workspace(workspace_id: str) -> None:
    with pytest.raises(ValueError):
        BridgeSettings(
            listen_host="127.0.0.1",
            listen_port=0,
            dify_base_url="http://127.0.0.1:1",
            gateway_base_url="http://127.0.0.1:1",
            publish_key=PUBLISH_KEY,
            console_origin=ORIGIN,
            workspace_id=workspace_id,
        )


# ── HTTP 面 ──────────────────────────────────────────────────────────────────


@pytest.fixture
def bridge(upstream: _Upstream) -> Any:
    settings = BridgeSettings(
        listen_host="127.0.0.1",
        listen_port=0,
        dify_base_url=upstream.base,
        gateway_base_url=upstream.base,
        publish_key=PUBLISH_KEY,
        console_origin=ORIGIN,
        workspace_id=TENANT_ID,
    )
    server = make_server(settings)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[:2]
    server.shutdown()
    server.server_close()


def _browser_headers(**overrides: str | None) -> list[tuple[str, str]]:
    """浏览器里那颗按钮实际发出的头（``dify-publish-button.js``）。值为 ``None`` 即去掉该头。"""
    headers: dict[str, str | None] = {
        "Cookie": f"locale=zh-Hans; access_token={SESSION}; csrf_token={CSRF}",
        "X-CSRF-Token": CSRF,
        "Origin": ORIGIN,
        "Referer": REFERER,
        "Sec-Fetch-Site": "same-origin",
        "Content-Type": "application/json",
    }
    headers.update({k.replace("_", "-"): v for k, v in overrides.items()})
    return [(k, v) for k, v in headers.items() if v is not None]


def _post(
    address: tuple[str, int],
    *,
    path: str = "/__aiops/publish",
    headers: list[tuple[str, str]] | None = None,
    body: bytes | None = b"{}",
    content_length: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """手工拼请求，以便发出 ``http.client`` 平时不让发的畸形 ``Content-Length``。"""
    conn = http.client.HTTPConnection(*address, timeout=5)
    try:
        conn.putrequest("POST", path, skip_accept_encoding=True)
        for name, value in headers if headers is not None else _browser_headers():
            conn.putheader(name, value)
        if content_length is not None:
            conn.putheader("Content-Length", content_length)
        elif body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body if content_length is None else None)
        response = conn.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}
    finally:
        conn.close()


def test_the_endpoint_publishes_a_real_click(bridge: Any, upstream: _Upstream) -> None:
    status, body = _post(bridge)
    assert status == 200
    assert body["published"] is True
    assert len(_gateway_calls(upstream)) == 1


def test_the_endpoint_forwards_only_the_dify_credentials(bridge: Any, upstream: _Upstream) -> None:
    """浏览器带来的其它头/cookie 一律不转 —— 桥不是代理。"""
    headers = [
        *_browser_headers(),
        ("Authorization", "Bearer browser-supplied"),
        ("X-Business-Entry", "operator"),
        ("X-Forwarded-For", "1.2.3.4"),
        ("X-Evil", "1"),
    ]
    status, _ = _post(bridge, headers=headers)
    assert status == 200

    for call in _paths(upstream, "/console/api/"):
        assert call["headers"]["cookie"] == f"access_token={SESSION}; csrf_token={CSRF}"
        for leaked in ("authorization", "x-business-entry", "x-forwarded-for", "x-evil", "origin", "referer"):
            assert leaked not in call["headers"], leaked
    post = _gateway_calls(upstream)[0]
    assert post["headers"]["authorization"] == f"Bearer {PUBLISH_KEY}"
    for leaked in (
        "cookie",
        "x-csrf-token",
        "x-business-entry",
        "x-forwarded-for",
        "x-evil",
        "origin",
        "referer",
    ):
        assert leaked not in post["headers"], leaked


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"Cookie": None}, 401),
        ({"Cookie": f"access_token={SESSION}"}, 401),  # 缺 csrf_token cookie
        ({"X_CSRF_Token": None}, 401),
        ({"X_CSRF_Token": "forged"}, 401),
        ({"Origin": None}, 403),
        ({"Origin": "http://evil.example"}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "http://console.local:10009"}, 403),
        ({"Sec_Fetch_Site": "cross-site"}, 403),
        ({"Sec_Fetch_Site": "same-site"}, 403),
        ({"Sec_Fetch_Site": "none"}, 403),
        ({"Referer": None}, 403),
        ({"Referer": f"http://evil.example/app/{APP_ID}/configuration"}, 403),
        ({"Referer": f"{ORIGIN}/app/not-a-uuid/configuration"}, 403),
        ({"Content_Type": None}, 415),
        ({"Content_Type": "text/plain"}, 415),
        ({"Content_Type": "application/x-www-form-urlencoded"}, 415),
    ],
)
def test_the_endpoint_refuses_a_forged_or_cross_origin_click_without_any_upstream_call(
    bridge: Any, upstream: _Upstream, overrides: dict[str, str | None], expected: int
) -> None:
    status, body = _post(bridge, headers=_browser_headers(**overrides))
    assert status == expected
    assert body["published"] is False
    assert upstream.calls == [], "被拒的点击不该问任何上游"


def test_a_missing_sec_fetch_site_is_tolerated_when_origin_matches(bridge: Any, upstream: _Upstream) -> None:
    """老浏览器不发 ``Sec-Fetch-Site``；那时 ``Origin`` 精确匹配就是门。"""
    status, body = _post(bridge, headers=_browser_headers(Sec_Fetch_Site=None))
    assert status == 200
    assert body["published"] is True


def test_the_endpoint_refuses_a_session_in_another_workspace(bridge: Any, upstream: _Upstream) -> None:
    upstream.summary_body = {**upstream.summary_body, "id": OTHER_WORKSPACE, "role": "owner"}
    status, body = _post(bridge)
    assert status == 403
    assert body["published"] is False
    assert [c["path"] for c in upstream.calls] == [SUMMARY_PATH]


def test_the_endpoint_refuses_a_role_below_admin(bridge: Any, upstream: _Upstream) -> None:
    upstream.summary_body = {**upstream.summary_body, "role": "editor"}
    status, body = _post(bridge)
    assert status == 403
    assert body["published"] is False
    assert _gateway_calls(upstream) == []


def test_the_endpoint_refuses_when_dify_rejects_the_session(bridge: Any, upstream: _Upstream) -> None:
    upstream.summary_status = 401
    status, body = _post(bridge)
    assert status == 403
    assert body["published"] is False
    assert _gateway_calls(upstream) == []


def test_the_endpoint_does_not_report_a_malformed_success(bridge: Any, upstream: _Upstream) -> None:
    upstream.publish_body = {"action": "synced"}
    status, body = _post(bridge)
    assert status == 502
    assert body["published"] is False


def test_the_endpoint_does_not_follow_a_gateway_redirect(
    bridge: Any, upstream: _Upstream, elsewhere: _Upstream
) -> None:
    upstream.redirects[GATEWAY_PATH] = (307, f"{elsewhere.base}/steal")
    status, body = _post(bridge)
    assert body["published"] is False
    assert status != 200
    assert elsewhere.calls == []


@pytest.mark.parametrize("body", [b"", b"{}", b"  {}  "])
def test_an_empty_body_is_accepted(bridge: Any, upstream: _Upstream, body: bytes) -> None:
    status, _ = _post(bridge, body=body)
    assert status == 200


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"app_id": "another-app"}).encode(),  # 请求体**不能**指定 app
        b"[]",
        b"null",
        b"not json",
        b"\xff\xfe",
    ],
)
def test_a_body_with_content_is_rejected(bridge: Any, upstream: _Upstream, body: bytes) -> None:
    status, response = _post(bridge, body=body)
    assert status == 400
    assert response["published"] is False
    assert upstream.calls == []


@pytest.mark.parametrize(
    ("content_length", "expected"),
    [
        ("abc", 400),
        ("-1", 400),
        ("+2", 400),
        ("0x2", 400),
        ("1e3", 400),
        ("99999999999999999999999", 413),
        ("4097", 413),
        ("257", 413),
    ],
)
def test_a_bad_content_length_is_a_4xx_not_a_crash(
    bridge: Any, upstream: _Upstream, content_length: str, expected: int
) -> None:
    status, body = _post(bridge, body=None, content_length=content_length)
    assert status == expected
    assert body["published"] is False
    assert upstream.calls == []


def test_a_chunked_body_is_rejected(bridge: Any, upstream: _Upstream) -> None:
    headers = [*_browser_headers(), ("Transfer-Encoding", "chunked")]
    status, body = _post(bridge, headers=headers, body=None)
    assert status == 400
    assert body["published"] is False
    assert upstream.calls == []


def test_the_endpoint_is_not_a_proxy(bridge: Any, upstream: _Upstream) -> None:
    """除了那一个路径，桥什么都不服务 —— 它不是转发器。"""
    status, _ = _post(bridge, path="/console/api/apps")
    assert status == 404
    assert upstream.calls == []


def test_the_bridge_does_not_log_the_session_cookie(bridge: Any, upstream: _Upstream, caplog: Any) -> None:
    """会话是运营的登录态，不进日志 —— 默认的 ``log_message`` 会打整行请求头。"""
    upstream.publish_body = {"action": "synced"}  # 也走一次失败分支的日志
    with caplog.at_level("INFO"):
        _post(bridge)
        _post(bridge, headers=_browser_headers(Origin="http://evil.example"))
    assert SESSION not in caplog.text
    assert CSRF not in caplog.text
    assert PUBLISH_KEY not in caplog.text


@pytest.mark.parametrize("prefix", ["__Host-"])
def test_host_prefixed_console_cookies_are_forwarded_with_their_names(prefix: str) -> None:
    session = console_session_from_headers(f"{prefix}access_token={SESSION}; {prefix}csrf_token={CSRF}", CSRF)
    assert session is not None
    assert session.cookie_header() == f"{prefix}access_token={SESSION}; {prefix}csrf_token={CSRF}"


@pytest.mark.parametrize("bad", ["http://console.local:10008\n", "http://console.\tlocal:10008"])
def test_control_characters_in_origin_are_rejected(bad: str) -> None:
    assert normalize_origin(bad) is None


def test_ambiguous_prefixed_and_plain_credentials_are_rejected() -> None:
    assert (
        console_session_from_headers(
            f"access_token={SESSION}; csrf_token={CSRF}; __Host-access_token=x; __Host-csrf_token={CSRF}",
            CSRF,
        )
        is None
    )


@pytest.mark.parametrize("name", ["Content-Length", "Origin", "Cookie", "X-CSRF-Token"])
def test_duplicate_sensitive_headers_never_reach_upstream(
    bridge: Any, upstream: _Upstream, name: str
) -> None:
    headers = _browser_headers()
    value = next((v for k, v in headers if k == name), "0")
    headers.append((name, value))
    status, _ = _post(bridge, headers=headers)
    assert status == 400
    assert upstream.calls == []


# --------------------------------------------------------------------------- #
# #780：放行判据改由公司权限码裁定（配了映射来源时）；未配 ⇒ 过渡行为逐字不变        #
# --------------------------------------------------------------------------- #


class _PermissionSource:
    """假登录代理：只实现 /_aiops/publish/check 的答法。"""

    def __init__(self, *, allowed: bool, status: int = 200, body: Any = None) -> None:
        self.allowed = allowed
        self.status = status
        self.body = body
        self.calls: list[dict[str, Any]] = []
        self.base = ""
        src = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                src.calls.append({"path": self.path, "body": json.loads(raw) if raw else None})
                if src.body is not None:
                    payload = src.body
                elif src.allowed:
                    payload = {
                        "allowed": True,
                        "reason": "",
                        "company_user_id": "B-42",
                        "company_username": "zhang",
                        "dify_account_id": "acct-1",
                    }
                else:
                    payload = {"allowed": False, "reason": "MISSING_PUBLISH_PERMISSION"}
                data = json.dumps(payload).encode()
                self.send_response(src.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        host, port = self._server.server_address
        self.base = f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()


def test_a_company_publish_permission_lets_the_publish_through_and_carries_the_initiator() -> None:
    """持发布权限码 ⇒ 发布成功，且网关的请求**带上发起人三字段**（审计用）。"""
    upstream = _Upstream()
    server = _serve(upstream)
    permissions = _PermissionSource(allowed=True)
    try:
        result = _publish(upstream, login_proxy_url=permissions.base)
    finally:
        server.shutdown()
        permissions.close()
    assert result.published is True, result.reason
    gateway = next(call for call in upstream.calls if call["path"] == GATEWAY_PATH)
    assert gateway["headers"]["x-aiops-initiator"] == "B-42"
    assert gateway["headers"]["x-aiops-initiator-name"] == "zhang"
    assert gateway["headers"]["x-aiops-dify-account"] == "acct-1"


def test_only_the_edit_permission_is_refused_and_the_gateway_is_never_called() -> None:
    """只有编辑权码 ⇒ 403 形状的拒绝，且**不调用 AI-Ops**。"""
    upstream = _Upstream()
    server = _serve(upstream)
    permissions = _PermissionSource(allowed=False)
    try:
        result = _publish(upstream, login_proxy_url=permissions.base)
    finally:
        server.shutdown()
        permissions.close()
    assert result.published is False
    assert "发布" in result.reason
    assert not any(call["path"] == GATEWAY_PATH for call in upstream.calls), "被拒却打了网关"


def test_no_mapping_is_refused_the_same_way() -> None:
    """无映射（会话不是我们签的）⇒ 同一形状的拒绝，不调网关。"""
    upstream = _Upstream()
    server = _serve(upstream)
    permissions = _PermissionSource(allowed=False, body={"allowed": False, "reason": "NO_MAPPING"})
    try:
        result = _publish(upstream, login_proxy_url=permissions.base)
    finally:
        server.shutdown()
        permissions.close()
    assert result.published is False
    assert not any(call["path"] == GATEWAY_PATH for call in upstream.calls)


def test_an_unreachable_permission_source_fails_closed() -> None:
    """代理不可达 ⇒ **拒**（fail-closed）。不退回 owner/admin —— 那会把"查不通"变成"放行"。"""
    upstream = _Upstream()
    server = _serve(upstream)
    try:
        result = _publish(upstream, login_proxy_url="http://127.0.0.1:1")
    finally:
        server.shutdown()
    assert result.published is False
    assert not any(call["path"] == GATEWAY_PATH for call in upstream.calls)


def test_without_a_permission_source_the_owner_admin_rule_is_unchanged() -> None:
    """**过渡开关的回归**：没配映射来源 ⇒ 行为与现状逐字一致（owner/admin 判）。"""
    upstream = _Upstream()
    server = _serve(upstream)
    try:
        ok = _publish(upstream)  # role=owner
        upstream.summary_body = {"id": TENANT_ID, "name": "ws", "role": "editor", "plan": None, "credits": -1}
        refused = _publish(upstream)
    finally:
        server.shutdown()
    assert ok.published is True
    assert refused.published is False, "过渡期非 owner/admin 仍应被拒"
