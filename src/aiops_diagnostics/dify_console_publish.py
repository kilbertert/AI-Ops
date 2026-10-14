"""Dify 控制台里那颗「发布到 AI-Ops」按钮的**后端桥**。

## 这一层在解决什么

运营在 Dify 控制台里编辑智能体，改完必须让**线上**生效。那条路我们早就有了
（``POST /v1/operator/publish``，ADR-0018 的第二种门），但它**没有界面** ——
运营要改完再找工程敲一条命令。这一层就是那个界面背后的东西。

## 它不持有任何"发布权"，它只是把运营**已经有的**能力做完

发布按钮**不引入新的权限**。理由要写清楚，否则下一个读代码的人会以为这里有个洞：

> **能登进 Dify 控制台改这个 app 的提示词的人，本来就已经能改这个智能体的行为。**

提示词是智能体的全部定义；编辑它就是改行为。所以"点一下让这个改动上线"**不给**
那个人任何他本来没有的能力 —— 它只是把他已经做出的改动走完最后一步。真正要防的不是
"谁能点"，而是"点了能不能跨出租户/跨出这个 app" —— 而那一层由
``/v1/operator/publish`` 的门自己堵（它只接受"这个 (租户,入口) 绑定的那一个 app"）。

## 因此唯一的凭据是**运营自己的 Dify 会话**，不是任何共享密钥

这一条是刻意的，而且是这一层与"给浏览器发一把密钥"的关键区别：

* **密钥不进浏览器。** 那把 ``AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY`` 只存在于服务端
  配置文件里，浏览器永远看不到它 —— 它在**这一层**被使用。
* **身份由 Dify 自己回答。** 桥拿请求里的 ``access_token`` cookie 去问 Dify
  ``GET /console/api/apps/<app_id>``：Dify 认了，说明这个会话有效且对这个 app 有可见性；
  Dify 拒了（401/403），桥就拒。
* **app_id 由 Dify 自己回答，不由请求体给。** 桥问的是**当前页面那个 app** ——
  它从注入的按钮所在的 URL 里取（``/app/<id>/configuration``）。所以请求体里
  **没有** app_id 可伪造，桥也不会去猜"运营想发布哪个"。

## 一处刻意的坏味道：它会失败得"很无聊"

桥的每一个失败分支都返回**同一个形状**（``{"published": false, "reason": ...}``），
而不是把 Dify 的错误码原样透出去。原因：这一层是给运营看的一句话（"发布成功/失败"），
而"你的会话过期了"与"Dify 拒绝了"对看的人是同一件事 —— 都是"再登一次/再点一次"。
把内部码透到浏览器里只会让一个界面按钮变成一次信息泄漏面。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

from aiops_diagnostics.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    json_body,
    parse_raw_envelope,
    request_json,
)

_LOGGER = logging.getLogger("aiops.dify_console_publish")

#: Dify 控制台的会话 cookie 名。它是**运营自己的**登录态，不是我们的任何凭据。
CONSOLE_ACCESS_TOKEN_COOKIE = "access_token"

#: 桥只打这两跳。都是**相对路径**，所以"打到谁"由部署决定（nginx 把它指到 Dify /
#: 指到网关），代码里没有地址。
DIFY_APP_PATH = "/console/api/apps/{app_id}"
PUBLISH_PATH = "/v1/operator/publish"


class ConsolePublishError(RuntimeError):
    """桥自己的一步失败了（不是"运营无权"，而是"这一步没做成"）。"""


@dataclass(frozen=True, slots=True)
class ConsolePublishResult:
    """一次按钮点击的结果 —— 面向前端，刻意只有三个字段。"""

    published: bool
    reason: str
    #: 发布实现自己的回答（``unchanged`` / ``synced`` / …）。失败时为空串。
    action: str = ""

    def public(self) -> dict[str, Any]:
        return {"published": self.published, "reason": self.reason, "action": self.action}


def app_id_from_referer(referer: str | None) -> str | None:
    """从**页面自己的 URL** 里取出当前 app 的 id，取不到就是 ``None``。

    这是 app_id 的唯一来源，而它来自浏览器**已经打开的页面**，不是请求体 ——
    所以调用方伪造不了"我要发布哪个 app"。取不到（Referer 缺失或形状不对）时
    返回 ``None``，调用方据此拒绝；**猜一个默认值**在这里是最坏的做法。

    形状取自 Dify 自己的路由：``/app/<uuid>/configuration``。只认这一段路径，
    不认查询串、不认 fragment —— 它们在 Referer 里可能出现，且与是哪个 app 无关。
    """
    if not referer:
        return None
    try:
        path = urllib.parse.urlsplit(referer).path
    except ValueError:
        return None
    parts = [segment for segment in path.split("/") if segment]
    if len(parts) < 2 or parts[0] != "app":
        return None
    candidate = parts[1]
    # app id 是 uuid 形状；不做正则，只做"像不像一个 id"的最小校验，避免把
    # `/app/settings` 这类路由当成一个 app。
    if not candidate or len(candidate) > 64 or candidate in {"settings", "new"}:
        return None
    return candidate


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any]:
    """一跳有界 HTTP，走仓库**唯一**的出站骨架（``bounded_http``）。

    返回 ``(status, decoded_or_text)``。这里**不复用骨架的错误映射**是有意的：
    这一层要的是"拿状态码自己判"，而不是把 401/403/5xx 折成领域异常 ——
    桥的每一个失败分支最后都收敛成同一句话，见模块 docstring。

    桥自己那一步失败（连不上/超时/解码不了）抛 ``ConsolePublishError``。
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
    )
    try:
        payload = request_json(spec, mapping=mapping, envelope=parse_raw_envelope)
    except ConsolePublishError:
        # 骨架把 HTTP 状态折成了异常 —— 但那不是"桥失败了"，是**上游明确回答**。
        # 把它还原成状态码，让调用方按状态判（这正是这一层要的粒度）。
        if not failures:
            raise
        failure = failures[0]
        if failure.status is None:
            # 传输层故障（连不上/超时/解码失败）**没有状态码** —— 它是"这一步没做成"，
            # 不是"上游回答了一个码"。原样抛，让上层的 502 分支接住。
            raise
        return failure.status, ""
    return 200, payload


def publish_current_app(
    *,
    dify_base_url: str,
    gateway_base_url: str,
    publish_key: str,
    console_session: str,
    referer: str | None,
    timeout: float = 15.0,
) -> ConsolePublishResult:
    """运营点了按钮之后，这一层做的全部事情。

    三步，顺序不能换（第 2 步是第 1 步授权的，第 3 步是第 2 步定址的）：

    1. 从 Referer 取当前 app 的 id；取不到 ⇒ 拒绝（**不猜**）。
    2. 拿运营的会话问 Dify ``GET /console/api/apps/<id>``：不是 2xx ⇒ 拒绝。
       这一步同时干了两件事 —— 验会话**有效**，且验该会话**看得见这个 app**。
    3. 用**服务端**的发布密钥调 ``POST /v1/operator/publish``，body 只有 ``app_id``。

    第 3 步用的是 ``X-Business-Entry: operator`` —— 与运营在管家端是同一个入口。
    ``/v1/operator/publish`` 自己还会再判一次"这个 app 是不是该 (租户,operator) 绑的"，
    所以即使第 2 步被绕过，越界发布也过不去。
    """
    app_id = app_id_from_referer(referer)
    if app_id is None:
        return ConsolePublishResult(False, "无法确定当前是哪个智能体（请从编排页点这个按钮）")

    dify_url = dify_base_url.rstrip("/") + DIFY_APP_PATH.format(app_id=urllib.parse.quote(app_id, safe=""))
    http_status, _ = _request(
        dify_url,
        headers={"Cookie": f"{CONSOLE_ACCESS_TOKEN_COOKIE}={console_session}"},
        timeout=timeout,
    )
    if not 200 <= http_status < 300:
        return ConsolePublishResult(False, "Dify 会话已失效或你看不到这个智能体，请重新登录后再试")

    gateway_url = gateway_base_url.rstrip("/") + PUBLISH_PATH
    http_status, payload = _request(
        gateway_url,
        method="POST",
        headers={
            "Authorization": f"Bearer {publish_key}",
            "X-Business-Entry": "operator",
            "Content-Type": "application/json",
        },
        body={"app_id": app_id},
        timeout=timeout,
    )
    if not 200 <= http_status < 300:
        # 发布实现自己的错误码不透出去：对点按钮的人，"你的会话过期了"和"Dify 拒绝了"
        # 是同一件事（再登一次/再点一次）。透出内部码只会多一个信息面。
        return ConsolePublishResult(False, "发布未通过我们的发布门，请联系工程查看原因")
    action = str((payload or {}).get("action") or "")
    if action == "unchanged":
        return ConsolePublishResult(True, "线上已是最新，无需改动", action)
    version = (payload or {}).get("version_no")
    return ConsolePublishResult(True, f"已发布到线上（版本 {version}）", action)


# ── HTTP 面：注入的按钮打的那一跳 ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    """桥的全部配置。**没有一样进浏览器** —— 这正是这一层的要点。"""

    listen_host: str
    listen_port: int
    #: Dify 自己（我们注入的页面所在的那个栈）。桥拿运营的会话去问它"你是谁"。
    dify_base_url: str
    #: 我们的网关。发布实现住在那里。
    gateway_base_url: str
    #: 服务端持有的发布密钥。与 ``AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY`` 同一把 ——
    #: 桥不引入新凭据，它只是替运营**拿着**那把已有的服务端密钥。
    publish_key: str
    timeout: float = 15.0

    @classmethod
    def from_env(cls) -> BridgeSettings:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        return cls(
            listen_host=os.environ.get("AIOPS_CONSOLE_BRIDGE_HOST", "127.0.0.1"),
            listen_port=int(os.environ.get("AIOPS_CONSOLE_BRIDGE_PORT", "10118")),
            dify_base_url=required("AIOPS_CONSOLE_BRIDGE_DIFY_URL").rstrip("/"),
            gateway_base_url=required("AIOPS_CONSOLE_BRIDGE_GATEWAY_URL").rstrip("/"),
            publish_key=required("AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY"),
        )


def _console_session_from_cookie(header: str | None) -> str | None:
    """从 ``Cookie:`` 头里取出 Dify 的 ``access_token``。取不到就是 ``None``。

    只做最小解析：按 ``;`` 切、按第一个 ``=`` 切。不用 ``http.cookies`` 是有意的 ——
    那个模块会为畸形输入抛异常，而这里畸形输入的正确处置是"当作没有"（→ 拒绝），
    不是"抛出去"。
    """
    if not header:
        return None
    for chunk in header.split(";"):
        name, _, value = chunk.strip().partition("=")
        if name == CONSOLE_ACCESS_TOKEN_COOKIE and value:
            return value.strip()
    return None


class ConsolePublishBridgeHandler(BaseHTTPRequestHandler):
    """一个端点：``POST /__aiops/publish``。

    与仓库里既有的 ``TDengineReadonlyHandler`` 同款（stdlib、有界、不做框架）。
    这里**只有这一个**路径 —— 桥不做代理、不做转发、不留后门；它只回答"发布成不成"。
    """

    settings: ClassVar[BridgeSettings]

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的协议
        if urllib.parse.urlsplit(self.path).path != "/__aiops/publish":
            self._json(404, {"published": False, "reason": "not found"})
            return
        # 长度上限：这个 body 允许为空，但**不允许很大** —— 桥不读它的内容，
        # 读掉它只是为了不把连接留在半读状态。
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            self._json(413, {"published": False, "reason": "payload too large"})
            return
        if length:
            self.rfile.read(length)
        session = _console_session_from_cookie(self.headers.get("Cookie"))
        if session is None:
            self._json(401, {"published": False, "reason": "未登录 Dify，请重新登录后再试"})
            return
        try:
            result = publish_current_app(
                dify_base_url=self.settings.dify_base_url,
                gateway_base_url=self.settings.gateway_base_url,
                publish_key=self.settings.publish_key,
                console_session=session,
                referer=self.headers.get("Referer"),
                timeout=self.settings.timeout,
            )
        except ConsolePublishError as exc:
            # 桥自己那一步没做成（网络/解码）。**不透出内部细节**，只记在服务端。
            self.log_message("console publish bridge failed: %s", exc)
            self._json(502, {"published": False, "reason": "发布链路暂时不可用，请稍后再试"})
            return
        self._json(200 if result.published else 403, result.public())

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 只有同源的页面会调它；浏览器不带凭据跨源也调不动。
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # 默认实现往 stderr 打**整行请求**，其中包含 Cookie。桥的会话是运营的登录态，
        # 不进日志 —— 覆盖掉，只打我们自己给的那句。
        _LOGGER.info("console-publish-bridge: " + fmt, *args)


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
