"""固定管理动作的同源桥；只转发服务端验证过的浏览器会话。"""

import re


def handle(handler, bridge):
    # 支持 python -m 入口，拒绝异常必须属于运行中的同一个模块。
    CONSOLE_CSRF_HEADER = bridge.CONSOLE_CSRF_HEADER
    _origin_and_path = bridge._origin_and_path
    _Rejected = bridge._Rejected
    _request = bridge._request
    console_session_from_headers = bridge.console_session_from_headers
    normalize_origin = bridge.normalize_origin

    origin = normalize_origin(handler._single_header("Origin"))
    referer = handler._single_header("Referer") or ""
    if (
        origin != handler.settings.console_origin
        or (_origin_and_path(referer) or (None, None))[0] != handler.settings.console_origin
    ):
        raise _Rejected(403, "请从Dify控制台操作")
    if handler._single_header("Sec-Fetch-Site") not in (None, "same-origin"):
        raise _Rejected(403, "请从Dify控制台操作")
    session = console_session_from_headers(
        handler._single_header("Cookie"), handler._single_header(CONSOLE_CSRF_HEADER)
    )
    if session is None:
        raise _Rejected(401, "请重新登录Dify")
    content_type = handler._single_header("Content-Type") or ""
    if content_type.split(";", 1)[0].lower() != "application/json":
        raise _Rejected(415, "请求必须是application/json")
    http_status, summary = _request(
        handler.settings.dify_base_url + "/console/api/workspaces/current/summary",
        headers=session.dify_headers(),
        timeout=15,
    )
    if (
        http_status != 200
        or not isinstance(summary, dict)
        or summary.get("role") not in {"owner", "admin", "editor"}
    ):
        raise _Rejected(403, "当前账号没有智能体管理权限")
    payload = handler._read_bounded_json(
        65536,
        frozenset(
            {
                "action",
                "app_id",
                "source_app_id",
                "source_version",
                "tenant_id",
                "name",
                "request_key",
                "id",
                "revision",
                "draft",
                "version_no",
                "workspace_id",
                # 双端开通：范围与每端来源。桥是**固定动作契约**，所以这两个字段不是
                # "放行任意结构"，而是在下面逐字段校验形状 —— 桥放行、后端再校验，
                # 等于让一个手写请求绕过第一道门。
                "scope",
                "sources",
                "question",
                "order_no",
                "trial_id",
                "knowledge_base_id",
                "allowed",
            }
        ),
    )
    action = payload.pop("action", "")
    paths = {
        "catalogue": ("GET", "/catalogue"),
        "status": ("POST", "/status"),
        "readiness": ("POST", "/readiness"),
        "create": ("POST", "/operations"),
        "publish": ("POST", "/publish"),
        "versions": ("POST", "/versions"),
        "version": ("POST", "/version"),
        "workspace": ("POST", "/workspace"),
        "trial": ("POST", "/trial"),
        "trial-result": ("POST", "/trial/result"),
        "trial-cancel": ("POST", "/trial/cancel"),
        "knowledge": ("POST", "/knowledge"),
        "knowledge-access": ("POST", "/knowledge/access"),
    }
    if action in {"operation", "retry"}:
        job = payload.pop("id", "")
        if not re.fullmatch(r"[a-f0-9-]{36}", job):
            raise _Rejected(400, "无效操作")
        method, path = (
            ("GET", "/operations/" + job)
            if action == "operation"
            else ("POST", "/operations/" + job + "/retry")
        )
    elif action in paths:
        method, path = paths[action]
    else:
        raise _Rejected(400, "无效管理动作")
    if (method == "GET" or action == "retry") and payload:
        raise _Rejected(400, "无效参数")
    _validate_scope_and_sources(action, payload, _Rejected)
    if action.startswith("trial"):
        expected = {"app_id", "question", "order_no"} if action == "trial" else {"app_id", "trial_id"}
        if set(payload) - expected or not handler.settings.trial_key:
            raise _Rejected(400, "无效预览参数")
        if action == "trial":
            if not isinstance(payload.get("question"), str) or not 0 < len(payload["question"]) <= 4000:
                raise _Rejected(400, "请填写预览问题")
            if not isinstance(payload.get("order_no", ""), str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{0,64}", payload.get("order_no", "")
            ):
                raise _Rejected(400, "无效订单号")
        elif not isinstance(payload.get("trial_id"), str) or not re.fullmatch(
            r"tr_[a-f0-9]{32}", payload["trial_id"]
        ):
            raise _Rejected(400, "无效预览")
    elif action == "knowledge-access":
        if (
            set(payload) != {"app_id", "knowledge_base_id", "allowed"}
            or type(payload["allowed"]) is not bool
            or not isinstance(payload["knowledge_base_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", payload["knowledge_base_id"])
        ):
            raise _Rejected(400, "无效知识授权参数")
    elif set(payload) & {"question", "order_no", "trial_id", "knowledge_base_id", "allowed"}:
        raise _Rejected(400, "无效参数")
    # 应用状态/发布必须能由当前Dify工作区读到；平台跨租户开通另由公司目录和来源登记裁定。
    if action in {
        "status",
        "readiness",
        "publish",
        "versions",
        "version",
        "trial",
        "trial-result",
        "trial-cancel",
        "knowledge",
        "knowledge-access",
    }:
        app_id = payload.get("app_id", "")
        if not re.fullmatch(r"[a-f0-9-]{36}", app_id):
            raise _Rejected(400, "无效应用")
        code, app = _request(
            handler.settings.dify_base_url + "/console/api/apps/" + app_id,
            headers=session.dify_headers(),
            timeout=15,
        )
        if code != 200 or not isinstance(app, dict) or app.get("id") != app_id:
            raise _Rejected(403, "无权管理此应用")
    code, result = _request(
        handler.settings.gateway_base_url + "/v1/dify/management" + path,
        method=method,
        headers={
            "Authorization": "Bearer "
            + (handler.settings.trial_key if action.startswith("trial") else handler.settings.publish_key),
            "X-AIOps-Console-Session": session.access_token,
            "Content-Type": "application/json",
        },
        body=payload if method == "POST" else None,
        timeout=30,
    )
    if (
        action == "status"
        and isinstance(result, dict)
        and result.get("error", {}).get("code") == "SETUP_DISABLED"
    ):
        handler._json(200, {"managed": False})
        return
    if action == "workspace" and code == 200 and isinstance(result, dict):
        workspace_id = result.get("workspace_id", "")
        http_status, profile = _request(
            handler.settings.dify_base_url + "/console/api/account/profile",
            headers=session.dify_headers(),
            timeout=15,
        )
        if (
            http_status != 200
            or not isinstance(profile, dict)
            or not profile.get("id")
            or not profile.get("email")
        ):
            raise _Rejected(403, "无法核对当前Dify账号")
        http_status, response = _request(
            handler.settings.dify_base_url + "/console/api/workspaces",
            headers=session.dify_headers(),
            timeout=15,
        )
        workspaces = response.get("workspaces", []) if isinstance(response, dict) else []
        if http_status != 200:
            raise _Rejected(502, "无法读取工作区")
        if not any(w.get("id") == workspace_id for w in workspaces):
            if not result.get("platform"):
                raise _Rejected(403, "当前账号没有此工作区访问权")
            http_status, response = _request(
                handler.settings.gateway_base_url + "/v1/dify/management/workspace/member",
                method="POST",
                headers={
                    "Authorization": "Bearer " + handler.settings.publish_key,
                    "X-AIOps-Console-Session": session.access_token,
                    "Content-Type": "application/json",
                },
                body={"workspace_id": workspace_id, "account_id": profile["id"], "email": profile["email"]},
                timeout=30,
            )
            if http_status != 200:
                raise _Rejected(502, "暂时无法进入客户工作区，请重试")
        http_status, response = _request(
            handler.settings.dify_base_url + "/console/api/workspaces/switch",
            method="POST",
            headers={**session.dify_headers(), "Content-Type": "application/json"},
            body={"tenant_id": workspace_id},
            timeout=15,
        )
        if http_status != 200 or not isinstance(response, dict) or response.get("result") != "success":
            raise _Rejected(502, "工作区切换失败，请重试")
    handler._json(code, result if isinstance(result, dict) else {"error": {"code": "SETUP_UNAVAILABLE"}})


def authorize_workspace(handler, bridge, session, workspace_id):
    code, result = bridge._request(
        handler.settings.gateway_base_url + "/v1/dify/management/workspace",
        method="POST",
        headers={
            "Authorization": "Bearer " + handler.settings.publish_key,
            "X-AIOps-Console-Session": session.access_token,
            "Content-Type": "application/json",
        },
        body={"workspace_id": workspace_id},
        timeout=15,
    )
    if code != 200 or not isinstance(result, dict) or result.get("workspace_id") != workspace_id:
        raise bridge._Rejected(403, "当前账号无权访问此客户知识库，请重新登录或联系平台管理员")


_SCOPES = {"both", "consumer", "operator"}
_ENTRIES = {"consumer", "operator"}
_APP_ID = re.compile(r"[a-f0-9-]{36}")


def _validate_scope_and_sources(action, payload, rejected):
    """`scope`/`sources` 只属于 `create`，且形状固定。

    桥这一层的职责是"浏览器只能提交固定动作的固定形状"。把新字段加进白名单却不校验形状，
    等于把"未知字段拒绝"降级成"未知字段也转发"，而下游的 Pydantic 校验发生在**网关**上 ——
    中间那一段（桥到网关）就变成谁都能塞东西的通道。
    """
    if "scope" not in payload and "sources" not in payload:
        return
    if action != "create":
        raise rejected(400, "无效参数")
    scope = payload.get("scope", "both")
    if not isinstance(scope, str) or scope not in _SCOPES:
        raise rejected(400, "无效开通范围")
    sources = payload.get("sources")
    if sources is None:
        # 旧的单来源形状：由后端按管家处理，桥不额外解释。
        return
    if not isinstance(sources, dict) or not sources or set(sources) - _ENTRIES:
        raise rejected(400, "无效来源")
    wanted = _ENTRIES if scope == "both" else {scope}
    if set(sources) != wanted:
        raise rejected(400, "来源与开通范围不一致")
    for spec in sources.values():
        if not isinstance(spec, dict) or set(spec) != {"app_id", "version_no"}:
            raise rejected(400, "无效来源")
        if not isinstance(spec["app_id"], str) or not _APP_ID.fullmatch(spec["app_id"]):
            raise rejected(400, "无效来源应用")
        if type(spec["version_no"]) is not int or spec["version_no"] < 1:
            raise rejected(400, "无效来源版本")
