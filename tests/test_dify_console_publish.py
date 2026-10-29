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
TRIAL_KEY = "server-side-trial-key"
SUMMARY_PATH = "/console/api/workspaces/current/summary"
GATEWAY_PATH = "/v1/dify/publish"
TRIAL_GATEWAY_PATH = "/v1/dify/trial"


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
        #: ``GET /v1/dify/workspace`` 的回答（#884）。默认 404 ⇒ 未开户。
        #: ``POST /v1/internal/knowledge/sync`` 的回答（网关口径，ADR-0125）。
        self.knowledge_sync_body: Any = {"changed": 0, "created": 0}
        self.workspace_lookup_status = 404
        self.workspace_lookup_body: Any = {"error": {"code": "DIFY_WORKSPACE_UNRESOLVED", "message": "x"}}
        #: ``POST /__aiops/publish/check``（代理）的回答。默认给一个"有编辑权、无租户"的映射。
        self.verdict_body: Any = {"allowed": False, "reason": "MISSING_PUBLISH_PERMISSION"}
        #: ``POST /v1/dify/trial`` 的回答（与网关试运行路由同形：202 + trial_id/status）。
        self.trial_status = 202
        self.trial_body: Any = {"trial_id": "tr_abc123", "status": "queued"}
        #: ``GET /v1/dify/trial/<id>`` 的回答（结果面板那一条）。
        self.trial_result_status = 200
        self.trial_result_body: Any = {"trial_id": "tr_abc123", "status": "queued"}
        #: M3-01 的三个运营动作（撤回 / 恢复历史版 / 版本列表）的网关回答。
        self.retract_status = 200
        self.retract_body: Any = {"app_id": APP_ID, "state": "retracted", "revision": 3}
        self.restore_status = 200
        self.restore_body: Any = {"app_id": APP_ID, "action": "synced", "version_no": 1}
        self.versions_status = 200
        self.versions_body: Any = {"app_id": APP_ID, "versions": [], "state": "active"}


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
            elif self.path == TRIAL_GATEWAY_PATH and method == "POST":
                self._reply(upstream.trial_status, upstream.trial_body)
            elif self.path.startswith(TRIAL_GATEWAY_PATH + "/") and method == "GET":
                self._reply(upstream.trial_result_status, upstream.trial_result_body)
            elif self.path.startswith("/v1/internal/knowledge/sync"):
                self._reply(200, upstream.knowledge_sync_body)
            elif self.path.startswith("/v1/dify/workspace"):
                self._reply(upstream.workspace_lookup_status, upstream.workspace_lookup_body)
            elif self.path == "/v1/dify/retract" and method == "POST":
                self._reply(upstream.retract_status, upstream.retract_body)
            elif self.path == "/v1/dify/restore" and method == "POST":
                self._reply(upstream.restore_status, upstream.restore_body)
            elif self.path == "/v1/dify/versions" and method == "POST":
                self._reply(upstream.versions_status, upstream.versions_body)
            elif self.path == "/__aiops/publish/check":
                self._reply(200, upstream.verdict_body)
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
def _sync_upstream() -> Any:
    """只为 knowledge-sync 那两条判据起一个上游。

    名字带下划线：它在测试签名里出现，但不该被 pytest 之外的东西当公共夹具。
    """
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


@pytest.fixture
def trial_bridge(upstream: _Upstream) -> Any:
    """与 ``bridge`` 同形，但**配了**试运行密钥（否则那条路是 404，见下面的反例）。"""
    settings = BridgeSettings(
        listen_host="127.0.0.1",
        listen_port=0,
        dify_base_url=upstream.base,
        gateway_base_url=upstream.base,
        publish_key=PUBLISH_KEY,
        trial_key=TRIAL_KEY,
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
                        "company_tenant_id": "T-1",
                        "company_user_type": "2",
                    }
                else:
                    # 有映射但**没有发布权限码**：真实代理在 `allowed=false` 时**仍然回
                    # 身份五字段**（身份与放行是两件事）。这些字段是试运行那条路要的。
                    payload = {
                        "allowed": False,
                        "reason": "MISSING_PUBLISH_PERMISSION",
                        "company_user_id": "B-42",
                        "company_username": "zhang",
                        "dify_account_id": "acct-1",
                        "company_tenant_id": "T-1",
                        "company_user_type": "2",
                    }
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

    def shutdown(self) -> None:
        """模拟"代理挂了"：**连监听套接字一起关掉**。

        只停 ``serve_forever`` 是不够的 —— 套接字还开着、内核照常完成 TCP 握手，
        于是调用方**不是**立刻收到「连接被拒」，而是一直等到自己的读超时。那测出来的是
        "超时"而不是"不可达"，与被测的那条分支不是同一件事。
        """
        self._server.shutdown()
        self._server.server_close()


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


# ── 试运行那一跳（#781 / #782 的部署层）─────────────────────────────────────


def _trial_post(
    address: tuple[str, int],
    *,
    body: Any = None,
    headers: list[tuple[str, str]] | None = None,
    path: str = "/__aiops/trial",
) -> tuple[int, dict[str, Any]]:
    raw = json.dumps(body if body is not None else {"order_no": "O-1"}).encode()
    return _post(address, path=path, headers=headers, body=raw)


def _trial_result_post(
    address: tuple[str, int],
    *,
    body: Any = None,
    headers: list[tuple[str, str]] | None = None,
) -> tuple[int, dict[str, Any]]:
    raw = json.dumps(body if body is not None else {"trial_id": "tr_abc123"}).encode()
    return _post(address, path="/__aiops/trial/result", headers=headers, body=raw)


def test_the_trial_submit_answer_carries_the_job_id(trial_bridge: Any) -> None:
    """提交成功时把 ``trial_id`` 交给浏览器 —— 没有它，结果面板无从轮询。

    只报"已提交"是不够的：结果在网关的库里，运营手里必须有一个能指名它的东西。
    """
    status, body = _trial_post(trial_bridge)
    assert status == 202, body
    assert body["trial_id"] == "tr_abc123"


def test_a_rejected_submission_carries_no_job_id(trial_bridge: Any, upstream: _Upstream) -> None:
    """反例：没提交成功就不该给一个 job id（给了会让面板去轮询一个不存在的东西）。"""
    upstream.trial_status = 404
    upstream.trial_body = {"error": {"code": "ORDER_NOT_FOUND"}}
    status, body = _trial_post(trial_bridge)
    assert status == 404
    assert "trial_id" not in body


def test_the_trial_result_route_reads_the_job_back(trial_bridge: Any, upstream: _Upstream) -> None:
    """结果面板那一跳：POST ``{"trial_id"}`` ⇒ 网关的 ``GET /v1/dify/trial/<id>``。

    与提交**同一套浏览器三门**、**同一把试运行钥匙** —— 它读的是真订单上的诊断结果。
    """
    upstream.trial_result_status = 200
    upstream.trial_result_body = {
        "trial_id": "tr_abc123",
        "status": "completed",
        "order_no": "O-1",
        "result": {
            "status": "diagnosed",
            "report": {
                "summary": "枪锁未释放导致无法拔枪",
                "root_cause": "车端未发出解锁信号",
                "evidence_ids": ["ev-001", "ev-002"],
                "failed_sources": ["tdengine"],
            },
        },
    }
    status, body = _trial_result_post(trial_bridge)
    assert status == 200, body
    assert body["read"] is True
    assert body["terminal"] is True
    assert "枪锁未释放" in body["reason"] and "车端未发出解锁信号" in body["reason"]
    assert "ev-001" in body["reason"]
    assert "tdengine" in body["reason"]

    reads = [
        c for c in upstream.calls if c["method"] == "GET" and c["path"] == f"{TRIAL_GATEWAY_PATH}/tr_abc123"
    ]
    assert len(reads) == 1
    assert reads[0]["headers"]["authorization"] == f"Bearer {TRIAL_KEY}"
    # 服务端的钥匙**不**回给浏览器
    assert TRIAL_KEY not in json.dumps(body, ensure_ascii=False)


def test_a_running_trial_is_reported_as_not_terminal(trial_bridge: Any, upstream: _Upstream) -> None:
    """还在跑 ⇒ ``terminal=false``，面板据此继续轮询。**终态由服务端判**。"""
    upstream.trial_result_body = {"trial_id": "tr_abc123", "status": "running"}
    status, body = _trial_result_post(trial_bridge)
    assert status == 200
    assert body["terminal"] is False
    assert body["status"] == "running"


def test_a_failed_trial_is_readable_and_named_as_failed(trial_bridge: Any, upstream: _Upstream) -> None:
    """失败也是**可读的终态**：不能因为"不是 completed"就当成读不到。"""
    upstream.trial_result_body = {"trial_id": "tr_abc123", "status": "failed", "error_code": "TRIAL_FAILED"}
    status, body = _trial_result_post(trial_bridge)
    assert status == 200
    assert body["terminal"] is True
    assert body["status"] == "failed"
    assert "failed" in body["reason"]


def test_a_failed_trial_carries_the_gateway_reason(trial_bridge: Any, upstream: _Upstream) -> None:
    """失败的**原因**必须出现在面板文字里，不能只给一个状态词。

    实测 2026-10-21：欠费导致的失败在面板上只显示「状态：failed」，与代码缺陷长得
    一模一样，运营无法判断该做什么。网关现在同时回 `error_message`（按请求语言渲染）
    与 `error_code`，桥把它们原样带进那句可读文字。
    """
    upstream.trial_result_body = {
        "trial_id": "tr_abc123",
        "status": "failed",
        "error_code": "TRIAL_FAILED",
        "error_message": "诊断执行失败，请稍后重试",
    }
    status, body = _trial_result_post(trial_bridge)
    assert status == 200
    assert "诊断执行失败" in body["reason"]
    assert "TRIAL_FAILED" in body["reason"]


def test_the_trial_result_route_needs_the_trial_key(bridge: Any) -> None:
    """未配试运行钥匙 ⇒ 404（与提交同一条机制）。"""
    status, _ = _trial_result_post(bridge)
    assert status == 404


def test_the_trial_result_route_requires_the_same_browser_gates(trial_bridge: Any) -> None:
    """三门一条都不能少：缺 Origin / CSRF / Referer ⇒ 4xx，而且**不打上游**。"""
    for headers in (
        _browser_headers(Origin=None),
        _browser_headers(**{"X-CSRF-Token": None}),
        _browser_headers(Referer=None),
    ):
        status, _ = _trial_result_post(trial_bridge, headers=headers)
        assert status in (401, 403)


def test_the_trial_result_body_is_exactly_one_key(trial_bridge: Any) -> None:
    """多带一个键 ⇒ 400（``extra="forbid"`` 的同一条主张）。"""
    status, _ = _trial_result_post(trial_bridge, body={"trial_id": "tr_abc123", "tenant_id": "T-9"})
    assert status == 400


def test_the_trial_result_route_reports_a_transient_read_failure_as_unconfirmed(
    trial_bridge: Any, upstream: _Upstream
) -> None:
    """网关 5xx ⇒ ``read=false`` 且 502，**不**编一个终态。"""
    upstream.trial_result_status = 500
    upstream.trial_result_body = {"error": {"code": "TRIAL_UNAVAILABLE"}}
    status, body = _trial_result_post(trial_bridge)
    assert status == 502
    assert body["read"] is False


def test_the_trial_route_is_absent_when_its_key_is_unconfigured(bridge: Any) -> None:
    """未配试运行钥匙 ⇒ **404**，与网关那条路由同一机制（"没开"不是"不需要钥匙"）。"""
    status, _ = _trial_post(bridge)
    assert status == 404


def test_the_trial_route_forwards_the_order_and_uses_its_own_key(
    trial_bridge: Any, upstream: _Upstream
) -> None:
    """桥只递 ``app_id``（取自 Referer）+ ``order_no``；用的是**试运行那把**钥匙。"""
    status, body = _trial_post(trial_bridge, body={"order_no": "O-987"})
    assert status == 202, body
    assert body["submitted"] is True
    calls = _paths(upstream, TRIAL_GATEWAY_PATH)
    assert len(calls) == 1
    assert calls[0]["body"] == {"app_id": APP_ID, "order_no": "O-987"}
    assert calls[0]["headers"]["authorization"] == f"Bearer {TRIAL_KEY}"
    # 发布那把钥匙**不**出现在试运行那一跳上（两条不同的授权）
    assert PUBLISH_KEY not in calls[0]["headers"]["authorization"]


def _identity_bridge(upstream: _Upstream, source: _PermissionSource) -> Any:
    """配了 ``login_proxy_url`` 的桥 —— 试运行那条路会先去问代理"这个会话是谁"。"""
    server = make_server(
        BridgeSettings(
            listen_host="127.0.0.1",
            listen_port=0,
            dify_base_url=upstream.base,
            gateway_base_url=upstream.base,
            publish_key=PUBLISH_KEY,
            trial_key=TRIAL_KEY,
            console_origin=ORIGIN,
            workspace_id=TENANT_ID,
            login_proxy_url=source.base,
        )
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_the_trial_carries_the_company_identity_from_the_proxy(
    upstream: _Upstream,
) -> None:
    """配了代理 ⇒ 试运行**带上发起人的公司身份**，网关据此算订单可见范围（#782）。

    这条路径原本是断的：代理早就把 ``company_tenant_id`` / ``company_user_type`` 回了，
    但桥只转发了三个头，于是网关每次都退回固定调试租户 —— "按人范围"的代码在库里
    躺了很久却从未被驱动过。
    """
    source = _PermissionSource(allowed=False)  # 没有发布权
    server = _identity_bridge(upstream, source)
    try:
        status, body = _trial_post(server.server_address[:2], body={"order_no": "O-5"})
    finally:
        server.shutdown()
        server.server_close()
    assert status == 202, body
    calls = _paths(upstream, TRIAL_GATEWAY_PATH)
    assert len(calls) == 1
    headers = calls[0]["headers"]
    assert headers["x-aiops-initiator"] == "B-42"
    assert headers["x-aiops-initiator-tenant"] == "T-1"
    assert headers["x-aiops-initiator-type"] == "2"
    # 试运行**只取身份**：没有发布权照样能试运行（它不产生版本、不改路由）。
    assert headers["authorization"] == f"Bearer {TRIAL_KEY}"


def test_the_publish_route_still_refuses_without_the_publish_permission(
    upstream: _Upstream,
) -> None:
    """身份与放行分开之后，**发布权的拒绝行为必须逐字不变**。

    代理在 `allowed=false` 时仍回身份字段，所以"有身份"不等于"能发布" —— 这一条
    钉住那个区分：同一个假代理，试运行放行、发布仍拒。
    """
    source = _PermissionSource(allowed=False)
    server = _identity_bridge(upstream, source)
    try:
        status, body = _post(server.server_address[:2], headers=_browser_headers())
    finally:
        server.shutdown()
        server.server_close()
    assert status == 403, body
    assert body["published"] is False
    # 没有权限就不该走到网关：被拒的尝试一次都没发出去。
    assert _paths(upstream, "/v1/dify/publish") == []


def test_the_trial_falls_back_to_the_debug_tenant_when_the_proxy_is_down(
    upstream: _Upstream,
) -> None:
    """代理不可达 ⇒ **不给身份**，网关退回固定调试租户（用户 2026-10-16 选定）。

    与发布那条路的 fail-closed **方向不同**，理由写在 ``submit_trial``：试运行退回的
    是"已经上线过的既有行为"，不是新的越权。这里钉住的是"取不到身份时照常提交"，
    并断言**一个身份头都没有带**。
    """
    source = _PermissionSource(allowed=True)
    dead = source.base  # 拿到地址后立刻关掉，模拟代理挂掉
    server = _identity_bridge(upstream, source)
    source.shutdown()
    try:
        status, body = _trial_post(server.server_address[:2], body={"order_no": "O-6"})
    finally:
        server.shutdown()
        server.server_close()
    assert status == 202, f"{body} (probe url was {dead})"
    calls = _paths(upstream, TRIAL_GATEWAY_PATH)
    assert len(calls) == 1
    for name in ("x-aiops-initiator", "x-aiops-initiator-tenant", "x-aiops-initiator-type"):
        assert name not in calls[0]["headers"], name


def test_the_trial_falls_back_when_the_proxy_reports_no_mapping(
    upstream: _Upstream,
) -> None:
    """代理回"没有映射"（只有 reason、无身份字段）⇒ 同样退回调试租户。

    租户为空是**必须**当作身份不全的：网关的 `TrialInitiator.from_headers` 在租户
    为空时静默退回调试租户，那是一次范围放宽，不能由上游一个空字段触发。
    """
    source = _PermissionSource(allowed=False, body={"allowed": False, "reason": "NO_MAPPING"})
    server = _identity_bridge(upstream, source)
    try:
        status, body = _trial_post(server.server_address[:2], body={"order_no": "O-7"})
    finally:
        server.shutdown()
        server.server_close()
    assert status == 202, body
    calls = _paths(upstream, TRIAL_GATEWAY_PATH)
    assert "x-aiops-initiator-tenant" not in calls[0]["headers"]


def test_the_trial_falls_back_when_the_tenant_field_is_empty(
    upstream: _Upstream,
) -> None:
    """身份字段**在**但租户是空串 ⇒ 仍算身份不全，不把空租户转给网关。

    这一条守的是形状而不是值：网关只判"字段有没有"，一个空串会通过它那一关，
    于是范围悄悄变成"整租户"。
    """
    source = _PermissionSource(
        allowed=True,
        body={
            "allowed": True,
            "reason": "",
            "company_user_id": "B-42",
            "company_username": "zhang",
            "dify_account_id": "acct-1",
            "company_tenant_id": "",
            "company_user_type": "2",
        },
    )
    server = _identity_bridge(upstream, source)
    try:
        status, _ = _trial_post(server.server_address[:2], body={"order_no": "O-8"})
    finally:
        server.shutdown()
        server.server_close()
    assert status == 202
    calls = _paths(upstream, TRIAL_GATEWAY_PATH)
    assert "x-aiops-initiator-tenant" not in calls[0]["headers"]
    assert "x-aiops-initiator" not in calls[0]["headers"]


def test_the_trial_route_rejects_a_scope_field_rather_than_ignoring_it(trial_bridge: Any) -> None:
    """body 里多一个 ``tenant_id`` ⇒ 400。忽略它会让人以为范围由它决定。"""
    status, _ = _trial_post(trial_bridge, body={"order_no": "O-1", "tenant_id": "T-OTHER"})
    assert status == 400


def test_the_trial_route_needs_a_non_empty_order_no(trial_bridge: Any) -> None:
    assert _trial_post(trial_bridge, body={"order_no": "   "})[0] == 400
    assert _trial_post(trial_bridge, body={})[0] == 400


def test_the_trial_route_keeps_the_same_browser_gates(trial_bridge: Any) -> None:
    """与发布**同一套**浏览器三门：换源、跨站、缺 CSRF 一律在问上游之前就被拒。"""
    assert _trial_post(trial_bridge, headers=_browser_headers(Origin="https://evil.test"))[0] == 403
    assert _trial_post(trial_bridge, headers=_browser_headers(**{"Sec-Fetch-Site": "cross-site"}))[0] == 403
    assert _trial_post(trial_bridge, headers=_browser_headers(**{"X-CSRF-Token": None}))[0] == 401


def test_the_trial_route_reports_a_busy_trial_as_429(trial_bridge: Any, upstream: _Upstream) -> None:
    upstream.trial_status = 429
    upstream.trial_body = {"error": {"code": "TRIAL_BUSY"}}
    status, body = _trial_post(trial_bridge)
    assert status == 429
    assert body["submitted"] is False


def test_the_trial_route_reports_an_unbound_app_as_404(trial_bridge: Any, upstream: _Upstream) -> None:
    upstream.trial_status = 404
    upstream.trial_body = {"error": {"code": "DIFY_APP_NOT_BOUND"}}
    status, body = _trial_post(trial_bridge)
    assert status == 404
    assert body["submitted"] is False
    assert "没有登记" in body["reason"]


def test_the_trial_route_does_not_call_an_order_scope_404_an_unbound_app(
    trial_bridge: Any, upstream: _Upstream
) -> None:
    """**同一个 404，两种含义，话必须不同**（2026-10-20 实测踩到的那一条）。

    网关对 ``ORDER_NOT_FOUND``（越权与不存在同形）也回 404。此前桥一律按
    "这个 app 没登记"回话，于是运营被指向绑定表，而真实原因是订单范围。判据是
    **两句话互不包含**：一条 404 只能落到其中一边。
    """
    upstream.trial_status = 404
    upstream.trial_body = {"error": {"code": "ORDER_NOT_FOUND", "message": "order not found"}}
    status, body = _trial_post(trial_bridge)
    assert status == 404
    assert body["submitted"] is False
    assert "没有登记" not in body["reason"]
    assert "可见范围" in body["reason"]


def test_a_404_without_a_recognisable_code_is_unconfirmed_rather_than_guessed(
    trial_bridge: Any, upstream: _Upstream
) -> None:
    """上游 404 但没带我们能读的码 ⇒ **未确认**，不猜成"没登记"。"""
    upstream.trial_status = 404
    upstream.trial_body = {"something": "else"}
    status, body = _trial_post(trial_bridge)
    assert status == 502
    assert body["submitted"] is False


def test_the_trial_route_treats_a_shapeless_answer_as_unconfirmed(
    trial_bridge: Any, upstream: _Upstream
) -> None:
    """2xx 但缺 ``trial_id`` / ``status`` ⇒ **未确认**，不许报成功。"""
    upstream.trial_body = {"something": "else"}
    status, body = _trial_post(trial_bridge)
    assert status == 502
    assert body["submitted"] is False


# --- #884：发布钉住**发起人所在**的 workspace，而不是一个配置常量 -------------------


def _identity_with_tenant(tenant_id: str = "T-884") -> dict[str, Any]:
    return {
        "allowed": True,
        "reason": "",
        "company_user_id": "u-884",
        "company_username": "zhang",
        "dify_account_id": "acc-884",
        "company_tenant_id": tenant_id,
        "company_user_type": "tenant",
    }


def _serve_workspace_lookup(upstream: _Upstream, *, workspace_id: str | None, status: int = 200) -> None:
    """让假上游同时回答网关的只读端点（`GET /v1/dify/workspace?tenant_id=…`）。"""
    upstream.summary_status = 200
    if workspace_id is None:
        upstream.workspace_lookup_body = {"error": {"code": "DIFY_WORKSPACE_UNRESOLVED", "message": "x"}}
        upstream.workspace_lookup_status = 404
    else:
        upstream.workspace_lookup_body = {"workspace_id": workspace_id, "environment": "41"}
        upstream.workspace_lookup_status = status


def test_publish_is_pinned_to_the_initiators_workspace(upstream) -> None:
    """钉住的值来自**发起人**（经网关解析），不是 `workspace_id` 常量（#884）。"""
    upstream.verdict_body = _identity_with_tenant("T-884")
    _serve_workspace_lookup(upstream, workspace_id=TENANT_ID)  # 与 summary 的 id 相同
    upstream.summary_body = {**upstream.summary_body, "id": TENANT_ID}

    result = publish_current_app(
        dify_base_url=upstream.base,
        gateway_base_url=upstream.base,
        publish_key=PUBLISH_KEY,
        console_session=ConsoleSession(access_token=SESSION, csrf_token=CSRF),
        console_origin=ORIGIN,
        workspace_id="ws-CONSTANT-NOT-THIS",  # 故意给一个不同的常量
        referer=REFERER,
        login_proxy_url=upstream.base,
        gateway_internal_token="internal-token-0123456789",
    )

    assert result.published is True, result.reason
    paths = [call["path"] for call in upstream.calls]
    assert any(path.startswith("/v1/dify/workspace?tenant_id=") for path in paths), "没有问网关要 workspace"
    assert any("tenant_id=T-884" in path for path in paths), "问的不是发起人的租户"


def test_publish_refuses_when_the_initiators_tenant_has_no_workspace(upstream) -> None:
    """解析不到 ⇒ 拒绝，且**不回落**任何默认 workspace（#884 的核心判据）。"""
    upstream.verdict_body = _identity_with_tenant("T-NO-WS")
    _serve_workspace_lookup(upstream, workspace_id=None)

    result = publish_current_app(
        dify_base_url=upstream.base,
        gateway_base_url=upstream.base,
        publish_key=PUBLISH_KEY,
        console_session=ConsoleSession(access_token=SESSION, csrf_token=CSRF),
        console_origin=ORIGIN,
        workspace_id=TENANT_ID,  # 常量"看起来是对的"也不能用
        referer=REFERER,
        login_proxy_url=upstream.base,
        gateway_internal_token="internal-token-0123456789",
    )

    assert result.published is False
    assert "工作区" in result.reason
    assert not any(call["path"] == GATEWAY_PATH for call in upstream.calls), (
        "解析失败后仍然打了网关的发布端点 —— 那会把正文发进别的工作区"
    )


def test_without_the_internal_token_the_constant_still_pins(upstream) -> None:
    """迁移期行为：没配内部令牌 ⇒ 仍按 `workspace_id` 常量判（逐字不变）。"""
    upstream.verdict_body = _identity_with_tenant("T-884")
    upstream.summary_body = {**upstream.summary_body, "id": TENANT_ID}

    result = publish_current_app(
        dify_base_url=upstream.base,
        gateway_base_url=upstream.base,
        publish_key=PUBLISH_KEY,
        console_session=ConsoleSession(access_token=SESSION, csrf_token=CSRF),
        console_origin=ORIGIN,
        workspace_id=TENANT_ID,
        referer=REFERER,
        login_proxy_url=upstream.base,
        gateway_internal_token="",
    )

    assert result.published is True, result.reason
    assert not any("/v1/dify/workspace" in call["path"] for call in upstream.calls)


def test_knowledge_sync_prefers_changed_over_created(_sync_upstream: _Upstream) -> None:
    """**「有没有变化」不等于「建了几条」**（ADR-0125）。

    运营改名或删库时网关的 `created` 是 0、`changed` 非 0。桥若只读 `created`，
    页面就不刷新 —— 卡片留在原地，点进去 404。这是本轮第三次踩同一个错
    （网关、前端各一次），所以判据钉在桥这一层。
    """
    from aiops_diagnostics.dify_console_publish import _knowledge_sync_payload

    _sync_upstream.knowledge_sync_body = {"changed": 3, "created": 1}

    changed = _knowledge_sync_payload(_sync_upstream.base, "token", workspace_id="ws-1", timeout=5.0)

    assert changed == 3, "改名/退休也要算进变化数"


def test_knowledge_sync_falls_back_to_created_for_an_old_gateway(_sync_upstream: _Upstream) -> None:
    """旧网关只回 `created` —— 桥不能因此读到 0 而永不刷新。"""
    from aiops_diagnostics.dify_console_publish import _knowledge_sync_payload

    _sync_upstream.knowledge_sync_body = {"created": 2}

    changed = _knowledge_sync_payload(_sync_upstream.base, "token", workspace_id="ws-1", timeout=5.0)

    assert changed == 2


# --- M3-01：撤回 / 恢复历史版 / 版本列表走的是**运营入口**，不是机器 key 直连 ---


def test_the_retract_button_goes_through_the_gateway_with_the_publish_key(
    bridge: Any, upstream: _Upstream
) -> None:
    """撤回由桥转发到 `/v1/dify/retract`，用的是**发布门那把钥匙**。

    运营能点的动作必须由桥承载：直接持机器 key 打网关不证明"运营能操作"。
    """
    upstream.retract_body = {"app_id": APP_ID, "state": "retracted", "revision": 3}
    status, body = _post(bridge, path="/__aiops/retract")
    assert status == 200
    assert body["published"] is True and body["action"] == "retracted"
    call = next(c for c in upstream.calls if c["path"] == "/v1/dify/retract")
    assert call["body"] == {"app_id": APP_ID}


def test_a_retract_that_the_gateway_did_not_confirm_is_not_reported_as_done(
    bridge: Any, upstream: _Upstream
) -> None:
    """网关回了 200 但状态不是 `retracted` ⇒ 桥**不能**报成功。

    "HTTP 200" 与"撤回真的成立"是两件事；把前者当后者会让运营以为已经停了。
    """
    upstream.retract_body = {"app_id": APP_ID, "state": "active", "revision": 2}
    status, body = _post(bridge, path="/__aiops/retract")
    assert status == 502
    assert body["published"] is False


def test_restore_carries_the_chosen_version_number(bridge: Any, upstream: _Upstream) -> None:
    """恢复历史版必须**带上选中的版本号**，不是"再发一次当前草稿"。"""
    upstream.restore_body = {"app_id": APP_ID, "action": "synced", "version_no": 1}
    status, body = _post(bridge, path="/__aiops/restore", body=b'{"version_no": 1}')
    assert status == 200
    call = next(c for c in upstream.calls if c["path"] == "/v1/dify/restore")
    assert call["body"] == {"app_id": APP_ID, "version_no": 1}


def test_restore_refuses_a_body_that_is_not_just_a_version(bridge: Any, upstream: _Upstream) -> None:
    """恢复的请求体只允许 `version_no`：多一个字段就是 400，不能夹带租户/workflow。"""
    status, _body = _post(bridge, path="/__aiops/restore", body=b'{"version_no": 1, "tenant_id": "X"}')
    assert status == 400
    assert not any(c["path"] == "/v1/dify/restore" for c in upstream.calls)


def test_the_versions_button_lists_what_can_be_restored(bridge: Any, upstream: _Upstream) -> None:
    """版本列表也走桥：运营要能看见"能恢复到哪几版"。"""
    upstream.versions_body = {"app_id": APP_ID, "versions": [{"version_no": 1}], "state": "active"}
    status, body = _post(bridge, path="/__aiops/versions")
    assert status == 200
    assert body["versions"] == [{"version_no": 1}]
    assert any(c["path"] == "/v1/dify/versions" for c in upstream.calls)


def test_a_platform_account_resolves_the_platform_workspace_not_its_sentinel_tenant(upstream) -> None:
    """平台账号（`user_type=-1`）问的是 `platform` workspace，不是哨兵值 `-1`。

    公司体系里平台账号没有 `sys_organ`，它的 `tenant_id` 是 `-1` —— 一个**哨兵值**，
    不是实体 id。拿它去查 workspace 会在网关的参数校验就失败（`_SAFE_ID` 要求首字符
    是字母或数字），网关回 400，而桥把所有非 2xx 都译成「这个租户没有工作区」。

    实测 2026-10-25（41 + 36）：`system` 点「发布到 AI-Ops」，桥日志
    `workspace lookup failed: _WorkspaceLookupFailed: 400`，运营看到
    「你所在的租户还没有可用的 Dify 工作区」——一句**不成立**的话：这个租户从来
    不需要开户，它需要的是落到自己的平台 workspace。

    登录侧早已这么处理（`dify_login_service.py` 的 #882），发布桥漏了同一层映射。
    """
    upstream.verdict_body = {
        **_identity_with_tenant("-1"),
        "company_user_type": "-1",  # 平台账号
        "company_username": "system",
    }
    _serve_workspace_lookup(upstream, workspace_id=TENANT_ID)
    upstream.summary_body = {**upstream.summary_body, "id": TENANT_ID}

    result = publish_current_app(
        dify_base_url=upstream.base,
        gateway_base_url=upstream.base,
        publish_key=PUBLISH_KEY,
        console_session=ConsoleSession(access_token=SESSION, csrf_token=CSRF),
        console_origin=ORIGIN,
        workspace_id=TENANT_ID,
        referer=REFERER,
        login_proxy_url=upstream.base,
        gateway_internal_token="internal-token-0123456789",
    )

    assert result.published is True, result.reason
    lookups = [c["path"] for c in upstream.calls if c["path"].startswith("/v1/dify/workspace")]
    assert lookups, "没有问网关要 workspace"
    assert all("tenant_id=platform" in path for path in lookups), (
        f"平台账号应当查 platform workspace，实际：{lookups}"
    )
    assert not any("tenant_id=-1" in path for path in lookups), (
        f"哨兵值 -1 被当成租户 id 发给了网关：{lookups}"
    )


def test_a_tenant_account_still_resolves_its_own_tenant(upstream) -> None:
    """对照：租户账号不受上面那条影响，仍然查**自己的** tenant（不能顺手改宽）。"""
    upstream.verdict_body = _identity_with_tenant("T-884")
    _serve_workspace_lookup(upstream, workspace_id=TENANT_ID)
    upstream.summary_body = {**upstream.summary_body, "id": TENANT_ID}

    result = publish_current_app(
        dify_base_url=upstream.base,
        gateway_base_url=upstream.base,
        publish_key=PUBLISH_KEY,
        console_session=ConsoleSession(access_token=SESSION, csrf_token=CSRF),
        console_origin=ORIGIN,
        workspace_id=TENANT_ID,
        referer=REFERER,
        login_proxy_url=upstream.base,
        gateway_internal_token="internal-token-0123456789",
    )

    assert result.published is True, result.reason
    lookups = [c["path"] for c in upstream.calls if c["path"].startswith("/v1/dify/workspace")]
    assert any("tenant_id=T-884" in path for path in lookups), f"租户账号查错了租户：{lookups}"
    assert not any("tenant_id=platform" in path for path in lookups), f"租户账号被误当成平台账号：{lookups}"


def test_the_inlined_platform_constants_match_their_sources() -> None:
    """桥里重写的两个常量必须与出处一致。

    发布桥的部署制品只含五个模块（`ops/dify-console-bridge/build-bundle.sh:13`），
    所以 `dify_console_publish.py` **不能** import `dify_login_service` 或
    `dify_workspaces` —— 实测 2026-10-25：那样写让桥在服务宿主上
    `ModuleNotFoundError`，五个部署测试转红，而它们在本地全绿。

    代价是这两个值在本模块重写了一份。本用例就是那份代价的守卫：出处改了而这里
    没跟上，立刻红。
    """
    from aiops_diagnostics import dify_login_service, dify_workspaces
    from aiops_diagnostics.dify_console_publish import PLATFORM_TENANT_ID as BRIDGE_TENANT
    from aiops_diagnostics.dify_console_publish import PLATFORM_USER_TYPE as BRIDGE_TYPE

    assert BRIDGE_TYPE == dify_login_service.PLATFORM_USER_TYPE, (
        "桥里的 PLATFORM_USER_TYPE 与 dify_login_service 不一致"
    )
    assert BRIDGE_TENANT == dify_workspaces.PLATFORM_TENANT_ID, (
        "桥里的 PLATFORM_TENANT_ID 与 dify_workspaces 不一致"
    )
    # 同名的另一个常量属于不同命名空间，值本就不同；钉住这一点，
    # 免得有人"统一"它们（ADR-0008 的「同名不同物」）。
    from aiops_diagnostics.publication import shortcut_lifecycle

    assert shortcut_lifecycle.PLATFORM_TENANT_ID != BRIDGE_TENANT
