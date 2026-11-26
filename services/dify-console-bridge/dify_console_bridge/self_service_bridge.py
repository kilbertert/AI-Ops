"""固定管理动作的同源桥；只转发服务端验证过的浏览器会话。"""

from __future__ import annotations

import re

from aiops_diagnostics.identity.order_identity import is_order_identifier

from . import bridge

_SCOPES = frozenset({"both", "consumer", "operator"})
_ENTRIES = frozenset({"consumer", "operator"})
_CAPABILITIES = frozenset({"consumer", "order", "offline"})
_ALLOWED_ROLES = frozenset({"owner", "admin", "editor"})

_APP_ID = re.compile(r"[a-f0-9-]{36}")
_TASK_ID = re.compile(r"task_[0-9a-f]{32}")
_PLAN_ID = re.compile(r"plan_[0-9a-f]{32}")
_TRIAL_ID = re.compile(r"tr_[a-f0-9]{32}")
_ENV_ID = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?")
_ENV_PLAN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,30}[a-z0-9]|[a-z0-9]")
_TENANT_REF = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RUN_ID = re.compile(r"run_[A-Za-z0-9_-]{43}")
_KB_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

_DRAFT_FIELDS = frozenset(
    {
        "id",
        "name",
        "enabled",
        "gateway_url",
        "cpms_base_url",
        "company_gateway_url",
        "deployment_target",
        "credential_ref",
        "timezone",
        "tenants",
    }
)

_ENV_TASK_ONLY_KEYS = frozenset(
    {
        "candidate_task",
        "installation",
        "installation_task",
        "registration_plan",
        "registration_task",
        "validation_plan",
        "validation_task",
        "publication_plan",
        "confirm_readonly",
        "confirm_customer_knowledge",
        "acceptance_ref",
        "restore_publication",
        "capabilities",
        "case_ref",
        "candidate_plan",
        "onboard_plan",
        "verify_task",
        "plan_id",
        "site_task",
        "workspace_task",
        "knowledge_task",
        "connection_task",
        "cancel",
    }
)

_ALLOWED_PAYLOAD_KEYS = (
    frozenset(
        {
            "action",
            "environment",
            "enabled",
            "app_id",
            "source_app_id",
            "source_version",
            "tenant_id",
            "name",
            "request_key",
            "id",
            "revision",
            "registry_revision",
            "initial_installation",
            "consumer_sources",
            "draft",
            "version_no",
            "workspace_id",
            "scope",
            "sources",
            "question",
            "order_no",
            "trial_id",
            "knowledge_base_id",
            "allowed",
            "page",
            "limit",
            "run_id",
            "wizard_action",
            "wizard_payload",
        }
    )
    | _ENV_TASK_ONLY_KEYS
)

_WIZARD_ACTIONS = frozenset(
    {
        "host_status",
        "host_enroll_password",
        "host_create_bootstrap_script",
        "host_revoke",
        "discover",
        "db_remediate",
        "artifact_pool_status",
        "infra_provision",
        "infra_status",
        "kb_upload",
        "kb_install_starter_faq",
        "self_validate",
        "publish_environment",
        "post_publish_smoke",
    }
)

_ACTION_ROUTES: dict[str, tuple[str, str]] = {
    "catalogue": ("GET", "/catalogue"),
    "environments": ("GET", "/environments"),
    "environment-update": ("POST", "/environments"),
    "environment-drafts": ("GET", "/environment-drafts"),
    "environment-overview": ("GET", "/environment-overview"),
    "environment-plan": ("POST", "/environment-plans"),
    "environment-task": ("POST", "/environment-tasks"),
    "environment-wizard": ("POST", "/environment-wizard"),
    "environment-draft-save": ("POST", "/environment-drafts"),
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
    "run-logs": ("POST", "/run-logs"),
    "run-log": ("POST", "/run-logs/detail"),
}

_APP_SCOPED_ACTIONS = frozenset(
    {
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
        "run-logs",
        "run-log",
    }
)

_PARENT_TASK_KEYS = ("site_task", "workspace_task", "knowledge_task", "connection_task")


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def _pos_int(value: object) -> bool:
    return type(value) is int and value >= 1


def _verify_browser_session(handler):
    """核验浏览器同源头、JSON 内容类型、Dify 控制台会话及管理角色。"""
    rejected = bridge._Rejected
    origin = bridge.normalize_origin(handler._single_header("Origin"))
    referer = handler._single_header("Referer") or ""
    if (
        origin != handler.settings.console_origin
        or (bridge._origin_and_path(referer) or (None, None))[0] != handler.settings.console_origin
    ):
        raise rejected(403, "请从Dify控制台操作")
    if handler._single_header("Sec-Fetch-Site") not in (None, "same-origin"):
        raise rejected(403, "请从Dify控制台操作")
    session = bridge.console_session_from_headers(
        handler._single_header("Cookie"), handler._single_header(bridge.CONSOLE_CSRF_HEADER)
    )
    if session is None:
        raise rejected(401, "请重新登录Dify")
    content_type = handler._single_header("Content-Type") or ""
    if content_type.split(";", 1)[0].lower() != "application/json":
        raise rejected(415, "请求必须是application/json")
    http_status, summary = bridge._request(
        handler.settings.dify_base_url + "/console/api/workspaces/current/summary",
        headers=session.dify_headers(),
        timeout=15,
    )
    if http_status != 200 or not isinstance(summary, dict) or summary.get("role") not in _ALLOWED_ROLES:
        raise rejected(403, "当前账号没有智能体管理权限")
    return session


def _resolve_action_route(action: str, payload: dict, rejected) -> tuple[str, str]:
    if action in {"operation", "retry"}:
        job = payload.pop("id", "")
        if not _matches(_APP_ID, job):
            raise rejected(400, "无效操作")
        method, path = (
            ("GET", "/operations/" + job)
            if action == "operation"
            else ("POST", "/operations/" + job + "/retry")
        )
    elif action in _ACTION_ROUTES:
        method, path = _ACTION_ROUTES[action]
    else:
        raise rejected(400, "无效管理动作")
    if (method == "GET" or action == "retry") and payload:
        raise rejected(400, "无效参数")
    return method, path


def _is_valid_environment_task(payload: dict) -> bool:
    keys = set(payload)
    if keys == {"restore_publication"}:
        return _matches(_TASK_ID, payload["restore_publication"])
    if keys == {
        "validation_task",
        "publication_plan",
        "confirm_readonly",
        "confirm_customer_knowledge",
        "acceptance_ref",
    }:
        return (
            _matches(_TASK_ID, payload["validation_task"])
            and _matches(_PLAN_ID, payload["publication_plan"])
            and payload["confirm_readonly"] is True
            and payload["confirm_customer_knowledge"] is True
            and payload["acceptance_ref"] == "readonly-diagnosis"
        )
    if keys == {"registration_task", "validation_plan", "capabilities", "case_ref"}:
        caps = payload["capabilities"]
        return (
            _matches(_TASK_ID, payload["registration_task"])
            and _matches(_PLAN_ID, payload["validation_plan"])
            and _matches(_TENANT_REF, payload["case_ref"])
            and isinstance(caps, list)
            and 1 <= len(caps) <= 3
            and all(isinstance(c, str) and c in _CAPABILITIES for c in caps)
            and len(set(caps)) == len(caps)
        )
    if keys == {"installation_task", "registration_plan"}:
        return _matches(_TASK_ID, payload["installation_task"]) and _matches(
            _PLAN_ID, payload["registration_plan"]
        )
    if keys == {"candidate_task", "installation"}:
        return payload["installation"] is True and _matches(_TASK_ID, payload["candidate_task"])
    if "onboard_plan" in keys:
        required = {"onboard_plan", "tenant_id", "source_app_id", "source_version"}
        allowed = required | {"initial_installation", "case_ref", "capabilities"}
        if not required <= keys or not keys <= allowed:
            return False
        if not (
            _matches(_PLAN_ID, payload["onboard_plan"])
            and _matches(_TENANT_REF, payload["tenant_id"])
            and _matches(_APP_ID, payload["source_app_id"])
            and _pos_int(payload["source_version"])
            and payload.get("initial_installation", True) is True
        ):
            return False
        if "case_ref" in keys or "capabilities" in keys:
            if not ("case_ref" in keys and "capabilities" in keys):
                return False
            caps = payload["capabilities"]
            return (
                _matches(_TENANT_REF, payload["case_ref"])
                and isinstance(caps, list)
                and 1 <= len(caps) <= 3
                and all(isinstance(c, str) and c in _CAPABILITIES for c in caps)
                and len(set(caps)) == len(caps)
            )
        return True
    if keys in (
        {"candidate_plan", "tenant_id", "source_app_id", "source_version"},
        {"candidate_plan", "tenant_id", "source_app_id", "source_version", "initial_installation"},
    ):
        return (
            _matches(_PLAN_ID, payload["candidate_plan"])
            and _matches(_TENANT_REF, payload["tenant_id"])
            and _matches(_APP_ID, payload["source_app_id"])
            and _pos_int(payload["source_version"])
            and payload.get("initial_installation", True) is True
        )
    if keys == {"consumer_sources"}:
        return payload["consumer_sources"] is True
    if keys == {"verify_task", "plan_id"}:
        return _matches(_TENANT_REF, payload["verify_task"]) and _matches(_PLAN_ID, payload["plan_id"])
    if keys == {"plan_id"}:
        return _matches(_PLAN_ID, payload["plan_id"])
    if keys in (
        {"site_task", "tenant_id"},
        {"workspace_task", "tenant_id"},
        {"knowledge_task", "tenant_id"},
        {"connection_task", "tenant_id"},
        {"connection_task", "tenant_id", "source_app_id", "source_version"},
    ):
        parent = next(payload[k] for k in _PARENT_TASK_KEYS if k in payload)
        return (
            _matches(_TASK_ID, parent)
            and _matches(_TENANT_REF, payload["tenant_id"])
            and (
                "source_app_id" not in payload
                or (_matches(_APP_ID, payload["source_app_id"]) and _pos_int(payload.get("source_version")))
            )
        )
    if keys == {"environment"}:
        return _matches(_ENV_ID, payload["environment"])
    return (
        keys in ({"id"}, {"id", "cancel"})
        and _matches(_TASK_ID, payload.get("id"))
        and payload.get("cancel", True) is True
    )


def _validate_environment_plan(payload: dict, rejected) -> None:
    keys = set(payload)
    if keys == {"id"}:
        if not _matches(_PLAN_ID, payload["id"]):
            raise rejected(400, "无效开通计划")
        return
    if keys != {"environment", "revision", "registry_revision"} or (
        not _matches(_ENV_PLAN_ID, payload["environment"])
        or not _pos_int(payload["revision"])
        or not _pos_int(payload["registry_revision"])
    ):
        raise rejected(400, "无效开通计划")


def _validate_environment_draft(payload: dict, rejected) -> None:
    if (
        set(payload) != {"draft", "revision", "registry_revision"}
        or not isinstance(payload["draft"], dict)
        or type(payload["revision"]) is not int
        or payload["revision"] < 0
        or not _pos_int(payload["registry_revision"])
    ):
        raise rejected(400, "无效目录草稿参数")
    candidate = payload["draft"]
    if set(candidate) != _DRAFT_FIELDS:
        raise rejected(400, "无效目录草稿字段")
    tenants = candidate["tenants"]
    if (
        type(candidate["enabled"]) is not bool
        or not isinstance(tenants, list)
        or len(tenants) > 10000
        or any(
            not isinstance(t, dict)
            or set(t) != {"id", "name", "enabled"}
            or type(t["enabled"]) is not bool
            or not isinstance(t["id"], str)
            or not isinstance(t["name"], str)
            for t in tenants
        )
        or any(
            not isinstance(candidate[k], str) or len(candidate[k]) > 2048
            for k in _DRAFT_FIELDS - {"enabled", "tenants"}
        )
    ):
        raise rejected(400, "无效目录草稿字段")


def _validate_environment_update(payload: dict, rejected) -> None:
    required = {"environment", "revision", "name", "enabled"}
    if (
        not required.issubset(payload)
        or set(payload) - (required | {"tenant_id"})
        or type(payload["enabled"]) is not bool
        or not _pos_int(payload["revision"])
        or not isinstance(payload["name"], str)
        or not 1 <= len(payload["name"]) <= 128
        or any(
            not _matches(_TENANT_REF, payload.get(k)) for k in ("environment", "tenant_id") if k in payload
        )
    ):
        raise rejected(400, "无效环境管理参数")


def _validate_run_logs(action: str, payload: dict, rejected) -> None:
    expected = {"app_id", "page", "limit"} if action == "run-logs" else {"app_id", "run_id"}
    if set(payload) - expected:
        raise rejected(400, "无效日志参数")
    if action == "run-logs":
        for key, default, maximum in (("page", 1, 1000), ("limit", 20, 50)):
            value = payload.get(key, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise rejected(400, "无效日志分页")
    elif not _matches(_RUN_ID, payload.get("run_id")):
        raise rejected(400, "无效运行编号")


def _validate_trial(action: str, payload: dict, settings, rejected) -> None:
    expected = {"app_id", "question", "order_no"} if action == "trial" else {"app_id", "trial_id"}
    if set(payload) - expected or not settings.trial_key:
        raise rejected(400, "无效预览参数")
    if action == "trial":
        if not isinstance(payload.get("question"), str) or not 0 < len(payload["question"]) <= 4000:
            raise rejected(400, "请填写预览问题")
        raw_order = payload.get("order_no", "")
        if not isinstance(raw_order, str) or (raw_order and not is_order_identifier(raw_order)):
            raise rejected(400, "无效订单号")
    elif not _matches(_TRIAL_ID, payload.get("trial_id")):
        raise rejected(400, "无效预览")


def _validate_payload(action: str, payload: dict, settings, rejected) -> None:
    """按固定动作契约逐字段校验形状，并拦截不属于本动作的跨动作字段。"""
    if action == "environment-task":
        if not _is_valid_environment_task(payload):
            raise rejected(400, "无效环境任务")
    elif set(payload) & _ENV_TASK_ONLY_KEYS:
        raise rejected(400, "无效参数")

    if action == "environment-wizard":
        if (
            set(payload) != {"wizard_action", "wizard_payload"}
            or payload.get("wizard_action") not in _WIZARD_ACTIONS
            or not isinstance(payload.get("wizard_payload"), dict)
        ):
            raise rejected(400, "无效向导参数")
    elif set(payload) & {"wizard_action", "wizard_payload"}:
        raise rejected(400, "无效参数")

    if action == "environment-plan":
        _validate_environment_plan(payload, rejected)
    elif action == "environment-draft-save":
        _validate_environment_draft(payload, rejected)
    elif "registry_revision" in payload:
        raise rejected(400, "无效参数")

    if action == "environment-update":
        _validate_environment_update(payload, rejected)
    elif action not in {"environment-plan", "environment-task"} and set(payload) & {"environment", "enabled"}:
        raise rejected(400, "无效参数")

    _validate_scope_and_sources(action, payload, rejected)

    if action in {"run-logs", "run-log"}:
        _validate_run_logs(action, payload, rejected)
    elif set(payload) & {"page", "limit", "run_id"}:
        raise rejected(400, "无效参数")

    if action.startswith("trial"):
        _validate_trial(action, payload, settings, rejected)
    elif action == "knowledge-access":
        if (
            set(payload) != {"app_id", "knowledge_base_id", "allowed"}
            or type(payload["allowed"]) is not bool
            or not _matches(_KB_ID, payload["knowledge_base_id"])
        ):
            raise rejected(400, "无效知识授权参数")
    elif set(payload) & {"question", "order_no", "trial_id", "knowledge_base_id", "allowed"}:
        raise rejected(400, "无效参数")


def _verify_app_access(handler, session, app_id: object) -> None:
    """确认当前 Dify 工作区能读取目标 app（防止跨工作区越权管理）。"""
    rejected = bridge._Rejected
    if not _matches(_APP_ID, app_id):
        raise rejected(400, "无效应用")
    code, app = bridge._request(
        handler.settings.dify_base_url + "/console/api/apps/" + app_id,
        headers=session.dify_headers(),
        timeout=15,
    )
    if code != 200 or not isinstance(app, dict) or app.get("id") != app_id:
        raise rejected(403, "无权管理此应用")


def _switch_workspace(handler, session, result: dict) -> None:
    """切换浏览器当前 Dify 工作区；平台账号未加入目标空间时先走网关受控加成员。"""
    rejected = bridge._Rejected
    workspace_id = result.get("workspace_id", "")
    http_status, profile = bridge._request(
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
        raise rejected(403, "无法核对当前Dify账号")
    http_status, response = bridge._request(
        handler.settings.dify_base_url + "/console/api/workspaces",
        headers=session.dify_headers(),
        timeout=15,
    )
    workspaces = response.get("workspaces", []) if isinstance(response, dict) else []
    if http_status != 200:
        raise rejected(502, "无法读取工作区")
    if not any(w.get("id") == workspace_id for w in workspaces):
        if not result.get("platform"):
            raise rejected(403, "当前账号没有此工作区访问权")
        http_status, response = bridge._request(
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
            raise rejected(502, "暂时无法进入客户工作区，请重试")
    http_status, response = bridge._request(
        handler.settings.dify_base_url + "/console/api/workspaces/switch",
        method="POST",
        headers={**session.dify_headers(), "Content-Type": "application/json"},
        body={"tenant_id": workspace_id},
        timeout=15,
    )
    if http_status != 200 or not isinstance(response, dict) or response.get("result") != "success":
        raise rejected(502, "工作区切换失败，请重试")


def handle(handler):
    """固定管理动作入口：验会话 → 解析路由 → 校验载荷 → 转发网关 → 可选切工作区。"""
    session = _verify_browser_session(handler)
    payload = handler._read_bounded_json(65536, _ALLOWED_PAYLOAD_KEYS)
    action = payload.pop("action", "")
    method, path = _resolve_action_route(action, payload, bridge._Rejected)
    _validate_payload(action, payload, handler.settings, bridge._Rejected)

    if action in _APP_SCOPED_ACTIONS:
        _verify_app_access(handler, session, payload.get("app_id", ""))

    key = handler.settings.trial_key if action.startswith("trial") else handler.settings.publish_key
    req_timeout = 180 if path == "/environment-wizard" else 30
    code, result = bridge._request(
        handler.settings.gateway_base_url + "/v1/dify/management" + path,
        method=method,
        headers={
            "Authorization": "Bearer " + key,
            "X-AIOps-Console-Session": session.access_token,
            "Content-Type": "application/json",
        },
        body=payload if method == "POST" else None,
        timeout=req_timeout,
    )
    if (
        action == "status"
        and isinstance(result, dict)
        and result.get("error", {}).get("code") == "SETUP_DISABLED"
    ):
        handler._json(200, {"managed": False})
        return
    if action == "workspace" and code == 200 and isinstance(result, dict):
        _switch_workspace(handler, session, result)
    handler._json(code, result if isinstance(result, dict) else {"error": {"code": "SETUP_UNAVAILABLE"}})


def authorize_workspace(handler, session, workspace_id):
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


def _validate_scope_and_sources(action, payload, rejected):
    """`scope`/`sources` 只属于 `create`，且形状固定。"""
    if "scope" not in payload and "sources" not in payload:
        return
    if action != "create":
        raise rejected(400, "无效参数")
    scope = payload.get("scope", "both")
    if not isinstance(scope, str) or scope not in _SCOPES:
        raise rejected(400, "无效开通范围")
    sources = payload.get("sources")
    if sources is None:
        return
    if not isinstance(sources, dict) or not sources or set(sources) - _ENTRIES:
        raise rejected(400, "无效来源")
    wanted = _ENTRIES if scope == "both" else {scope}
    if set(sources) != wanted:
        raise rejected(400, "来源与开通范围不一致")
    for spec in sources.values():
        if not isinstance(spec, dict) or set(spec) != {"app_id", "version_no"}:
            raise rejected(400, "无效来源")
        if not _matches(_APP_ID, spec["app_id"]):
            raise rejected(400, "无效来源应用")
        if not _pos_int(spec["version_no"]):
            raise rejected(400, "无效来源版本")
