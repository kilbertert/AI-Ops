"""Dify 控制台里那颗「发布到 AI-Ops」按钮的**后端桥**（ADR-0021）。

## 这一层在解决什么

运营在 Dify 控制台里编辑智能体，改完必须让**线上**生效。发布实现早就有了
（网关的 ``_publish_bound_app``），但它**没有界面**。这一层就是那个界面背后的东西。

## 形态是 BFF：先把"人"验明白，再以"机器"的身份把事做完

* **人**：桥拿浏览器带来的**运营自己的 Dify 控制台凭据**问 Dify。Dify 1.17.1 的
  ``/console/api/*`` 要求的完整凭据是 ``access_token`` cookie + ``csrf_token``
  cookie + 与后者相同的 ``X-CSRF-Token`` 头（``api/libs/token.py`` 的
  ``check_csrf_token``，GET 也校验）。少任何一样 Dify 都回 401。
* **角色**：能 ``GET`` 到一个 app 只说明"看得见"，不说明"有权让改动上线"。桥先问
  ``GET /console/api/workspaces/current/summary``（返回该账号**当前工作空间**的 ``id``
  与 ``TenantAccountJoin.role``），默认只放行 ``owner`` / ``admin``（与 Dify 自己的
  ``TenantAccountRole.is_privileged_role`` 同一集合）。
* **工作空间被钉住**：summary 的 ``id`` 必须（常量时间）等于配置的
  ``AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID``，否则既不查 app 也不发布。没有这一条，
  "当前工作空间"在两次调用之间可以被运营切换：角色来自 X（owner），app 查询落在
  W（只是 editor）。钉住保证**角色**来自配置的工作空间。它**不能**单独关掉 summary
  与 app 查询之间的切换窗口：那时 app 查询可能落在 W 并成功。这个窗口由下游关上：
  网关只发布已绑定的 app，且**以配置的工作空间导出 DSL** —— W 里的 app 在那里导不出来。
  所以"网关导出所用工作空间 == ``AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID``"是本层安全性的
  前提，属于部署侧约束，桥本身证明不了。**不依赖** app 回答里有 ``tenant_id``
  （Dify 1.17.1 的 ``AppDetail`` 没有这个字段）。
* **app**：再问 ``GET /console/api/apps/<id>`` —— Dify 按**当前工作空间**限定
  （``App.tenant_id == current_tenant_id``），并核对回答里的 ``id`` 就是这个 app。
* **机器**：最后以服务端密钥调 ``POST /v1/dify/publish``（共享密钥门）。**不是**
  ``/v1/operator/publish`` —— 那条认的是运营的**网关/公司**会话，桥手上只有 Dify
  会话，两者换不通。两条门最终走同一个 ``_publish_bound_app``，它只接受**已绑定**的 app。

## 浏览器侧的门

* **来源**：只认配置的**控制台公开源**（``AIOPS_CONSOLE_BRIDGE_CONSOLE_ORIGIN``，
  浏览器地址栏里那个源，不是桥访问 Dify 用的内网地址）。``Origin`` 必须精确等于它；
  ``Sec-Fetch-Site`` 出现时必须是 ``same-origin``；``Referer`` 也必须在这个源上。
* **这些只校验请求上下文，不是授权。** 同源检查回答的是"这次点击像不像从我们的
  控制台页面发出"，挡的是跨站伪造请求；它不回答"这个人能不能发布这个 app"。
  ``Referer`` 是调用方可控的（非浏览器客户端可以随意写），所以 app_id **可以被指定**
  为任意 uuid —— 挡住越权的是后面 Dify 对该会话的角色、工作空间与 app 回答。
* **app_id 取自 Referer 的页面路径**（``/app/<规范 uuid>/<页>``），不另设请求体字段。
  请求体只允许为空或 ``{}``，``Content-Type`` 必须是 ``application/json``
  （跨源的简单表单请求发不出它）。
* **不转发任意头。** 发往 Dify 的只有那两枚 cookie 和 CSRF 头；发往网关的只有密钥。
* **不跟随重定向。** 跟随会把运营的 cookie 或服务端密钥带去 ``Location``。

## 失败得"很无聊"，成功不可伪造

每一个失败分支都返回同一个形状（``{"published": false, "reason": ...}``），不把
Dify/网关的错误码透出去。反过来，网关回 2xx 也**不等于**发布成功：``action`` 必须是
``synced`` / ``unchanged`` 且 ``version_no`` 是正整数、``app_id`` 对得上；网关自己的
``refused`` / ``failed`` 是拒绝（409），形状不对是"结果未确认"（502）。
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import re
import time as _time_module
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

from aiops_diagnostics.async_job_lifecycle import DIAGNOSIS as DIAGNOSIS_PROFILE
from aiops_diagnostics.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    json_body,
    request_json,
)

_LOGGER = logging.getLogger("aiops.dify_console_publish")

#: Dify 1.17.1 的控制台凭据名（``api/constants/__init__.py``）。都是**运营自己的**登录态。
#: https 且未设 COOKIE_DOMAIN 时 Dify 加 __Host- 前缀；解析时保留实际凭据名。
CONSOLE_ACCESS_TOKEN_COOKIE = "access_token"
CONSOLE_CSRF_COOKIE = "csrf_token"
CONSOLE_CSRF_HEADER = "X-CSRF-Token"

#: 桥只打这三跳，都是相对路径 —— "打到谁"由部署配置决定。
# 平台账号的两个哨兵值。
#
# ⚠️ **不 import 它们的原始出处**：发布桥的部署制品只含
# `{__init__, dify_console_publish, async_job_lifecycle, bounded_http, http_auth}.py`
# （`ops/dify-console-bridge/build-bundle.sh:13`）。从 `dify_login_service` 或
# `dify_workspaces` 导入会让桥在服务宿主上 `ModuleNotFoundError` —— 实测 2026-10-25：
# 五个部署测试立刻转红，而它们在本地全绿。这是本模块唯一允许"重写一个常量"的地方，
# 代价由 `tests/test_dify_console_publish.py` 的两条对照用例钉住。
#
# 值与出处必须一致：
#   - `PLATFORM_USER_TYPE = "-1"` ← `dify_login_service.py:66`
#   - `PLATFORM_TENANT_ID = "platform"` ← `dify_workspaces.py:448`（workspace 目录的
#     平台键，即 `dify_workspaces` 表里 `tenant_id='platform'` 那一行）
#
# 另注：`shortcut_lifecycle.py` 有同名的 `PLATFORM_TENANT_ID = "__platform__"`，那是
# **快捷动作**命名空间，与 workspace 无关（ADR-0008 记的「同名不同物」）。
PLATFORM_USER_TYPE = "-1"
PLATFORM_TENANT_ID = "platform"

DIFY_WORKSPACE_SUMMARY_PATH = "/console/api/workspaces/current/summary"
DIFY_APP_PATH = "/console/api/apps/{app_id}"
#: 机器门（共享密钥）。**不是** ``/v1/operator/publish``：那条要运营的网关会话。
PUBLISH_PATH = "/v1/dify/publish"
#: 试运行（#781 / #782 的部署层）。草稿由**网关**自己从 Dify 读回来，桥只递 app_id。
TRIAL_PATH = "/v1/dify/trial"

#: Dify ``TenantAccountRole`` 的全部取值（``api/models/account.py``）。
KNOWN_ROLES = frozenset({"owner", "admin", "editor", "normal", "dataset_operator"})
#: 默认只放行 Dify 自己的 ``is_privileged_role`` 集合。
DEFAULT_ALLOWED_ROLES = frozenset({"owner", "admin"})

#: 网关 ``SyncOutcome.action``（``dify_dsl_pull.py``）：前两种是成功，后两种是拒绝。
_PUBLISHED_ACTIONS = frozenset({"synced", "unchanged"})
_REFUSED_ACTIONS = frozenset({"refused", "failed"})

#: 请求体只允许为空或 ``{}``；上限远小于 nginx 的 ``client_max_body_size 4k``。
MAX_REQUEST_BODY_BYTES = 256
#: 上游响应体上限。超限即截断 → JSON 解码失败 → 按"这一步没做成"处置（失败关闭）。
MAX_UPSTREAM_RESPONSE_BYTES = 1024 * 1024
#: 单个 cookie / CSRF 值的长度上限（JWT 远小于它）。
MAX_TOKEN_CHARS = 8192

#: RFC 6265 §4.1.1 的 cookie-octet（不含引号、逗号、分号、反斜杠、空白与控制字符）。
#: CSRF 头的值就是 csrf_token cookie 的副本，用同一字符集。值会被拼进转发出去的头，
#: 所以这一条同时是**头注入**的防线。
_TOKEN_RE = re.compile(r"[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+")
_CONTENT_LENGTH_RE = re.compile(r"[0-9]{1,6}")

_PUBLISH_ROUTE = "/__aiops/publish"

#: 试运行（#781 / #782 的部署层）。与发布同源、同一套浏览器三门，但 body 要带订单号。
_TRIAL_ROUTE = "/__aiops/trial"

#: 试运行请求体上限。它只带 ``{order_no}``（几十字节），但给足余量，免得一个异常的
#: 调用方因为多带了一个字段而拿到一个看不懂的 400。**草稿不从这里进** —— 草稿由网关
#: 自己去 Dify 拉（见 ``_TRIAL_PAYLOAD_KEYS`` 的注释）。
MAX_TRIAL_BODY_BYTES = 32 * 1024

#: 试运行请求体**只允许**这几个键。``app_id`` 由 Referer 决定，不从 body 读。
_TRIAL_PAYLOAD_KEYS = frozenset({"order_no"})

#: 读试运行结果（本票新增）。与提交共用同一套浏览器三门，所以同样是 POST ——
#: 浏览器的同源 GET **不发** ``Origin``，把这条做成 GET 就得为它单开一套更弱的门。
_TRIAL_RESULT_ROUTE = "/__aiops/trial/result"

#: 结果请求体**只允许**这一个键。``trial_id`` 是提交时网关给的，客户端原样带回。
_TRIAL_RESULT_PAYLOAD_KEYS = frozenset({"trial_id"})

#: 试运行终态词表**只有一处**（``async_job_lifecycle.DIAGNOSIS.terminal``）。
#: 这里不另写一份：多一份词表就多一次"一边改了另一边没改"，而它的表现是
#: 面板永远转圈或永远不刷新 —— 两种都看不出成因。
TRIAL_TERMINAL_STATUSES: frozenset[str] = DIAGNOSIS_PROFILE.terminal

#: 发布权查询（#780）：登录代理上的只读端点。**未配置它 = 维持过渡行为**
#: （按 Dify 的 owner/admin 判），所以身份闭环上线前行为逐字不变。
_PUBLISH_CHECK_PATH = "/__aiops/publish/check"

#: 知识库入口（#984 / PRD #981）：浏览器新标签 GET 到这里，桥解析身份后 303 给网关。
#:
#: **为什么是 GET 而不是 POST**：它是浏览器的一次**顶层导航**（`window.open`），
#: 不是同源 fetch —— 顶层导航不带 `Origin`、也不带 CSRF 头，所以发布那条门在这里
#: 用不了，也不该用：这条路由**不改任何东西**，它只回答"往哪跳"。真正改变状态的
#: 动作在网关那一侧（换会话），而那里的租户来自**登录代理验过的会话**。
_KNOWLEDGE_OPEN_ROUTE = "/__aiops/knowledge-open"
#: 编排页「添加知识库」弹窗打开时调一次（ADR-0122）。与上面那条**同一套身份**：
#: 租户由桥从 Cookie 里的 Dify 会话解析，工作区是桥自己的固定配置，请求体为空。
_KNOWLEDGE_SYNC_ROUTE = "/__aiops/knowledge-sync"
_REFUSED_PUBLISH_PERMISSION = "公司账号没有发布到 AI-Ops 的权限，请联系公司管理员"


class _KnowledgeRefused(Exception):
    """网关给出一个运营**能行动**的拒绝（未开户 / 链接过期）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ConsolePublishError(RuntimeError):
    """桥自己的一步失败了（不是"运营无权"，而是"这一步没做成"）。"""


_REFUSED_NO_WORKSPACE = "你所在的租户还没有可用的 Dify 工作区，不能发布。请联系管理员为该租户开户后再试"


def _console_access_token_from_cookie(cookie_header: str | None) -> ConsoleSession | None:
    """只取 ``access_token``，**不要求 CSRF**（#984）。

    ``console_session_from_headers`` 要求 CSRF 双提交，因为发布那条路是**同源 POST**
    且会改变状态。知识库入口是浏览器的**顶层 GET 导航**（新标签），它不携带
    ``X-CSRF-Token``，也没有要防的跨站伪造 —— 这条路只回答"往哪跳"，真正的状态变化
    在网关，而网关的租户来自登录代理验过的会话。

    CSRF 空串是**显式**的，不是遗漏：它在这里不参与任何判定，也不会被发出去。
    """
    if not cookie_header:
        return None
    found: dict[str, list[str]] = {prefix + CONSOLE_ACCESS_TOKEN_COOKIE: [] for prefix in ("", "__Host-")}
    for chunk in cookie_header.split(";"):
        name, sep, value = chunk.strip().partition("=")
        if name in found:
            found[name].append(value if sep else "")
    plain = bool(found[CONSOLE_ACCESS_TOKEN_COOKIE])
    prefixed = bool(found["__Host-" + CONSOLE_ACCESS_TOKEN_COOKIE])
    if plain == prefixed:  # 两种都没有，或两种都有（不知道该用哪一个）
        return None
    prefix = "__Host-" if prefixed else ""
    access = found[prefix + CONSOLE_ACCESS_TOKEN_COOKIE]
    if len(access) != 1 or not _is_token(access[0]):
        return None
    return ConsoleSession(access_token=access[0], csrf_token="", cookie_prefix=prefix)


def _escape(text: str) -> str:
    """最小 HTML 转义：这段文案里可能有来自上游的字符串，别让它变成标记。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _workspace_for(
    gateway_base_url: str, internal_token: str, *, tenant_id: str, timeout: float
) -> str | None:
    """问网关切"这个租户的 workspace 是哪一个"（#884 / ADR-0067 的只读端点）。

    返回 ``None`` = 解析不到（没开户 / 创建中 / 失败 / 未知）**或**查询本身失败。
    两种都**拒绝发布**：一个是"他不该发"，一个是"我们不知道他该发到哪" ——
    在任何一种下继续发布都可能把 A 的正文发进 B 的工作区。

    真正的区分在网关那一侧（四个可区分的码）；到了桥这里，"能不能发"是同一个答案。
    运营看到的文案因此只说一件事：**去找管理员开户**，而不是猜是哪种状态。
    """
    import time

    from aiops_diagnostics.bounded_http import ErrorMapping, RequestSpec, join_url, request_json
    from aiops_diagnostics.http_auth import build_internal_token_headers

    def refused(failure: HttpFailure) -> Exception:
        return _WorkspaceLookupFailed(f"{failure.status or failure.detail}")

    try:
        payload = request_json(
            RequestSpec(
                url=join_url(gateway_base_url, "/v1/dify/workspace", query={"tenant_id": tenant_id}),
                headers=build_internal_token_headers(internal_token, 300, int(time.time())),
                timeout=timeout,
            ),
            mapping=ErrorMapping(
                auth_rejected=refused,
                http_error=refused,
                unavailable=refused,
                invalid_body=refused,
                invalid_envelope=refused,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 查不通一律按"不能确认"处置
        # 记一行可区分的原因：这条路径上的"查不到"与"查不通"在运营看到的那句话里
        # 是同一个，但在日志里必须分开 —— 否则排查只能靠猜。
        _LOGGER.info("workspace lookup failed: %s: %s", type(exc).__name__, exc)
        return None
    workspace = str(payload.get("workspace_id") or "") if isinstance(payload, dict) else ""
    if not workspace:
        _LOGGER.info("workspace lookup returned no workspace_id: %r", payload)
    return workspace or None


class _WorkspaceLookupFailed(Exception):
    """内部信号：查询失败（真正的处置在上面的 ``except`` 里）。"""


@dataclass(frozen=True, slots=True)
class ConsolePublishResult:
    """一次按钮点击的结果 —— 面向前端，刻意只有三个字段（``status`` 只给 HTTP 面用）。"""

    published: bool
    reason: str
    #: 发布实现自己的回答（``unchanged`` / ``synced``）。失败时为空串。
    action: str = ""
    #: 桥回给浏览器的 HTTP 状态：200 成功、403 拒绝、409 发布门拒绝、502 结果未确认。
    status: int = 403
    #: 试运行提交成功时网关给的作业 id，按钮拿它去轮询结果面板。
    #: **不是新的授权**：读回那条路由仍要同一个发起人与同一把钥匙。
    #: 它不进 :meth:`public` —— 发布那条路的回答形状是两个不同的契约，混在一起会让
    #: 「发布」的响应多出一个它永远不会有的字段。
    trial_id: str = ""
    versions: tuple[dict[str, Any], ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "reason": self.reason,
            "action": self.action,
            **({"versions": list(self.versions)} if self.versions else {}),
        }


@dataclass(frozen=True, slots=True)
class ConsoleSession:
    """运营的 Dify 控制台凭据：两枚 cookie（CSRF 头就是 ``csrf_token`` 的副本）。"""

    access_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    cookie_prefix: str = ""

    def cookie_header(self) -> str:
        return (
            f"{self.cookie_prefix}{CONSOLE_ACCESS_TOKEN_COOKIE}={self.access_token}; "
            f"{self.cookie_prefix}{CONSOLE_CSRF_COOKIE}={self.csrf_token}"
        )

    def dify_headers(self) -> dict[str, str]:
        """发往 Dify 的**全部**头 —— 除此之外浏览器带来的什么都不转。"""
        return {
            "Cookie": self.cookie_header(),
            CONSOLE_CSRF_HEADER: self.csrf_token,
            "Accept": "application/json",
        }


def _is_token(value: str) -> bool:
    return 0 < len(value) <= MAX_TOKEN_CHARS and _TOKEN_RE.fullmatch(value) is not None


def console_session_from_headers(cookie_header: str | None, csrf_header: str | None) -> ConsoleSession | None:
    """从 ``Cookie:`` 与 ``X-CSRF-Token:`` 取出完整的控制台凭据；不完整就是 ``None``。

    规则（任一不满足 ⇒ ``None`` ⇒ 拒绝，而不是抛异常）：

    * ``access_token`` 与 ``csrf_token`` 两枚 cookie **各恰好一次**（同名出现两次时
      不知道 Dify 会取哪一个，桥不替它猜）；
    * 值是合法 cookie-octet（它们会被原样拼进转发的 ``Cookie`` 头）；
    * ``X-CSRF-Token`` 与 ``csrf_token`` cookie 逐字相同（Dify 的双提交规则，桥先在本地判，
      不合规的请求不去打扰 Dify）。

    不用 ``http.cookies``：它对畸形输入会抛异常或静默跳过，这里两者都不是想要的。
    """
    if not cookie_header or not csrf_header:
        return None
    found: dict[str, list[str]] = {
        prefix + name: []
        for prefix in ("", "__Host-")
        for name in (CONSOLE_ACCESS_TOKEN_COOKIE, CONSOLE_CSRF_COOKIE)
    }
    for chunk in cookie_header.split(";"):
        name, sep, value = chunk.strip().partition("=")
        if name in found:
            found[name].append(value if sep else "")
    plain = bool(found[CONSOLE_ACCESS_TOKEN_COOKIE] or found[CONSOLE_CSRF_COOKIE])
    prefixed = bool(found["__Host-access_token"] or found["__Host-csrf_token"])
    if plain and prefixed:
        return None
    prefix = "__Host-" if prefixed else ""
    access, csrf = found[prefix + CONSOLE_ACCESS_TOKEN_COOKIE], found[prefix + CONSOLE_CSRF_COOKIE]
    if len(access) != 1 or len(csrf) != 1:
        return None
    if not (_is_token(access[0]) and _is_token(csrf[0]) and _is_token(csrf_header)):
        return None
    if not hmac.compare_digest(csrf[0].encode("ascii"), csrf_header.encode("ascii")):
        return None
    return ConsoleSession(access_token=access[0], csrf_token=csrf[0], cookie_prefix=prefix)


# ── 来源与 app_id ───────────────────────────────────────────────────────────


def _origin_and_path(raw: str | None) -> tuple[str, urllib.parse.SplitResult] | None:
    """把一个绝对 http(s) URL 拆成（规范化的源, 拆分结果）；不是就 ``None``。

    规范化按 RFC 6454 的源三元组：协议与主机小写、默认端口省略。带 userinfo 的
    URL 一律不认（``http://x@host`` 常被用来让人眼看错主机）。
    """
    if not raw or any(ord(char) <= 32 or ord(char) == 127 for char in raw):
        return None
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in {"http", "https"} or not host or "@" in parts.netloc:
        return None
    if ":" in host:  # IPv6 字面量
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    origin = f"{scheme}://{host}" if port in {None, default_port} else f"{scheme}://{host}:{port}"
    return origin, parts


def normalize_origin(raw: str | None) -> str | None:
    """规范化一个**源**（只有协议/主机/端口，可带一个结尾 ``/``）；否则 ``None``。

    ``"null"``（不透明源）、带路径/查询/片段的值都不是源。
    """
    split = _origin_and_path(raw)
    if split is None:
        return None
    origin, parts = split
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        return None
    return origin


def _is_canonical_uuid(candidate: str) -> bool:
    """Dify 的 app id 是 uuid4，路由里是小写带连字符的规范形；别的形状一律不认。"""
    try:
        return str(uuid.UUID(candidate)) == candidate
    except ValueError:
        return False


def app_id_from_referer(referer: str | None, *, console_origin: str) -> str | None:
    """从**控制台本源页面**的 URL 里取出当前 app 的 id，取不到就是 ``None``。

    这是 app_id 的唯一来源。它校验的是**请求上下文**（像不像从我们控制台某个 app 页面
    点的），**不是授权**：Referer 由调用方控制，非浏览器客户端可以写任意 app 的 uuid。
    越权由后续 Dify 对该会话的工作空间/角色/app 回答挡住。三条都要满足：

    * Referer 的源**精确等于**配置的控制台公开源（外来页面不算）；
    * 路径形如 Dify 自己的路由 ``/app/<id>/<页>``（``configuration`` / ``workflow`` …）；
    * ``<id>`` 是规范 uuid。

    取不到时返回 ``None``，调用方据此拒绝；**猜一个默认值**在这里是最坏的做法。
    """
    expected = normalize_origin(console_origin)
    split = _origin_and_path(referer)
    if expected is None or split is None:
        return None
    origin, parts = split
    if origin != expected:
        return None
    segments = parts.path.split("/")
    # "/app/<id>/<页>..." → ["", "app", "<id>", "<页>", ...]
    if len(segments) < 4 or segments[0] != "" or segments[1] != "app" or not segments[3]:
        return None
    candidate = segments[2]
    return candidate if _is_canonical_uuid(candidate) else None


def parse_allowed_roles(raw: str | None) -> frozenset[str]:
    """解析 ``AIOPS_CONSOLE_BRIDGE_ALLOWED_ROLES``（逗号分隔）；缺省为所有者/管理员。

    未知角色名**启动即失败** —— 拼错一个角色名静默变成"谁都不放行"或"放行了别的"
    都比报错更糟。
    """
    if raw is None or not raw.strip():
        return DEFAULT_ALLOWED_ROLES
    roles = frozenset(item.strip() for item in raw.split(",") if item.strip())
    if not roles:
        raise ValueError("AIOPS_CONSOLE_BRIDGE_ALLOWED_ROLES must name at least one role")
    unknown = roles - KNOWN_ROLES
    if unknown:
        raise ValueError(f"AIOPS_CONSOLE_BRIDGE_ALLOWED_ROLES has unknown roles: {sorted(unknown)}")
    return roles


# ── 出站：一跳有界 HTTP ─────────────────────────────────────────────────────


def _any_json(payload: Any, mapping: ErrorMapping) -> Any:
    """不套信封：解码后的 JSON 原样交给调用方，**形状由调用方逐字段判**。

    不用 ``parse_raw_envelope``：它把标量载荷判成传输故障（502），而这里一个 2xx 的
    标量回答应当走"形状不对"的同一条拒绝/未确认分支。
    """
    del mapping
    return payload


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any]:
    """一跳有界 HTTP，走仓库**唯一**的出站骨架（``bounded_http``）。

    返回 ``(status, decoded_json)``。两条硬约束：

    * **不跟随重定向**（``follow_redirects=False``）：3xx 原样作为状态码返回，调用方按
      "非 2xx"拒绝。跟随会把运营的 cookie / 服务端密钥带去 ``Location``。
    * **响应体有上限**：超限即截断，JSON 解码失败 → ``ConsolePublishError``。

    这里**不复用骨架的错误映射**是有意的：这一层要"拿状态码自己判"。桥自己那一步失败
    （连不上/超时/解码不了）抛 ``ConsolePublishError``。
    """
    failures: list[HttpFailure] = []

    def _capture(failure: HttpFailure) -> Exception:
        failures.append(failure)
        return ConsolePublishError(f"{method} {url} 失败：HTTP {failure.status}")

    def _transport(failure: HttpFailure) -> Exception:
        failures.append(failure)
        return ConsolePublishError(f"{method} {url} 失败：{failure.kind}")

    mapping = ErrorMapping(
        auth_rejected=_capture,
        http_error=_capture,
        unavailable=_transport,
        invalid_body=_transport,
        invalid_envelope=_transport,
    )
    spec = RequestSpec(
        url=url,
        method=method,
        headers=dict(headers or {}),
        body=json_body(dict(body)) if body is not None else None,
        timeout=timeout,
        max_read_bytes=MAX_UPSTREAM_RESPONSE_BYTES,
        follow_redirects=False,
    )
    try:
        payload = request_json(spec, mapping=mapping, envelope=_any_json)
    except ConsolePublishError:
        # 骨架把 HTTP 状态折成了异常 —— 那不是"桥失败了"，是**上游明确回答**（含 3xx）。
        if not failures or failures[0].status is None:
            # 传输/解码故障**没有状态码** —— "这一步没做成"，让上层的 502 分支接住。
            raise
        failure = failures[0]
        # **把上游自己的错误码带回去。** 没有它，桥只能按 HTTP 状态猜意思，而同一个
        # 404 在网关上有两种完全不同的含义：``DIFY_APP_NOT_BOUND``（这个 app 没登记）
        # 与 ``ORDER_NOT_FOUND``（这张订单看不到）。实测（2026-10-20）：试运行时前者被
        # 误报成"没有登记成绑定来源"，而真实原因是后者，运营照着那句话查了半天绑定表。
        #
        # 形状保持与网关一致（``{"error": {...}}``），所以下游读 ``error.code`` 的代码
        # 不必知道这条 body 是桥重建的。
        return failure.status, (
            {"error": {"code": failure.code, "message": failure.detail}}
            if failure.code or failure.detail
            else None
        )
    return 200, payload


def _str_field(payload: Any, key: str) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


# ── 一次点击 ────────────────────────────────────────────────────────────────

_REFUSED_SESSION = "Dify 会话已失效或你看不到这个智能体，请重新登录后再试"
_REFUSED_ROLE = "只有工作空间的所有者或管理员可以发布到线上"
_REFUSED_WORKSPACE = "当前不在可发布的工作空间，请切换到正确的工作空间后再试"
_UNCONFIRMED = "发布结果无法确认，请到 AI-Ops 核对线上版本后再试"


def publish_current_app(
    *,
    dify_base_url: str,
    gateway_base_url: str,
    publish_key: str,
    console_session: ConsoleSession,
    console_origin: str,
    workspace_id: str,
    referer: str | None,
    allowed_roles: frozenset[str] = DEFAULT_ALLOWED_ROLES,
    login_proxy_url: str = "",
    gateway_internal_token: str = "",
    timeout: float = 15.0,
    operation: str = "publish",
    version_no: int | None = None,
) -> ConsolePublishResult:
    """运营点了按钮之后，这一层做的全部事情。顺序不能换：

    1. 从 Referer 取当前 app 的 id（页面必须在控制台公开源上、id 是规范 uuid）；
       取不到 ⇒ 拒绝（**不猜**），一个上游都不问。这一步是请求上下文，不是授权。
    2. 问 Dify ``GET /console/api/workspaces/current/summary``：非 2xx、``id`` 不等于
       钉住的 ``workspace_id``、或 ``role`` 不在 ``allowed_roles`` 里 ⇒ 拒绝，
       **不查 app、不打网关**。能看见 app 不等于能发布。
    3. 问 Dify ``GET /console/api/apps/<id>``：非 2xx、或回答的 ``id`` 不是这个 app ⇒
       拒绝。Dify 按**当前**工作空间限定这一查。若运营恰在第 2、3 步之间切了工作空间，
       这一查可能落在别的空间并成功 —— 桥关不掉这个窗口（app 回答不带 ``tenant_id``）；
       它由第 4 步关上：网关以配置的工作空间导出 DSL，别的空间的 app 导不出来。
    4. 用**服务端**密钥调 ``POST /v1/dify/publish``，body 只有 ``app_id``；
       网关只接受**已绑定**的 app，并把它的回答逐字段核对后才报成功。
    """
    app_id = app_id_from_referer(referer, console_origin=console_origin)
    if app_id is None:
        return ConsolePublishResult(False, "无法确定当前是哪个智能体（请从编排页点这个按钮）")

    dify_base = dify_base_url.rstrip("/")
    dify_headers = console_session.dify_headers()

    http_status, summary = _request(
        dify_base + DIFY_WORKSPACE_SUMMARY_PATH, headers=dify_headers, timeout=timeout
    )
    if not 200 <= http_status < 300:
        return ConsolePublishResult(False, _REFUSED_SESSION)
    role = _str_field(summary, "role")
    current_workspace = _str_field(summary, "id")
    if role is None or current_workspace is None:
        _LOGGER.info("console publish refused: workspace summary has no role/id")
        return ConsolePublishResult(False, _REFUSED_SESSION)
    # 放行判据（#780）与**钉住的 workspace**（#884）。两者共用同一次身份查询：
    # 钉住的值现在是"**发起人所在的那个** workspace"，而不是一个配置常量。
    #
    # 顺序是刻意的：先拿到身份（因此拿到发起人的公司租户），再解析 workspace，
    # 最后才比对 summary 的当前工作空间。反过来的话，一个还没开户的租户会在
    # "工作空间不符"上被拒，而真正的原因是"他没有工作区"—— 两种下一步完全不同。
    initiator_headers: dict[str, str] = {}
    pinned_workspace = workspace_id
    if login_proxy_url:
        verdict = _identity_and_publish_verdict(
            login_proxy_url,
            console_session=console_session,
            timeout=timeout,
        )
        if verdict is None:
            # 查不到 = 代理不可达或会话不是我们签的：**拒**（fail-closed），
            # 不退回 owner/admin —— 那会让"查不通"悄悄变成"按旧规则放行"。
            _LOGGER.info("console publish refused: publish-permission check unavailable")
            return ConsolePublishResult(False, "暂时无法核对发布权限，请稍后重试")
        initiator_headers, allowed = verdict
        if not allowed:
            _LOGGER.info("console publish refused: no company publish permission")
            return ConsolePublishResult(False, _REFUSED_PUBLISH_PERMISSION)
        if gateway_internal_token:
            tenant_id = initiator_headers.get("X-AIOps-Initiator-Tenant", "")
            # 平台账号（`company_user_type == PLATFORM_USER_TYPE`，即公司体系的 `-1`）
            # 不属于任何 `sys_organ`，它的 `tenant_id` 是哨兵值 `-1` 而不是实体 id。
            # 拿 `-1` 去查 workspace 会在网关的参数校验就失败（`_SAFE_ID` 要求首字符是
            # 字母或数字），网关回 400，而桥把所有非 2xx 都译成「这个租户没有工作区」——
            # 于是真正的原因（租户 id 非法）被抹成了一句不成立的话。
            #
            # 登录侧早就处理了这一层（`dify_login_service.py` 的 #882：平台账号落到
            # 它自己的 `platform` workspace），发布桥漏了同样的映射。实测 2026-10-25：
            # `system` 用真实会话点「发布到 AI-Ops」，桥日志 `workspace lookup failed:
            # _WorkspaceLookupFailed: 400`，用户看到「你所在的租户还没有可用的 Dify 工作区」。
            #
            # #882 明写平台账号的「可见全部」不通过进别人的 workspace 体现（ADR-0059 §4）：
            # 编辑面只在它自己的平台 workspace。这里照同一条口径。
            if initiator_headers.get("X-AIOps-Initiator-Type", "").strip() == PLATFORM_USER_TYPE:
                tenant_id = PLATFORM_TENANT_ID
            resolved = _workspace_for(
                gateway_base_url,
                gateway_internal_token,
                tenant_id=tenant_id,
                timeout=timeout,
            )
            if resolved is None:
                # **不回落任何默认 workspace**：回落等于把"这个租户还没开户"悄悄变成
                # "发布进别人的工作区"，而后者看起来是成功的。
                _LOGGER.info("console publish refused: the initiator's tenant has no workspace")
                return ConsolePublishResult(False, _REFUSED_NO_WORKSPACE)
            pinned_workspace = resolved
    elif role not in allowed_roles:
        _LOGGER.info("console publish refused: role %r not allowed", role[:32])
        return ConsolePublishResult(False, _REFUSED_ROLE)

    if not hmac.compare_digest(current_workspace.encode("utf-8"), pinned_workspace.encode("utf-8")):
        # 不记录对方的工作空间 id：日志只需知道"不是钉住的那个"。
        _LOGGER.info("console publish refused: current workspace is not the pinned one")
        return ConsolePublishResult(False, _REFUSED_WORKSPACE)

    app_url = dify_base + DIFY_APP_PATH.format(app_id=urllib.parse.quote(app_id, safe=""))
    http_status, app = _request(app_url, headers=dify_headers, timeout=timeout)
    if not 200 <= http_status < 300 or _str_field(app, "id") != app_id:
        return ConsolePublishResult(False, _REFUSED_SESSION)

    http_status, outcome = _request(
        gateway_base_url.rstrip("/")
        + {
            "publish": PUBLISH_PATH,
            "retract": "/v1/dify/retract",
            "restore": "/v1/dify/restore",
            "versions": "/v1/dify/versions",
        }[operation],
        method="POST",
        headers={
            "Authorization": f"Bearer {publish_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            **initiator_headers,
        },
        body={"app_id": app_id, **({"version_no": version_no} if operation == "restore" else {})},
        timeout=timeout,
    )
    if not 200 <= http_status < 300:
        # 发布门的错误码不透出去：对点按钮的人，它们是同一件事。
        _LOGGER.info("console publish refused by gateway: HTTP %s", http_status)
        return ConsolePublishResult(False, "发布未通过我们的发布门，请联系工程查看原因")
    if operation == "retract":
        ok = (
            isinstance(outcome, dict)
            and outcome.get("app_id") == app_id
            and outcome.get("state") == "retracted"
        )
        return ConsolePublishResult(
            ok,
            "已撤回正式版本" if ok else _UNCONFIRMED,
            action="retracted" if ok else "",
            status=200 if ok else 502,
        )
    if operation == "versions":
        if not isinstance(outcome, dict) or outcome.get("app_id") != app_id:
            return ConsolePublishResult(False, _UNCONFIRMED, status=502)
        versions = outcome.get("versions")
        if not isinstance(versions, list):
            return ConsolePublishResult(False, _UNCONFIRMED, status=502)
        return ConsolePublishResult(True, "可恢复的历史版本", status=200, versions=tuple(versions))
    return _interpret_publish_outcome(outcome, app_id)


def _identity_and_publish_verdict_for_identity(
    login_proxy_url: str,
    *,
    console_session: ConsoleSession,
    timeout: float,
) -> tuple[dict[str, str], bool] | None:
    """只要**身份**，不看发布权（#984）。

    与 ``_identity_and_publish_verdict`` 是同一个上游调用、同一套解析 —— 分成两个名字
    是因为**读法不同**：知识库入口只问"这个会话是谁"，发布权（`allowed`）与它无关。
    运营能进自己租户的知识库，不该因为他没有"发布智能体"的权限而被挡住。

    返回值语义沿用上游：``None`` = 查不通 / 会话不是代理签的 / 没有映射。
    """
    return _identity_and_publish_verdict(login_proxy_url, console_session=console_session, timeout=timeout)


def _knowledge_sync_payload(
    gateway_base_url: str, internal_token: str, *, workspace_id: str, timeout: float
) -> int:
    """让网关收敛一次，返回它建了几条 dataset（ADR-0122）。

    ``workspace`` 是**桥的固定配置**，不是请求带来的 —— 网关按它反查租户，与
    ``/v1/internal/knowledge/open`` 同一条判据。返回值只用于"要不要刷新列表"，
    调用方不该从它推断业务状态。
    """
    from aiops_diagnostics.bounded_http import (
        ErrorMapping,
        RequestSpec,
        join_url,
        request_json,
    )
    from aiops_diagnostics.http_auth import build_internal_token_headers

    def failed(failure: HttpFailure) -> Exception:
        return ConsolePublishError(f"knowledge sync: {failure.status or failure.detail}")

    payload = request_json(
        RequestSpec(
            url=join_url(
                gateway_base_url,
                "/v1/internal/knowledge/sync",
                query={"workspace": workspace_id},
            ),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **build_internal_token_headers(internal_token, 300, int(_time_module.time())),
            },
            body=b"{}",
            timeout=timeout,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(
            auth_rejected=failed,
            http_error=failed,
            unavailable=failed,
            invalid_body=failed,
            invalid_envelope=failed,
        ),
    )
    if not isinstance(payload, dict):
        raise ConsolePublishError("knowledge sync: gateway returned a non-object")
    # **「有没有变化」不等于「建了几条」。** 网关回的是 `changed` = 建 + 改 + 退休
    # （ADR-0125）；只取 `created` 会让运营改名/删库之后页面不刷新 —— 卡片留在原地，
    # 点进去 404。旧网关没有 `changed`，回落 `created`。
    for key in ("changed", "created"):
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _knowledge_open_payload(
    gateway_base_url: str, internal_token: str, *, workspace_id: str, timeout: float
) -> dict[str, Any]:
    """问网关"这个租户的 RAGFlow 入口在哪"，拿到后就跳过去（#982 的端点）。

    网关那条路由按**内部令牌**鉴权，租户来自它自己按 workspace 反查 —— 桥这里传的是
    **登录代理验过的** ``company_tenant_id``，而网关按 workspace 反查同一个租户。
    两边都从"验过的会话"出发，没有一处读 URL。

    失败一律抛 :class:`ConsolePublishError` 交给调用点译成可读的一句话 —— 桥不猜
    "为什么没成"，那是网关的四个可区分码的职责。
    """
    from aiops_diagnostics.bounded_http import ErrorMapping, RequestSpec, join_url, request_json
    from aiops_diagnostics.http_auth import build_internal_token_headers

    # 网关的 409 是"这个租户还没开 RAGFlow"—— 那是运营**能行动**的信息（去找管理员
    # 开户），不该被压成"暂时不可用"。其余状态码保持"这一步没做成"。
    _REFUSALS = {
        409: "这个租户还没有开通 RAGFlow 知识库，请联系管理员开户后再试",
        410: "入口链接已过期，请回到 Dify 重新点击",
    }

    def failed(failure: HttpFailure) -> Exception:
        status = failure.status or 0
        message = _REFUSALS.get(status)
        if message:
            return _KnowledgeRefused(status, message)
        return ConsolePublishError(f"knowledge open: {status or failure.detail}")

    try:
        payload = request_json(
            RequestSpec(
                url=join_url(
                    gateway_base_url,
                    "/v1/internal/knowledge/open",
                    query={"workspace": workspace_id},
                ),
                method="GET",
                headers={
                    "Accept": "application/json",
                    **build_internal_token_headers(internal_token, 300, int(_time_module.time())),
                },
                timeout=timeout,
                follow_redirects=False,
            ),
            mapping=ErrorMapping(
                auth_rejected=failed,
                http_error=failed,
                unavailable=failed,
                invalid_body=failed,
                invalid_envelope=failed,
            ),
        )
    except _KnowledgeRefused:
        # 运营能行动的拒绝（未开户 / 链接过期）原样上抛 —— 它已经有对的文案与状态码，
        # 再包一层就退回成"暂时不可用"。
        raise
    except Exception as exc:  # noqa: BLE001 - 一步失败，交给调用点说人话
        raise ConsolePublishError(f"knowledge open: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("target"):
        raise ConsolePublishError("knowledge open: gateway returned no target")
    return payload


def _identity_and_publish_verdict(
    login_proxy_url: str,
    *,
    console_session: ConsoleSession,
    timeout: float,
) -> tuple[dict[str, str], bool] | None:
    """问代理两件事：这个会话**是谁**，以及他**能不能发布**（#780 / #782）。

    ``None`` = 查不通、会话不是我们签的、或没有可用映射 —— **身份也拿不到**。
    两个调用方（发布与试运行）都据此拒绝，理由各自写在调用处。

    ``(headers, False)`` 是**合法且常见**的组合：有映射、身份齐全，但缺发布权限码。
    试运行只要求「这个会话是公司认可的人」，**不要求发布权** —— 所以发布路径看第二个
    返回值，试运行只看第一个。把两者混成一个布尔会让「只有编辑权的人连试运行都做不了」。

    ``headers`` 是发起人身份，给网关做审计账本与**试运行范围**。对发布而言它们只是
    审计元数据（授权已在桥这边判完）；对试运行而言 ``-Tenant`` / ``-Type`` 是**范围
    的输入** —— 网关按它们算订单可见性。所以身份字段虽不是"授权"，在试运行那条路上
    它确实改变行为，这一点与发布不同，写在这里以免被误读成同一种东西。

    **身份是否齐全由字段在不在判定**（``company_user_id`` + ``company_tenant_id``
    都非空），不解析 ``reason`` 字符串 —— 那会把代理的文案变成桥的契约。
    ``company_tenant_id`` 为空必须当作"身份不全"而不是"照发"：网关的
    ``TrialInitiator.from_headers`` 在租户为空时会**静默退回整租户的调试租户行为**，
    那是一次范围放宽，不能由上游一个空字段触发。

    代理验的是**它自己签的**会话，而浏览器的 Dify 控制台 cookie 就是那枚令牌
    （桥原样转发、从不读取或重签）。
    """
    try:
        http_status, verdict = _request(
            login_proxy_url.rstrip("/") + _PUBLISH_CHECK_PATH,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body={"session": console_session.access_token},
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - 查不通一律按"不能确认"处置
        return None
    if not 200 <= http_status < 300 or not isinstance(verdict, dict):
        return None
    company_user_id = (_str_field(verdict, "company_user_id") or "").strip()
    company_tenant_id = (_str_field(verdict, "company_tenant_id") or "").strip()
    if not company_user_id or not company_tenant_id:
        # 没有映射 / 映射被停用 / 会话无效：代理这两个字段是空的（或整个没回）。
        _LOGGER.info("console identity: proxy did not resolve a company identity")
        return None
    headers = {"X-AIOps-Initiator": company_user_id}
    company_username = (_str_field(verdict, "company_username") or "").strip()
    if company_username:
        headers["X-AIOps-Initiator-Name"] = company_username
    dify_account_id = (_str_field(verdict, "dify_account_id") or "").strip()
    if dify_account_id:
        headers["X-AIOps-Dify-Account"] = dify_account_id
    # #782：试运行的订单范围来自这两项，网关按发起人的公司身份算可见站点。
    headers["X-AIOps-Initiator-Tenant"] = company_tenant_id
    company_user_type = (_str_field(verdict, "company_user_type") or "").strip()
    if company_user_type:
        headers["X-AIOps-Initiator-Type"] = company_user_type
    return headers, verdict.get("allowed") is True


def _interpret_publish_outcome(outcome: Any, app_id: str) -> ConsolePublishResult:
    """网关 2xx 的回答**逐字段**核对后才算成功（形状取自 ``_publish_bound_app``）。

    * ``action`` ∈ {``refused``, ``failed``} ⇒ 发布门拒绝（409）；
    * ``action`` ∈ {``synced``, ``unchanged``} 且 ``version_no`` 是正整数、``app_id``
      就是请求的那个 ⇒ 成功；
    * 其余一切 ⇒ 结果未确认（502）。不能报成功，也不能断言没发 —— 让人去核对。
    """
    action = _str_field(outcome, "action")
    if _str_field(outcome, "app_id") != app_id:
        _LOGGER.warning("console publish: gateway answer does not name the requested app")
        return ConsolePublishResult(False, _UNCONFIRMED, status=502)
    if action in _REFUSED_ACTIONS:
        _LOGGER.info("console publish refused by publish gate: action=%s", action)
        return ConsolePublishResult(False, "发布门拒绝了这次改动，请联系工程查看原因", status=409)
    version = outcome.get("version_no")
    if action not in _PUBLISHED_ACTIONS or type(version) is not int or version < 1:
        _LOGGER.warning("console publish: gateway answer has an unexpected shape")
        return ConsolePublishResult(False, _UNCONFIRMED, status=502)
    if action == "unchanged":
        return ConsolePublishResult(True, f"线上已是最新（版本 {version}），无需改动", action, status=200)
    return ConsolePublishResult(True, f"已发布到线上（版本 {version}）", action, status=200)


def submit_trial(
    *,
    gateway_base_url: str,
    trial_key: str,
    app_id: str,
    order_no: str,
    login_proxy_url: str = "",
    console_session: ConsoleSession | None = None,
    timeout: float = 15.0,
) -> ConsolePublishResult:
    """把一次试运行**提交**给网关（#781 / #782 的部署层）。

    与 :func:`publish_current_app` 的形状差别只有一处，而且是**有意**的：

    * 发布那条路把"读草稿 + 映射工具"整件事留在网关里（``/v1/dify/publish`` 自己拉 DSL）；
    * 试运行同样如此 —— 桥只递 ``app_id`` 与 ``order_no``，**草稿由网关自己读**。
      桥这一侧读不懂那份草稿：导出的 DSL 里工具条目只有提供方的 UUID，映射规则
      （``partition_aiops_tools``）住在网关，而且 36 上的 python 没有 pydantic，
      桥连 ``dify_dsl_pull`` 都 import 不了。让桥再实现一遍就是第二份规则。

    ## 发起人身份（#782）：试运行的**订单范围**由它决定

    配了 ``login_proxy_url`` 就先去问代理"这个 Dify 会话是谁"，把公司身份当请求头
    转给网关，网关据此算这个人的可见站点（顶层账号整租户／代理商只看自己据点）。
    **只取身份，不看发布权** —— 拿不到发布权的人照样能试运行（他没越过任何授权：
    试运行不产生版本、不改路由）。

    ## 取不到身份时**退回固定调试租户**（用户 2026-10-16 选定）

    这里与发布那条路的 fail-closed **方向不同**，写下来不藏：发布那边"配了但查不通"
    ⇒ 拒，因为退回旧规则等于放宽**授权**。试运行这边退回调试租户意味着范围从
    "只看自己据点"放宽到"看整个调试租户"—— 但那是一个**已经上了线的既有行为**
    （#781 的第一版就是这个），而不是一个新的越权：钥匙仍然是那把独立的试运行钥匙，
    调用方仍然是控制台里的会话。选它的理由是身份闭环刚上线时可能有人还没映射，
    退回能让功能不断。

    代价明确、可单独收紧：把下面那个 ``return`` 换成拒绝即可（一行），或把这条路
    改成 fail-closed。**先让功能可用，再谈收紧**，而两者的切换点只有这一处。
    """
    initiator_headers = _initiator_headers_for(
        login_proxy_url, console_session=console_session, timeout=timeout
    )
    if console_session is not None:
        initiator_headers["X-AIOps-Console-Session"] = console_session.access_token
    http_status, answer = _request(
        gateway_base_url.rstrip("/") + TRIAL_PATH,
        method="POST",
        headers={
            # 这是**第六把**独立钥匙：能发布不等于能在真数据上跑一次诊断。
            "Authorization": f"Bearer {trial_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            **initiator_headers,
        },
        body={"app_id": app_id, "order_no": order_no},
        timeout=timeout,
    )
    # 名字用 ``http_status`` 与本文件其余部分一致（``publish_current_app`` 也是这么叫的）：
    # 裸名 ``status`` 会让 ``tests/test_order_status.py`` 的结构判据把它当成**订单状态**
    # 的内联数字判据 —— 那条判据是 PRD #732 用来拦住"订单状态又多一份拼写"的，不该为了
    # 一个 HTTP 状态码去放宽它。
    #
    # **404 有两种含义，必须按网关自己的错误码分开**（实测 2026-10-20）：
    # ``DIFY_APP_NOT_BOUND`` 是这个 app 没登记；``ORDER_NOT_FOUND`` 是那张订单看不到
    # （越权与不存在同形）。此前一律按前者回话，于是运营看到"没有登记成绑定来源"，
    # 去查绑定表，而真正的原因是订单范围 —— 一句话把人指向了错误的地方。
    error = answer.get("error") if isinstance(answer, dict) else None
    error_code = (_str_field(error, "code") or "").strip()
    if http_status == 404:
        if error_code == "DIFY_APP_NOT_BOUND":
            return ConsolePublishResult(
                False, "这个智能体没有登记成 AI-Ops 的绑定来源，无法试运行", status=404
            )
        if error_code == "ORDER_NOT_FOUND":
            return ConsolePublishResult(
                False,
                "这张订单不在你的可见范围内（或不存在）。试运行只对你自己能看到的订单生效",
                status=404,
            )
        return ConsolePublishResult(False, _UNCONFIRMED, status=502)
    if http_status == 429:
        return ConsolePublishResult(False, "已经有一次试运行在跑，请等它结束再试", status=429)
    if not 200 <= http_status < 300 or not isinstance(answer, dict):
        _LOGGER.warning("console trial: gateway answered status=%s", http_status)
        return ConsolePublishResult(False, _UNCONFIRMED, status=502)
    trial_id = _str_field(answer, "trial_id")
    trial_status = _str_field(answer, "status")
    if not trial_id or not trial_status:
        _LOGGER.warning("console trial: gateway answer has an unexpected shape")
        return ConsolePublishResult(False, _UNCONFIRMED, status=502)
    # ``action`` 里带回 trial_id：按钮靠它去轮询结果面板。它不是新授权 —— 读回那条
    # 路由仍然要求同一个发起人和同一把钥匙（见 :func:`read_trial`）。
    return ConsolePublishResult(
        True,
        f"已提交试运行（{trial_id}），状态：{trial_status}",
        trial_id=trial_id,
        status=202,
    )


#: 结果面板一次读回的字段上限。诊断报告本身 ≤ 8KB（契约里的字段长度上限之和远小于它），
#: 这里给足余量，同时不给一个异常上游把桥的内存撑开的机会。
MAX_TRIAL_RESULT_TEXT = 4000


def read_trial(
    *,
    gateway_base_url: str,
    trial_key: str,
    trial_id: str,
    login_proxy_url: str = "",
    console_session: ConsoleSession | None = None,
    timeout: float = 15.0,
) -> TrialReport:
    """把一次试运行的状态与结果读回来（结果面板那一条）。

    **它与 :func:`submit_trial` 共用同一个发起人身份**：网关那条 GET 按
    ``scope_fingerprint`` 隔离作业行，而那个指纹由发起人的公司身份算出。不带同样的
    ``X-AIOps-Initiator*`` 头去读，回来的一定是 404 —— 而且与"这个 id 不存在"
    完全同形。所以取身份那段抽成 :func:`_initiator_headers_for`，两条路都调它。

    这里**不接 app_id**：网关那条 GET 只按 ``trial_id`` + 范围找行，请求里没有 app
    这一维；桥侧的门（同源 + CSRF + Referer 必须落在某个智能体页）已经在调用方过完了。

    返回的 ``status`` 由调用方用 :data:`TRIAL_TERMINAL_STATUSES` 判终态；
    本函数**不**自己判"完成了没有"，那份词表只在一处。
    """
    if login_proxy_url and console_session is None:
        return TrialReport(False, "会话缺失，无法核对身份", status=403)
    initiator_headers = _initiator_headers_for(
        login_proxy_url, console_session=console_session, timeout=timeout
    )
    if console_session is not None:
        initiator_headers["X-AIOps-Console-Session"] = console_session.access_token
    http_status, answer = _request(
        gateway_base_url.rstrip("/") + f"{TRIAL_PATH}/{urllib.parse.quote(trial_id, safe='')}",
        method="GET",
        headers={
            "Authorization": f"Bearer {trial_key}",
            "Accept": "application/json",
            **initiator_headers,
        },
        timeout=timeout,
    )
    if http_status == 404:
        return TrialReport(False, "读不到这次试运行（可能已过期，或不属于你的可见范围）", status=404)
    if not 200 <= http_status < 300 or not isinstance(answer, dict):
        _LOGGER.warning("console trial result: gateway answered status=%s", http_status)
        return TrialReport(False, _UNCONFIRMED, status=502)
    status_text = _str_field(answer, "status")
    if not status_text:
        _LOGGER.warning("console trial result: gateway answer has no status")
        return TrialReport(False, _UNCONFIRMED, status=502)
    return TrialReport(
        True,
        _trial_report_text(answer, status_text),
        status=200,
        job_status=status_text,
        terminal=status_text in TRIAL_TERMINAL_STATUSES,
    )


@dataclass(frozen=True, slots=True)
class TrialReport:
    """一次结果读取的回答：可读的一句话 + 原始状态。"""

    ok: bool
    reason: str
    status: int = 200
    #: 网关给的作业状态（``queued``/``running``/终态之一）。读失败时是空串。
    job_status: str = ""
    #: 终态了吗 —— 用**唯一那份**词表判（``TRIAL_TERMINAL_STATUSES``）。
    terminal: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "read": self.ok,
            "reason": self.reason,
            "status": self.job_status,
            "terminal": self.terminal,
        }


def _trial_report_text(answer: Mapping[str, Any], status_text: str) -> str:
    """把网关的结果压成一段**运营能读**的文字。

    三种终态各有各的说法，这里不合并：
    * ``completed`` —— 报告里的 ``summary`` 与 ``root_cause``；
    * 其余终态（``failed`` / ``expired`` / ``cancelled`` / ``inconclusive``）——
      直说它是哪一种，不冒充"完成"；
    * 还在跑 —— 报当前状态，让面板继续轮询。

    证据 id 与失败来源**照实列出**：它们是"这个结论有据"与"哪几个源没取到"的唯一
    可见面，压在报告里就等于没有。
    """
    if status_text not in TRIAL_TERMINAL_STATUSES:
        return f"试运行进行中（{status_text}）"
    if status_text != "completed":
        # 失败/过期/取消/未决：网关给一句**按请求语言渲染**的原因（`error_message`，
        # 与诊断面同一张表）。没有它就只剩一个状态词 —— 而"failed"不告诉运营
        # 该做什么（实测 2026-10-21：欠费导致的失败与代码缺陷长得一模一样）。
        reason = str(answer.get("error_message") or "").strip()
        code = str(answer.get("error_code") or "").strip()
        text = f"试运行结束，状态：{status_text}"
        if reason:
            text += f"：{reason}"
        if code:
            text += f"（{code}）"
        return text[:MAX_TRIAL_RESULT_TEXT]
    result = answer.get("result")
    report = result.get("report") if isinstance(result, Mapping) else None
    if not isinstance(report, Mapping):
        return "试运行完成，但结果里没有可读的报告"
    summary = str(report.get("summary") or "").strip()
    root_cause = str(report.get("root_cause") or "").strip()
    parts = [part for part in (summary, root_cause) if part]
    text = " ／ ".join(parts) or "试运行完成，报告为空"
    evidence = report.get("evidence_ids")
    if isinstance(evidence, list) and evidence:
        text += f"（证据 {len(evidence)} 条：{', '.join(str(item) for item in evidence[:5])}）"
    failed = report.get("failed_sources")
    if isinstance(failed, list) and failed:
        text += f"（未取到的数据源：{', '.join(str(item) for item in failed[:5])}）"
    return text[:MAX_TRIAL_RESULT_TEXT]


def _initiator_headers_for(
    login_proxy_url: str,
    *,
    console_session: ConsoleSession | None,
    timeout: float,
) -> dict[str, str]:
    """发起人身份的头，**提交与读结果共用这一处**。

    两处各写一份就会漂：一边带了 ``-Tenant`` 一边没带，读回来的就永远是 404，
    而表现与"这个 id 不存在"完全同形（这是 #782 留下过的那个形态）。
    """
    if not login_proxy_url:
        return {}
    if console_session is None:  # pragma: no cover - 调用方保证成对给
        return {}
    verdict = _identity_and_publish_verdict(login_proxy_url, console_session=console_session, timeout=timeout)
    if verdict is None:
        _LOGGER.info("console trial: identity unavailable, falling back to the debug tenant")
        return {}
    identity_headers, _allowed = verdict
    return identity_headers


# ── HTTP 面：注入的按钮打的那一跳 ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    """桥的全部配置。**没有一样进浏览器** —— 这正是这一层的要点。"""

    listen_host: str
    listen_port: int
    #: Dify 自己（桥从服务端访问它的地址，通常是内网/回环）。桥拿运营的凭据去问它。
    dify_base_url: str
    #: 我们的网关。发布实现住在那里。
    gateway_base_url: str
    #: 服务端持有的发布密钥。与 ``AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY`` 同一把。
    publish_key: str
    #: 浏览器地址栏里的**控制台公开源**（如 ``http://36.156.159.175:10008``）。
    #: ``Origin`` / ``Referer`` 只按它判；**不是** ``dify_base_url``（那是内网地址）。
    console_origin: str
    #: **迁移期**的钉住工作空间 id（#884）。配了 ``gateway_internal_token`` 之后
    #: **不再参与判定** —— 钉住的值改为"发起人所在的那个 workspace"。
    #: 保留它只为一种情况：还没部署网关内部令牌的环境，行为逐字不变。
    workspace_id: str
    #: 网关自己的**内部令牌**（与网关的 `AIOPS_GATEWAY_INTERNAL_TOKEN_SECRET` 同一把）。
    #: 配了 ⇒ 桥按**发起人的公司租户**问网关"这个租户的 workspace 是哪一个"
    #: （`GET /v1/dify/workspace`），并用它作为钉住判据（#884）。留空 ⇒ 回落到静态
    #: `workspace_id`（迁移期行为）。
    #:
    #: 它**不是** Dify 的钥匙：调用方是我们自己的进程，输出只有一个 workspace UUID。
    gateway_internal_token: str = field(repr=False, default="")
    allowed_roles: frozenset[str] = DEFAULT_ALLOWED_ROLES
    #: 服务端持有的**试运行**密钥。与 ``AIOPS_GATEWAY_DIFY_TRIAL_API_KEY`` 同一把。
    #: 它必须与发布密钥**不同**（网关在启动期就强制两者互不相同）：能发布不等于能在
    #: 真数据上跑一次诊断，这是两条不同的授权。留空 ⇒ 试运行那条路 404。
    trial_key: str = ""
    #: 登录代理地址。它是**公司身份的来源开关**，一个键驱动三件事（#780 / #782）：
    #:
    #: 1. **发布权**：配了 ⇒ 放行判据换成公司 ``aiops:agent:publish`` 权限码；
    #:    留空 ⇒ 维持过渡行为（按 Dify owner/admin 判）。
    #: 2. **试运行的订单范围**：配了 ⇒ 按发起人的公司身份算可见站点；留空 ⇒ 固定调试租户。
    #: 3. **审计发起人**：配了 ⇒ 发布账本里记下是谁。
    #:
    #: "没配"与"配了但代理挂了"是两件不同的事：发布那条路后者 **fail-closed 拒**，
    #: 试运行那条路后者**退回固定调试租户**（理由写在 :func:`submit_trial`）。
    login_proxy_url: str = ""
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if normalize_origin(self.console_origin) != self.console_origin:
            raise ValueError("console_origin must be a normalized origin (scheme://host[:port])")
        if not self.workspace_id or self.workspace_id != self.workspace_id.strip():
            raise ValueError("workspace_id must be a non-empty id without surrounding whitespace")
        if not self.allowed_roles or not self.allowed_roles <= KNOWN_ROLES:
            raise ValueError("allowed_roles must be a non-empty subset of Dify roles")

    @classmethod
    def from_env(cls) -> BridgeSettings:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        origin = normalize_origin(required("AIOPS_CONSOLE_BRIDGE_CONSOLE_ORIGIN"))
        if origin is None:
            raise ValueError(
                "AIOPS_CONSOLE_BRIDGE_CONSOLE_ORIGIN must be an http(s) origin (scheme://host[:port])"
            )
        return cls(
            listen_host=os.environ.get("AIOPS_CONSOLE_BRIDGE_HOST", "127.0.0.1"),
            listen_port=int(os.environ.get("AIOPS_CONSOLE_BRIDGE_PORT", "10118")),
            dify_base_url=required("AIOPS_CONSOLE_BRIDGE_DIFY_URL").rstrip("/"),
            gateway_base_url=required("AIOPS_CONSOLE_BRIDGE_GATEWAY_URL").rstrip("/"),
            publish_key=required("AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY"),
            trial_key=os.environ.get("AIOPS_GATEWAY_DIFY_TRIAL_API_KEY", "").strip(),
            console_origin=origin,
            workspace_id=required("AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID"),
            gateway_internal_token=os.environ.get("AIOPS_CONSOLE_BRIDGE_GATEWAY_INTERNAL_TOKEN", "").strip(),
            allowed_roles=parse_allowed_roles(os.environ.get("AIOPS_CONSOLE_BRIDGE_ALLOWED_ROLES")),
            login_proxy_url=os.environ.get("AIOPS_CONSOLE_BRIDGE_LOGIN_PROXY_URL", "").strip().rstrip("/"),
        )


class _Rejected(Exception):  # noqa: N818 - 控制流信号，不是错误
    """请求在问任何上游之前就被拒了。"""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class ConsolePublishBridgeHandler(BaseHTTPRequestHandler):
    """一个端点：``POST /__aiops/publish``。

    与仓库里既有的 ``TDengineReadonlyHandler`` 同款（stdlib、有界、不做框架）。
    这里**只有这一个**路径 —— 桥不做代理、不做转发、不留后门。
    """

    settings: ClassVar[BridgeSettings]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的协议
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path != _KNOWLEDGE_OPEN_ROUTE:
                raise _Rejected(404, "not found")
            self._get_knowledge_open()
        except _Rejected as exc:
            self._html(exc.status, exc.reason)
        except _KnowledgeRefused as exc:
            self._html(exc.status, exc.message)
        except ConsolePublishError as exc:
            self.log_message("knowledge open bridge failed: %s", exc)
            self._html(502, "知识库入口暂时不可用，请稍后再试")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的协议
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == NATIVE_PREVIEW_ROUTE:
                _native_preview(self)
                return
            if path == NATIVE_CANCEL_ROUTE:
                _native_cancel(self)
                return
            if path == _TRIAL_ROUTE:
                self._post_trial()
                return
            if path == _TRIAL_RESULT_ROUTE:
                self._post_trial_result()
                return
            if path == _KNOWLEDGE_SYNC_ROUTE:
                self._post_knowledge_sync()
                return
            operations = {
                _PUBLISH_ROUTE: "publish",
                "/__aiops/retract": "retract",
                "/__aiops/restore": "restore",
                "/__aiops/versions": "versions",
            }
            if path not in operations:
                raise _Rejected(404, "not found")
            session, referer = self._check_browser_request()
            version_no = None
            if operations[path] == "restore":
                payload = self._read_bounded_json(MAX_REQUEST_BODY_BYTES, frozenset({"version_no"}))
                version_no = payload.get("version_no")
                if type(version_no) is not int or version_no < 1:
                    raise _Rejected(400, "请选择有效的历史版本")
            else:
                self._read_empty_body()
            result = publish_current_app(
                dify_base_url=self.settings.dify_base_url,
                gateway_base_url=self.settings.gateway_base_url,
                publish_key=self.settings.publish_key,
                console_session=session,
                console_origin=self.settings.console_origin,
                workspace_id=self.settings.workspace_id,
                referer=referer,
                allowed_roles=self.settings.allowed_roles,
                login_proxy_url=self.settings.login_proxy_url,
                gateway_internal_token=self.settings.gateway_internal_token,
                timeout=self.settings.timeout,
                operation=operations[path],
                version_no=version_no,
            )
        except _Rejected as exc:
            if path.startswith("/__aiops/operator-preview"):
                self._json(exc.status, {"code": "OPERATOR_PREVIEW_REJECTED", "message": exc.reason})
                return
            self._json(exc.status, {"published": False, "reason": exc.reason, "action": ""})
            return
        except ConsolePublishError as exc:
            if path.startswith("/__aiops/operator-preview"):
                self._json(502, {"code": "OPERATOR_PREVIEW_UNAVAILABLE", "message": "预览链路暂时不可用"})
                return
            # 桥自己那一步没做成（网络/解码）。**不透出内部细节**，只记在服务端。
            self.log_message("console publish bridge failed: %s", exc)
            self._json(502, {"published": False, "reason": "发布链路暂时不可用，请稍后再试", "action": ""})
            return
        self._json(result.status, result.public())

    def _post_knowledge_sync(self) -> None:
        """让网关把该租户的 RAGFlow 库收敛成 Dify dataset（ADR-0122 / #1056）。

        **它不改这个控制台的任何东西**：真正的写发生在网关 → Dify Console API，
        而这里只是把浏览器已经持有的 Dify 会话原样交给登录代理验一次，证明"哪个
        租户"—— 与 ``/__aiops/knowledge-open`` 同一套判据。

        **工作区由页面带上来，租户绝不能从请求里来。**

        这与 ``/__aiops/knowledge-open?workspace=…`` 同一条规矩，两条都不能省：

        * **workspace 必须来自页面**：桥自己的 ``workspace_id`` 是**迁移期的静态
          回退值**（见 ``BridgeSettings.workspace_id`` 的注释），不是"当前活跃
          workspace"。用它会在多 workspace 部署里同步错租户 —— 而运营看到的却是
          自己那一页，错误不报，只表现为"新库没出现"。
        * **租户仍然只能来自验过的会话**：网关按 workspace **反查**租户，并且
          请求里出现任何租户参数都是 400。页面能说的只有"我在哪一页"。

        返回 ``{"changed": N}``：调用方只在 N > 0 时刷新列表。**失败一律 200 +
        ``changed: 0``** —— 这是前端弹窗路径上的调用，一次网关抖动不该让运营看到
        红字，更不该挡住「添加知识库」本身（ADR-0122 约束 1）。

        ``changed`` 而不是 ``created``：运营**改名**或**删库**时 created 是 0，而页面
        正靠这个数决定要不要刷新（ADR-0125）。桥在这里是第三次犯同一个「只看新增」
        的错误（网关、前端各一次），所以字段名也一并改掉 —— 留着 `created` 这个名字
        会引诱下一个人接着只数新增。
        """
        if not self.settings.login_proxy_url or not self.settings.gateway_internal_token:
            raise _Rejected(404, "not found")
        session = _console_access_token_from_cookie(self._single_header("Cookie"))
        if session is None:
            raise _Rejected(401, "请先在 Dify 中登录")
        verdict = _identity_and_publish_verdict_for_identity(
            self.settings.login_proxy_url, console_session=session, timeout=self.settings.timeout
        )
        if verdict is None:
            raise _Rejected(401, "Dify 会话无效或已过期，请重新登录")
        headers, _allowed = verdict
        tenant_id = headers.get("X-AIOps-Initiator-Tenant", "").strip()
        if not tenant_id:
            raise _Rejected(403, "你的账号还没有绑定公司租户，请联系管理员")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        workspace_id = (query.get("workspace") or [""])[0].strip() if query else ""
        if not workspace_id:
            # 没有它就没法确定同步哪个 workspace。**回 ``created: 0`` 而不是 4xx**：
            # 前端拿不到 workspace 是它自己的问题，不该让弹窗报错。
            self.log_message("knowledge sync: no workspace in request; nothing synced")
            self._json(200, {"changed": 0, "created": 0})
            return
        try:
            changed = _knowledge_sync_payload(
                self.settings.gateway_base_url,
                self.settings.gateway_internal_token,
                workspace_id=workspace_id,
                timeout=self.settings.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - see the docstring's last paragraph
            self.log_message("knowledge sync bridge failed: %s", exc)
            self._json(200, {"changed": 0, "created": 0})
            return
        self.log_message("knowledge sync: tenant=%s changed=%s", tenant_id, changed)
        # `created` 仍保留为同值：老前端的 `changed ?? created` 回落在它上面，
        # 而不是回落到 0。两个字段同时在场，新旧前端都不会读错。
        self._json(200, {"changed": changed, "created": changed})

    def _post_trial(self) -> None:
        """试运行那条路（#781 / #782 的部署层）。

        与发布共用**同一套浏览器三门**（同源 Origin、Sec-Fetch-Site、Referer+CSRF）——
        运营点的是同一张页面里的按钮，不该有两套门。**app_id 取自 Referer**，不从请求体
        读：任何能构造请求体的人都能写一个别人的 app_id，而 Referer 至少还要求"你站在这
        个页面上"（它仍然不是授权，授权是那枚 Dify 会话）。

        未配 ``AIOPS_GATEWAY_DIFY_TRIAL_API_KEY`` ⇒ **404**，与网关那条路由同一机制：
        "这个部署没开试运行"不是"试运行不需要钥匙"。
        """
        if not self.settings.trial_key:
            raise _Rejected(404, "not found")
        session, referer = self._check_browser_request()
        app_id = app_id_from_referer(referer, console_origin=self.settings.console_origin)
        if app_id is None:  # pragma: no cover - _check_browser_request 已保证
            raise _Rejected(403, "无法确定当前是哪个智能体（请从编排页点这个按钮）")
        payload = self._read_bounded_json(MAX_TRIAL_BODY_BYTES, _TRIAL_PAYLOAD_KEYS)
        order_no = payload.get("order_no")
        if not isinstance(order_no, str) or not order_no.strip():
            raise _Rejected(400, "请填写要试运行的订单号")
        try:
            result = submit_trial(
                gateway_base_url=self.settings.gateway_base_url,
                trial_key=self.settings.trial_key,
                app_id=app_id,
                order_no=order_no.strip(),
                login_proxy_url=self.settings.login_proxy_url,
                console_session=session,
                timeout=self.settings.timeout,
            )
        except ConsolePublishError as exc:
            self.log_message("console trial bridge failed: %s", exc)
            self._json(502, {"submitted": False, "reason": "试运行链路暂时不可用，请稍后再试"})
            return
        self._json(
            result.status,
            {
                "submitted": result.published,
                "reason": result.reason,
                "action": result.action,
                # 只有提交成功才有它；按钮拿它去轮询结果面板。
                **({"trial_id": result.trial_id} if result.trial_id else {}),
            },
        )

    def _post_trial_result(self) -> None:
        """读一次试运行的结果（结果面板那一条）。

        **与提交同一套浏览器三门**，也同样是 POST：浏览器的同源 GET 不发 ``Origin``，
        把这条做成 GET 就得为它单开一套更弱的门 —— 而它读的是真订单上的诊断结果。

        ``app_id`` 仍取自 Referer：读的虽然是 ``trial_id``，但让一个未登记的 app
        借这条路去读别人的作业，就等于绕开了提交侧那道绑定门。
        """
        if not self.settings.trial_key:
            raise _Rejected(404, "not found")
        session, referer = self._check_browser_request()
        app_id = app_id_from_referer(referer, console_origin=self.settings.console_origin)
        if app_id is None:  # pragma: no cover - _check_browser_request 已保证
            raise _Rejected(403, "无法确定当前是哪个智能体（请从编排页点这个按钮）")
        payload = self._read_bounded_json(MAX_TRIAL_BODY_BYTES, _TRIAL_RESULT_PAYLOAD_KEYS)
        trial_id = payload.get("trial_id")
        if not isinstance(trial_id, str) or not trial_id.strip():
            raise _Rejected(400, "缺少 trial_id")
        try:
            report = read_trial(
                gateway_base_url=self.settings.gateway_base_url,
                trial_key=self.settings.trial_key,
                trial_id=trial_id.strip(),
                login_proxy_url=self.settings.login_proxy_url,
                console_session=session,
                timeout=self.settings.timeout,
            )
        except ConsolePublishError as exc:
            self.log_message("console trial result bridge failed: %s", exc)
            self._json(502, {"read": False, "reason": "试运行结果暂时读不到，请稍后再试"})
            return
        self._json(report.status, report.public())

    def _sse(self, event: dict) -> None:
        self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())
        self.wfile.flush()

    def _single_header(self, name: str) -> str | None:
        """安全敏感头必须恰好一次；重复时拒绝，免得桥与代理对"取哪一个"理解不同。"""
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise _Rejected(400, "duplicate request header")
        return values[0] if values else None

    def _get_knowledge_open(self) -> None:
        """知识库入口（#984 / PRD #981）。

        **租户只来自登录代理验过的会话。** 请求里带任何 `workspace` / `tenant_id` 一律
        不看 —— 这不是"忽略得更宽容"，是"根本没有读它的代码路径"。

        为什么这条门比发布那条弱却不影响安全：

        * 这条路由**不改任何东西**，只回答"往哪跳"；
        * 它把浏览器的 Dify 会话原样交给登录代理验证，代理回身份才继续；
        * 真正改变状态的那一步（换 RAGFlow 会话）在网关，而网关的租户来自这里
          解析出的 `company_tenant_id`，**不是**来自 URL。

        未配 `login_proxy_url` 或 `gateway_internal_token` ⇒ 404（与其余桥路由同一
        机制："这个部署没开"不是"不需要身份"）。
        """
        if not self.settings.login_proxy_url or not self.settings.gateway_internal_token:
            raise _Rejected(404, "not found")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        session = _console_access_token_from_cookie(self._single_header("Cookie"))
        if session is None:
            raise _Rejected(401, "请先在 Dify 中登录")
        verdict = _identity_and_publish_verdict_for_identity(
            self.settings.login_proxy_url, console_session=session, timeout=self.settings.timeout
        )
        if verdict is None:
            raise _Rejected(401, "Dify 会话无效或已过期，请重新登录")
        headers, _allowed = verdict
        tenant_id = headers.get("X-AIOps-Initiator-Tenant", "").strip()
        if not tenant_id:
            raise _Rejected(403, "你的账号还没有绑定公司租户，请联系管理员")
        # 桥手上是**租户**，网关那条路由要的是 **workspace**。先换成 workspace
        # （与发布链路同一个只读端点），再调入口 —— 这样网关侧"租户只来自 workspace
        # 反查"那条判据一个字都不用动。
        # **workspace 由前端带上来，因为只有页面自己知道"我此刻在哪个 workspace"。**
        #
        # 真机（2026-10-23）连着纠正了两次：
        #   1. 桥拿公司身份推 workspace —— 用 Dify 的是平台账号 `tenant_id=-1`，推不出来；
        #   2. 桥自己去问 Dify —— 顶层 GET 不带 CSRF，Dify 回 401。
        #
        # 所以由注入脚本在**页面内**问 Dify（它有会话也有 CSRF），把 `current_tenant.id`
        # 带过来。**这不是把判据交给前端**：桥仍然要求一个有效 Dify 会话，而网关那条路由
        # 仍然只认内部令牌 + workspace 反查（#982）。前端能做的只是"我这页看着是哪个
        # workspace"，而它换出来的会话仍然属于那个 workspace 对应的租户。
        workspace_id = (query.get("workspace") or [""])[0].strip() if query else ""
        if not workspace_id:
            raise _Rejected(400, "缺少当前工作区，请刷新 Dify 页面后重试")
        opened = _knowledge_open_payload(
            self.settings.gateway_base_url,
            self.settings.gateway_internal_token,
            workspace_id=workspace_id,
            timeout=self.settings.timeout,
        )
        self.log_message(
            "knowledge open: target=%s cookies=%d",
            opened.get("target"),
            len(opened.get("cookies") or []),
        )
        self.send_response(303)
        self.send_header("Location", str(opened["target"]))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        # 网关把 cookie 交给我们转给**浏览器**：桥是服务端，它收到 303 也不会存 cookie。
        # 这里原样转出，属性一个都不改 —— 改一个（尤其 Domain）就等于换了一个作用域。
        for cookie in opened.get("cookies") or []:
            self._relay_cookie(cookie)
        self.end_headers()

    def _current_dify_workspace(self, session: ConsoleSession) -> str:
        """问 Dify"当前这个会话停在哪个 workspace"。

        走 Dify 自己的 ``/console/api/workspaces/current/summary``，**用浏览器带来的
        会话**（cookie 原样转发）与它自己的 CSRF 双提交 —— 桥不持有任何 Dify 密码或密钥，
        它只是替这个浏览器问一句。

        **为什么不能从公司身份推**：用 Dify 的是平台账号（`tenant_id = -1`），租户是在
        Dify 里**切**出来的。真机上这条推错了两次：先拿 `-1` 去换 workspace（401），
        再退回"公司租户"（同样换不出）。切在哪，就管哪个 —— 这是 `current_tenant` 的
        唯一含义。
        """
        from urllib.parse import urlsplit

        from aiops_diagnostics.bounded_http import ErrorMapping, RequestSpec, join_url, request_json

        origin = urlsplit(self.settings.dify_base_url)
        if not origin.scheme or not origin.netloc:
            return ""
        cookie = f"{session.cookie_prefix}{CONSOLE_ACCESS_TOKEN_COOKIE}={session.access_token}"
        if session.csrf_token:
            cookie += f"; {session.cookie_prefix}{CONSOLE_CSRF_COOKIE}={session.csrf_token}"

        def failed(failure: HttpFailure) -> Exception:
            return ConsolePublishError(f"dify workspace: {failure.status or failure.detail}")

        payload = request_json(
            RequestSpec(
                url=join_url(self.settings.dify_base_url, "/console/api/workspaces/current/summary"),
                headers={
                    "Accept": "application/json",
                    "Cookie": cookie,
                    CONSOLE_CSRF_HEADER: session.csrf_token,
                },
                timeout=self.settings.timeout,
            ),
            mapping=ErrorMapping(
                auth_rejected=failed,
                http_error=failed,
                unavailable=failed,
                invalid_body=failed,
                invalid_envelope=failed,
            ),
        )
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("id") or "").strip()

    def _relay_cookie(self, cookie: Any) -> None:
        """把网关给的 cookie 属性逐字转成 ``Set-Cookie``。

        只接受形状正确的项；字段缺失就跳过这个 cookie 并记一行 —— 不"补一个默认
        Domain"，那会把一个限定在 ``.mall.qushiyun.com`` 的会话放大到更宽的范围。
        """
        if not isinstance(cookie, dict):
            return
        name, value, domain = cookie.get("name"), cookie.get("value"), cookie.get("domain")
        if not isinstance(name, str) or not isinstance(value, str) or not name or not value:
            self.log_message("knowledge open: skipping a malformed cookie")
            return
        parts = [f"{name}={value}"]
        if isinstance(domain, str) and domain:
            parts.append(f"Domain={domain}")
        parts.append("Path=/")
        if cookie.get("httponly"):
            parts.append("HttpOnly")
        if cookie.get("samesite"):
            parts.append(f"SameSite={cookie['samesite']}")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _html(self, status: int, message: str) -> None:
        """给**浏览器顶层导航**用的失败回应：一句人能读的话，不是 JSON。

        这一条会被新标签页直接渲染，所以它必须是 HTML。JSON 在标签里是一屏字面量。
        """
        body = (
            "<!doctype html><meta charset=utf-8><title>知识库入口</title>"
            '<div style="font:16px/1.6 system-ui;max-width:36rem;margin:12vh auto;padding:0 1rem">'
            f'<h1 style="font-size:1.1rem">无法打开知识库</h1><p>{_escape(message)}</p>'
            '<p style="color:#888;font-size:.85rem">关闭本页回到 Dify 重试；问题持续请联系管理员。</p>'
            "</div>"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _check_browser_request(self) -> tuple[ConsoleSession, str]:
        """浏览器侧的门，**一条没过就不问任何上游**。

        必须是控制台同源 POST：``Origin`` 精确匹配，``Sec-Fetch-Site`` 若出现则必须是
        ``same-origin``（老浏览器可能不发它，所以缺失时仍靠 Origin 与 CSRF 两条门）。
        不接受 ``same-site``：同站不等于同源，兄弟子域被攻陷不该能让这里发布。
        """
        origin = self._single_header("Origin")
        if origin is None or normalize_origin(origin) != self.settings.console_origin:
            raise _Rejected(403, "请从 Dify 控制台内点发布按钮")
        fetch_site = self._single_header("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site != "same-origin":
            raise _Rejected(403, "请从 Dify 控制台内点发布按钮")
        referer = self._single_header("Referer")
        if (
            referer is None
            or app_id_from_referer(referer, console_origin=self.settings.console_origin) is None
        ):
            raise _Rejected(403, "无法确定当前是哪个智能体（请从编排页点这个按钮）")
        content_type = self._single_header("Content-Type")
        if not content_type or content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise _Rejected(415, "请求必须是 application/json")
        session = console_session_from_headers(
            self._single_header("Cookie"), self._single_header(CONSOLE_CSRF_HEADER)
        )
        if session is None:
            raise _Rejected(401, "Dify 登录凭据不完整，请重新登录后再试")
        return session, referer

    def _read_empty_body(self) -> None:
        """只允许空 body 或 ``{}``，有界读取。畸形长度一律 4xx，绝不拿负数去 ``read``。

        RFC 9110 §8.6：Content-Length 是十进制非负整数。这里不接受重复长度，也不接受
        Transfer-Encoding（本端点没实现 chunked，不能让前置代理与桥分帧不同）。
        """
        if self._single_header("Transfer-Encoding") is not None:
            raise _Rejected(400, "transfer encoding is not supported")
        raw_length = self._single_header("Content-Length")
        if raw_length is None:
            length = 0
        elif not raw_length.isascii() or not raw_length.isdecimal():
            raise _Rejected(400, "invalid content length")
        elif _CONTENT_LENGTH_RE.fullmatch(raw_length) is None:
            # 全数字但很长：不交给 int（Python 自己也有位数上限），按超限处置。
            raise _Rejected(413, "payload too large")
        else:
            length = int(raw_length)
        if length > MAX_REQUEST_BODY_BYTES:
            raise _Rejected(413, "payload too large")
        if length == 0:
            return
        # ponytail: 单请求占一个线程；5 秒读超时限制慢 body，占用量大时交给前置代理限流。
        self.connection.settimeout(5.0)
        try:
            raw = self.rfile.read(length)
        except (OSError, TimeoutError):
            raise _Rejected(400, "incomplete request body") from None
        if len(raw) != length:
            raise _Rejected(400, "incomplete request body")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise _Rejected(400, "invalid request body") from None
        if payload != {}:
            raise _Rejected(400, "request body must be empty")

    def _read_bounded_json(self, limit: int, allowed_keys: frozenset[str]) -> dict[str, Any]:
        """有界读一个 JSON object，且**只允许** ``allowed_keys`` 里的键。

        与 :meth:`_read_empty_body` 同一套长度/编码/超时纪律（RFC 9110 §8.6：不接受重复
        长度、不接受 Transfer-Encoding —— 本端点没实现 chunked，不能让前置代理与桥分帧
        不同）。多出来的键一律 **400，不是安静忽略** —— 与网关那条请求体的
        ``extra="forbid"`` 同一条主张：多一个字段会让人以为它起了作用。
        """
        if self._single_header("Transfer-Encoding") is not None:
            raise _Rejected(400, "transfer encoding is not supported")
        raw_length = self._single_header("Content-Length")
        if raw_length is None:
            raise _Rejected(411, "content length required")
        if not raw_length.isascii() or not raw_length.isdecimal():
            raise _Rejected(400, "invalid content length")
        if _CONTENT_LENGTH_RE.fullmatch(raw_length) is None:
            raise _Rejected(413, "payload too large")
        length = int(raw_length)
        if length > limit:
            raise _Rejected(413, "payload too large")
        if length == 0:
            raise _Rejected(400, "request body must not be empty")
        # ponytail: 单请求占一个线程；5 秒读超时限制慢 body，占用量大时交给前置代理限流。
        self.connection.settimeout(5.0)
        try:
            raw = self.rfile.read(length)
        except (OSError, TimeoutError):
            raise _Rejected(400, "incomplete request body") from None
        if len(raw) != length:
            raise _Rejected(400, "incomplete request body")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise _Rejected(400, "invalid request body") from None
        if not isinstance(payload, dict):
            raise _Rejected(400, "request body must be an object")
        extra = set(payload) - allowed_keys
        if extra:
            raise _Rejected(400, "unexpected field in request body")
        return payload

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 失败时可能还没读 body；关闭而非复用半读连接（也避免 HTTP/1.1 管线请求歧义）。
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # 不打印请求头（Cookie/CSRF 都是凭据）。只打我们自己给的那句。
        _LOGGER.info("console-publish-bridge: " + fmt, *args)


NATIVE_PREVIEW_ROUTE = "/__aiops/operator-preview"
NATIVE_CANCEL_ROUTE = NATIVE_PREVIEW_ROUTE + "/cancel"
NATIVE_POLL_SECONDS = 1.0
NATIVE_TIMEOUT_SECONDS = 180
NATIVE_TASK_PREFIX = "aiops_"


def _native_session(handler):
    if not handler.settings.trial_key or not handler.settings.login_proxy_url:
        raise _Rejected(404, "管家预览尚未启用")
    session, referer = handler._check_browser_request()
    app_id = app_id_from_referer(referer, console_origin=handler.settings.console_origin)
    code, app = _request(
        handler.settings.dify_base_url + "/console/api/apps/" + app_id,
        headers=session.dify_headers(),
        timeout=handler.settings.timeout,
    )
    if code != 200 or not isinstance(app, dict) or app.get("id") != app_id:
        raise _Rejected(403, "当前会话无权预览此应用")
    return session, app_id


def _native_gateway(handler, session, path, *, body=None):
    return _request(
        handler.settings.gateway_base_url + "/v1/dify/trial" + path,
        method="POST" if body is not None else "GET",
        headers={
            "Authorization": "Bearer " + handler.settings.trial_key,
            "X-AIOps-Console-Session": session.access_token,
            "Content-Type": "application/json",
        },
        body=body,
        timeout=handler.settings.timeout,
    )


def _native_refusal(answer):
    error = answer.get("error", {}) if isinstance(answer, dict) else {}
    code = str(error.get("code") or "OPERATOR_PREVIEW_UNAVAILABLE")
    if code.startswith("OPERATOR_TRIAL_LOGIN") or code == "OPERATOR_TRIAL_IDENTITY_MISMATCH":
        message = "公司登录已失效或无法核验，请重新登录后重试"
    elif code in {"ORDER_NOT_FOUND", "CPMS_SCOPE_MISMATCH", "OPERATOR_TRIAL_SCOPE_MISMATCH"}:
        message = "订单不存在或不在当前账号可见范围内"
    elif code == "CHAT_ROUTE_RETRACTED":
        message = "管家节点路线已撤回，请联系管理员恢复"
    else:
        message = "管家预览未完成，请联系管理员核对节点与授权"
    return {"event": "error", "code": code, "message": message}


def _native_order(query):
    # 首片不做“上一单”推断；多个订单也不擅自选第一张。
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", query.strip()) and any(c.isdigit() for c in query):
        return query.strip()
    explicit = re.findall(
        r"(?:订单(?:号)?|order(?:\s+(?:id|no|number))?)\s*[:：#]?\s*([A-Za-z0-9_-]{1,64})(?![A-Za-z0-9_.:-])",
        query,
        re.IGNORECASE,
    )
    candidates = set(re.findall(r"(?<![A-Za-z0-9_.:-])[A-Za-z0-9_-]{6,64}(?![A-Za-z0-9_.:-])", query))
    candidates = {s for s in candidates if any(c.isdigit() for c in s) and (not s.isdigit() or len(s) >= 15)}
    candidates.update(s for s in explicit if any(c.isdigit() for c in s))
    if len(candidates) != 1:
        raise _Rejected(400, "请在本条消息中填写一个明确订单号；暂不支持“上一个订单”")
    return candidates.pop()


def _native_preview(handler):
    session, app_id = _native_session(handler)
    payload = handler._read_bounded_json(65536, frozenset({"app_id", "query", "pre_prompt", "tools"}))
    if payload.get("app_id") != app_id:
        raise _Rejected(403, "应用与当前页面不一致")
    query = payload.get("query")
    prompt, tools = payload.get("pre_prompt"), payload.get("tools")
    if (
        not isinstance(query, str)
        or not 0 < len(query) <= 4000
        or not isinstance(prompt, str)
        or not 0 < len(prompt) <= 8000
        or not isinstance(tools, list)
        or len(tools) > 32
    ):
        raise _Rejected(400, "预览问题、提示词或工具配置无效")
    order = _native_order(query)
    http_status, answer = _native_gateway(
        handler,
        session,
        "",
        body={
            "app_id": app_id,
            "order_no": order,
            "question": query,
            "operator_preview": {"pre_prompt": prompt, "tools": tools},
        },
    )
    trial_id = answer.get("trial_id") if isinstance(answer, dict) else None
    # 桥的 _request 将所有成功响应归一为 200；任务形状仍须单独验证。
    if http_status != 200 or not isinstance(trial_id, str) or not re.fullmatch(r"qa_[a-zA-Z0-9]+", trial_id):
        error = _native_refusal(answer)
        handler._json(http_status if 400 <= http_status < 500 else 502, error)
        return
    message_id = str(uuid.uuid4())
    common = {
        "id": message_id,
        "message_id": message_id,
        "conversation_id": "",
        "task_id": NATIVE_TASK_PREFIX + trial_id,
        "created_at": int(_time_module.time()),
    }
    finished = False
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        handler._sse({**common, "event": "message", "answer": ""})
        # Dify 的 agent-chat 把回答追加到当前 thought；这里只建立显示项，不伪造模型推理。
        handler._sse(
            {
                **common,
                "event": "agent_thought",
                "position": 1,
                "thought": "",
                "tool": "",
                "tool_input": "",
                "observation": "",
                "message_files": [],
            }
        )
        deadline = _time_module.monotonic() + NATIVE_TIMEOUT_SECONDS
        while _time_module.monotonic() < deadline:
            http_status, result = _native_gateway(handler, session, "/" + trial_id)
            if http_status != 200 or not isinstance(result, dict):
                handler._sse(_native_refusal(result))
                return
            if result.get("status") in TRIAL_TERMINAL_STATUSES:
                if result["status"] == "completed":
                    handler._sse(
                        {
                            **common,
                            "event": "message",
                            "answer": "Java 节点试用（模型由节点平台配置）\n\n"
                            + _trial_report_text(result, "completed"),
                        }
                    )
                    handler._sse({**common, "event": "message_end", "metadata": {}})
                else:
                    handler._sse(
                        {
                            "event": "error",
                            "code": result.get("error_code") or "OPERATOR_PREVIEW_FAILED",
                            "message": _trial_report_text(result, result["status"]),
                        }
                    )
                finished = True
                return
            handler.wfile.write(b": waiting\n\n")
            handler.wfile.flush()
            _time_module.sleep(NATIVE_POLL_SECONDS)
        handler._sse(
            {"event": "error", "code": "OPERATOR_PREVIEW_TIMEOUT", "message": "预览超时，任务已请求取消"}
        )
    except ConsolePublishError:
        with contextlib.suppress(OSError):
            handler._sse(
                {
                    "event": "error",
                    "code": "OPERATOR_PREVIEW_UNAVAILABLE",
                    "message": "预览结果读取失败，任务已请求取消",
                }
            )
    except OSError:
        pass
    finally:
        handler.close_connection = True
        if not finished:
            try:
                cancelled, _ = _native_gateway(
                    handler, session, "/" + trial_id + "/cancel", body={"app_id": app_id}
                )
                if cancelled != 200:
                    handler.log_message("operator preview cancellation refused: %s", cancelled)
            except ConsolePublishError:
                handler.log_message("operator preview cancellation could not be confirmed")


def _native_cancel(handler):
    session, app_id = _native_session(handler)
    payload = handler._read_bounded_json(4096, frozenset({"task_id"}))
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not re.fullmatch(r"aiops_qa_[a-zA-Z0-9]+", task_id):
        raise _Rejected(400, "预览任务无效")
    http_status, answer = _native_gateway(
        handler, session, "/" + task_id.removeprefix(NATIVE_TASK_PREFIX) + "/cancel", body={"app_id": app_id}
    )
    handler._json(http_status, {"result": "success"} if http_status == 200 else _native_refusal(answer))


def make_server(settings: BridgeSettings) -> ThreadingHTTPServer:
    handler = type("_BoundConsolePublishHandler", (ConsolePublishBridgeHandler,), {"settings": settings})
    return ThreadingHTTPServer((settings.listen_host, settings.listen_port), handler)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = make_server(BridgeSettings.from_env())
    _LOGGER.info("console publish bridge listening on %s:%s", *server.server_address)
    server.serve_forever()


if __name__ == "__main__":  # pragma: no cover - 进程入口
    main()
