"""候选工作区知识API准备，未知创建只对账，不发布应用。"""

import hashlib
import time
from types import SimpleNamespace

from aiops_diagnostics.dify.dify_console_admin import ConsoleApiError, ConsoleApiSettings, DifyConsoleAdmin
from aiops_diagnostics.dify.dify_ensure_workspace import EnsureError, reconcile_by_name
from aiops_diagnostics.dify.knowledge_connection import PATH, ensure
from aiops_diagnostics.knowledge.knowledge_retrieval import KbServiceClient, KnowledgeSearchUnavailable
from aiops_diagnostics.onboarding import candidate_knowledge as candidate
from aiops_diagnostics.onboarding import tasks
from aiops_diagnostics.onboarding.candidate_knowledge import connection_binding as binding


def run(context, path, reference, payload):
    if set(payload) != {"id", "owner", "prepare"} or payload["prepare"] != "connection":
        raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    identifier, owner, settings = payload["id"], payload["owner"], context.settings

    def current(fresh=False):
        return tasks.workspace_request(
            path, reference, identifier, owner, action="knowledge-connect", reconciling=not fresh
        )

    request = current()
    scope = (
        request["plan"]["environment"],
        request["tenant_id"],
        request["workspace_id"],
        request["knowledge_base_id"],
    )
    resources = request.get("resources", {})
    state, error, connection = "observed", "", ""

    def save():
        latest = current(fresh=True)
        if latest["cancel_requested"]:
            raise tasks.TaskRefused("ENVIRONMENT_TASK_CANCELLED")
        if request["binding"] != binding(settings):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
        tasks.save_knowledge_progress(
            path, reference, identifier, owner, resources, action="knowledge-connect"
        )

    try:
        if request["binding"] != binding(settings):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
        if not resources.get("attempted"):
            save()
        scope_id = candidate.knowledge_id(scope)
        resources["binding"] = request["binding"]
        resources["name"] = (
            resources.get("name") or "aiops-kb-" + hashlib.sha256(scope_id.encode()).hexdigest()[:31]
        )
        if resources["name"] == "aiops-kb-" + hashlib.sha256(scope_id.encode()).hexdigest()[:32]:
            # Dify 1.17.1 在副作用前拒绝41字符名称；仅接续这一确定无创建的旧格式。
            resources["name"] = resources["name"][:-1]
            resources.pop("attempted", None)
        rows = reconcile_by_name(
            console_base_url=settings.dify_console_base_url,
            console_api_key=settings.dify_console_api_key,
            name="",
            workspace_id=scope[2],
        )
        if len(rows) != 1:
            raise ConsoleApiError("DIFY_KNOWLEDGE_CONNECTION_UNCONFIRMED", "工作区未确认")
        from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

        ws = DifyWorkspaces(settings.database_file)
        if (
            ws.resolve(scope[1], scope[0]) != scope[2]
            or ws.tenant_for_workspace(scope[2], scope[0], across_environments=True) != scope[1]
        ):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        libraries = KbServiceClient(
            settings.kb_service_base_url,
            tenant_id=scope[1],
            environment=scope[0],
            timeout=getattr(settings, "kb_service_timeout_seconds", 10),
        ).list_knowledge_bases()
        if sum(row["knowledge_base_id"] == scope[3] for row in libraries) != 1:
            raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        console = DifyConsoleAdmin(
            ConsoleApiSettings(settings.dify_console_base_url, settings.dify_console_api_key)
        )
        connection_settings = SimpleNamespace(
            public_base_url=settings.public_base_url,
            environment="unused",
            dify_external_knowledge_api_id=resources.get("connection_id", ""),
            dify_knowledge_api_key=candidate.token(settings.internal_token_secret, scope),
        )
        # 固定名字保存后只对账，不使用控制环境原API配置。
        connection = ensure(
            console, scope[2], connection_settings, pending=resources, save=save, name=resources["name"]
        )
        observed = console._call(scope[2], PATH + "/" + connection)
        if (
            observed.get("tenant_id") != scope[2]
            or observed.get("settings", {}).get("endpoint")
            != settings.public_base_url.rstrip("/") + "/v1/dify"
        ):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        if observed.get("settings", {}).get("api_key") != connection_settings.dify_knowledge_api_key:
            raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        if request["binding"] != binding(settings):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
        resources["connection_id"] = connection
        tasks.save_knowledge_progress(
            path, reference, identifier, owner, resources, action="knowledge-connect"
        )
    except (ConsoleApiError, KnowledgeSearchUnavailable, ValueError, RuntimeError, OSError) as exc:
        code = getattr(exc, "code", str(exc))
        if code in {"ENVIRONMENT_TASK_LEASE_LOST", "ENVIRONMENT_TASK_UNAVAILABLE"}:
            raise
        if (
            isinstance(exc, ConsoleApiError)
            and code
            in {
                "DIFY_KNOWLEDGE_CONNECTION_UNCONFIRMED",
                "DIFY_CONSOLE_UNREACHABLE",
                "DIFY_CONSOLE_UNREADABLE",
            }
            or isinstance(exc, KnowledgeSearchUnavailable)
            or isinstance(exc, EnsureError)
            and code == "DIFY_RECONCILE_UNAVAILABLE"
            or isinstance(exc, ConsoleApiError)
            and exc.status is not None
            and 500 <= exc.status < 600
        ):
            return tasks.worker(
                path,
                reference,
                {"id": identifier, "owner": owner, "error": "ENVIRONMENT_TASK_TARGET_UNCONFIRMED"},
            )
        state, error = (
            "blocked",
            code
            if isinstance(exc, (ConsoleApiError, tasks.TaskRefused))
            else "ENVIRONMENT_TASK_OBSERVATION_REQUIRED",
        )
    now = time.time()
    result = {
        "scope": "candidate-knowledge-connection-only",
        "environment": scope[0],
        "tenant_id": scope[1],
        "workspace_id": scope[2],
        "knowledge_base_id": scope[3],
        "connection_id": connection,
        "state": state,
        "error_code": error,
        "executable": False,
        "created_at": now,
        "expires_at": now + 900,
        "checks": [
            *[{"id": k, "state": "passed"} for k in ("workspace", "knowledge")],
            {"id": "connection", "state": "passed" if state == "observed" else "blocked"},
            *[{"id": k, "state": "pending"} for k in ("application", "business")],
        ],
    }
    return tasks.finish_workspace(path, reference, identifier, owner, result)
