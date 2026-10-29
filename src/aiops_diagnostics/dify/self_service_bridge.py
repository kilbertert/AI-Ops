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
    # 应用状态/发布必须能由当前Dify工作区读到；平台跨租户开通另由公司目录和来源登记裁定。
    if action in {"status", "readiness", "publish", "versions", "version"}:
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
            "Authorization": "Bearer " + handler.settings.publish_key,
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
    handler._json(code, result if isinstance(result, dict) else {"error": {"code": "SETUP_UNAVAILABLE"}})
