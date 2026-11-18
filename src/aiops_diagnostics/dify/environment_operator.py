"""候选管家应用准备：仅资源装配，不登记或发布正式路由。"""

import hashlib
import json
import time
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.operator import operator_agent, operator_tools
from aiops_diagnostics.operator.ops_provider import ensure_node_provider

from . import candidate_knowledge, self_service_config, self_service_prepare
from .dify_console_admin import ConsoleApiError, ConsoleApiSettings, DifyConsoleAdmin
from .dify_dsl_pull import _declared_dataset_ids
from .environment_knowledge_connection import binding as connection_binding
from .self_service_identity import SetupRefused


def model(settings):
    profiles = self_service_config.profiles(settings)
    choices = {json.dumps(p["model"], sort_keys=True): p["model"] for p in profiles.values()}
    if len(choices) != 1:
        raise SetupRefused("SETUP_MODEL_CONFLICT")
    return next(iter(choices.values()))


def binding(settings):
    template = model(settings)
    return hashlib.sha256(
        json.dumps(
            {
                "connection": connection_binding(settings),
                "model": template,
                "credential": hashlib.sha256(Path(template["credential_file"]).read_bytes()).hexdigest(),
                "sop": operator_agent.sop(),
                "tools": [
                    dict(name=t.name, description=t.description, schema=t.input_schema())
                    for t in operator_tools.tools()
                ],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def run(context, path, reference, payload):
    if set(payload) != {"id", "owner", "prepare"} or payload["prepare"] != "operator":
        raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    identifier, owner, settings = payload["id"], payload["owner"], context.settings

    def current(fresh=False):
        return tasks.workspace_request(
            path, reference, identifier, owner, action="operator-prepare", reconciling=not fresh
        )

    request = current()
    scope = (
        request["plan"]["environment"],
        request["tenant_id"],
        request["workspace_id"],
        request["knowledge_base_id"],
    )
    resources = request.get("resources", {})

    def boundary():
        latest = current(fresh=True)
        if latest["cancel_requested"]:
            raise tasks.TaskRefused("ENVIRONMENT_TASK_CANCELLED")
        if request["binding"] != binding(settings):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
        candidate_knowledge.active(settings, scope)

    def save():
        tasks.save_knowledge_progress(
            path, reference, identifier, owner, resources, action="operator-prepare"
        )

    class Console(DifyConsoleAdmin):
        def _call(self, *args, method="GET", **kwargs):
            if method != "GET":
                boundary()
            return super()._call(*args, method=method, **kwargs)

    console = Console(ConsoleApiSettings(settings.dify_console_base_url, settings.dify_console_api_key))
    app_id, state, error = "", "observed", ""
    try:
        candidate_knowledge.active(settings, scope)
        observed_connection = console._call(
            scope[2], "/console/api/datasets/external-knowledge-api/" + request["connection_id"]
        )
        if (
            observed_connection.get("tenant_id") != scope[2]
            or observed_connection.get("settings", {}).get("endpoint")
            != settings.public_base_url.rstrip("/") + "/v1/dify"
            or observed_connection.get("settings", {}).get("api_key")
            != candidate_knowledge.token(settings.internal_token_secret, scope)
        ):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        if request["binding"] != binding(settings):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
        if resources and resources.get("binding") != request["binding"]:
            raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
        if not resources:
            boundary()
            operation = str(uuid4())
            resources.update(
                operation_id=operation,
                expected_app_id=str(
                    uuid5(UUID("6e5a60f0-9495-4c43-a714-d954c2609aeb"), scope[2] + ":" + operation)
                ),
                name="aiops-candidate-" + uuid4().hex,
                binding=request["binding"],
            )
            save()
        app_id = resources["expected_app_id"]
        try:
            app = console._call(scope[2], "/console/api/apps/" + app_id)
        except ConsoleApiError as exc:
            if exc.status != 404:
                raise
            app = None
        if app is None:
            if resources.get("attempted"):
                raise ConsoleApiError("DIFY_CONSOLE_UNREADABLE", "应用创建未确认，继续对账")
            capability = console._call(scope[2], "/console/api/apps/imports")
            if (
                capability.get("aiops_idempotent_import") != 1
                or capability.get("aiops_candidate_import") != 1
            ):
                raise SetupRefused("SETUP_IMPORT_UPGRADE_REQUIRED")
            template = model(settings)
            self_service_prepare.model_credentials(console, scope[2], template)
            ensure_node_provider(console, scope[2], request["gateway_url"], reconcile=True)
            providers = console._call(scope[2], "/console/api/workspaces/current/tool-providers?type=api")
            matches = [p for p in providers if p.get("name") == "cpms-node"]
            if len(matches) != 1:
                raise SetupRefused("SETUP_PROVIDER_UNAVAILABLE")
            resources["provider_id"] = matches[0]["id"]
            boundary()
            resources["attempted"] = True
            save()
            self_service_prepare.create_app(
                console,
                scope[2],
                resources["operation_id"],
                {
                    "name": resources["name"],
                    "prompt": operator_agent.sop(),
                    "tools": [t.name for t in operator_tools.tools()],
                },
                {"model": template},
                resources["provider_id"],
                candidate=True,
            )
            app = console._call(scope[2], "/console/api/apps/" + app_id)
        ensure_node_provider(console, scope[2], request["gateway_url"], verify_only=True)
        mc = app.get("model_config") or {}
        expected_tools = {t.name for t in operator_tools.tools()}
        tools = mc.get("agent_mode", {}).get("tools", [])
        template = model(settings)
        if (
            app.get("id") != app_id
            or app.get("name") != resources["name"]
            or app.get("mode") != "agent-chat"
            or app.get("enable_api") is not False
            or app.get("enable_site") is not False
            or mc.get("pre_prompt") != operator_agent.sop()
            or {t.get("tool_name") for t in tools} != expected_tools
            or len(tools) != len(expected_tools)
            or any(
                t.get("provider_id") != resources["provider_id"]
                or t.get("provider_type") != "api"
                or t.get("enabled") is not True
                for t in tools
            )
            or mc.get("model", {}).get("name") != template["name"]
            or mc.get("model", {}).get("provider") != template["provider"]
            or mc.get("model", {}).get("completion_params") != template["completion_params"]
            or mc.get("agent_mode", {}).get("enabled") is not True
            or mc.get("agent_mode", {}).get("strategy") != "function_call"
            or mc.get("agent_mode", {}).get("max_iteration") != 10
            or _declared_dataset_ids(mc)
        ):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
        candidate_knowledge.active(settings, scope)
        if request["binding"] != binding(settings):
            raise tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
        current()
        resources["app_id"] = app_id
        save()
    except (ValueError, RuntimeError, OSError) as exc:
        code = getattr(exc, "code", str(exc))
        if code in {"ENVIRONMENT_TASK_LEASE_LOST", "ENVIRONMENT_TASK_UNAVAILABLE"}:
            raise
        if isinstance(exc, ConsoleApiError) and (
            code in {"DIFY_CONSOLE_UNREACHABLE", "DIFY_CONSOLE_UNREADABLE"}
            or exc.status
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
            if isinstance(exc, (SetupRefused, tasks.TaskRefused, ConsoleApiError))
            else "ENVIRONMENT_TASK_OBSERVATION_REQUIRED",
        )
    now = time.time()
    return tasks.finish_workspace(
        path,
        reference,
        identifier,
        owner,
        {
            "scope": "candidate-operator-application-only",
            "environment": scope[0],
            "tenant_id": scope[1],
            "workspace_id": scope[2],
            "knowledge_base_id": scope[3],
            "connection_id": request["connection_id"],
            "app_id": app_id,
            "state": state,
            "error_code": error,
            "executable": False,
            "created_at": now,
            "expires_at": now + 900,
            "checks": [
                *[{"id": k, "state": "passed"} for k in ("workspace", "knowledge", "connection")],
                {"id": "application", "state": "passed" if state == "observed" else "blocked"},
                {"id": "business", "state": "pending"},
            ],
        },
    )
