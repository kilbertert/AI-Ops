"""消费者候选模板：正式版本与单库绑定，不能产生运行授权。"""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from aiops_diagnostics.consumer import consumer_agent_flow as flow
from aiops_diagnostics.consumer.consumer_publication import workflow_of
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

from . import candidate_knowledge, self_service_config, self_service_prepare
from . import self_service_identity as auth
from .dify_workspaces import DifyWorkspaces
from .environment_knowledge_connection import binding as connection_binding


def sources(context, caller, app_id=None):
    if not caller.platform:
        raise auth.SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
    if app_id is not None and (not isinstance(app_id, str) or not app_id):
        raise auth.SetupRefused("SETUP_SOURCE_NOT_AVAILABLE")
    allowed = {r["id"] for r in auth.tenants(context, caller)}
    result = []
    for route in ChatRouteBindingStore(context.settings.database_file).all():
        if (
            route.environment != context.settings.execution_environment
            or route.entry != "consumer"
            or route.scenario != "consumer_service"
            or not route.active
            or route.tenant_id not in allowed
            or not route.workflow_id
            or app_id is not None
            and route.app_id != app_id
        ):
            continue
        snapshot = context.agent_manager.store.version(
            route.agent_id, route.tenant_id, route.agent_version_no
        ).snapshot
        if snapshot.get("app_id") != route.app_id or snapshot.get("workflow_id") != route.workflow_id:
            continue
        if DifyWorkspaces(context.settings.database_file).resolve(
            route.tenant_id, context.settings.environment
        ) != snapshot.get("workspace_id"):
            continue
        workflow = snapshot.get("workflow") or {}
        if not flow.enabled(workflow):
            continue
        source = {
            "app_id": route.app_id,
            "name": snapshot.get("name", route.app_id),
            "tenant_id": route.tenant_id,
            "agent_id": route.agent_id,
            "version_no": route.agent_version_no,
            "workflow": workflow,
        }
        if app_id is not None:
            return source
        result.append({k: v for k, v in source.items() if k != "workflow"})
    if app_id is not None:
        raise auth.SetupRefused("SETUP_SOURCE_NOT_AVAILABLE")
    return {"sources": result}


def document(source, library):
    from .self_service_prepare import consumer_source_ids, rebind_knowledge

    doc = {"workflow": copy.deepcopy(source["workflow"])}
    doc["workflow"]["graph"].pop("viewport", None)
    for index, node in enumerate(doc["workflow"]["graph"]["nodes"]):
        node.update(type="custom", position={"x": index * 360, "y": 0})
    for edge in doc["workflow"]["graph"]["edges"]:
        edge.update(id=edge["source"] + "-" + edge["target"], type="custom")
    variables = doc["workflow"].get("environment_variables", [])
    for variable in variables:
        variable.pop("selector", None)  # Dify导出携带selector，草稿详情省略；它不是变量值。
    if any(
        v.get("value_type") != "string"
        or v.get("name")
        not in {"cpms_status_enabled", "cpms_billing_enabled", "knowledge_answer_mode", flow.MODE}
        for v in variables
    ):
        raise auth.SetupRefused("SETUP_SOURCE_NOT_CONSUMER")
    rebind_knowledge(doc, [library], forbidden_ids=consumer_source_ids(doc))
    for node in doc["workflow"]["graph"]["nodes"]:
        if node["data"]["type"] == "agent":
            node["data"]["agent_parameters"]["model"]["value"].pop("credential_id", None)
    flow.publication_settings(
        doc["workflow"],
        {"live_order_status": True, "live_billing_record": True},
        [{"knowledge_base_id": library, "audience": "customer"}],
    )
    doc.update(kind="app", version="0.7.0", app={"mode": "advanced-chat", "name": "候选客服"})
    return doc


def profile(settings, source):
    selected = next(
        n["data"]["agent_parameters"]["model"]["value"]
        for n in source["workflow"]["graph"]["nodes"]
        if n["data"]["type"] == "agent"
    )
    choices = {
        json.dumps(m, sort_keys=True): m
        for p in self_service_config.profiles(settings).values()
        for m in [p["model"], *p.get("consumer_models", [])]
        if (m["provider"], m["name"]) == (selected["provider"], selected["model"])
    }
    if len(choices) != 1:
        raise auth.SetupRefused("SETUP_MODEL_CONFLICT")
    return {"model": next(iter(choices.values()))}


def binding(settings, source):
    model = profile(settings, source)["model"]
    return workflow_of(
        {
            "connection": connection_binding(settings),
            "source": source,
            "model": model,
            "credential": hashlib.sha256(Path(model["credential_file"]).read_bytes()).hexdigest(),
        }
    )


def dependencies(console, settings, request, scope):
    from aiops_diagnostics.consumer.consumer_knowledge_selection import PROVIDER

    from .self_service_prepare import consumer_models, consumer_plugin

    doc = document(request["consumer_source"], scope[3])
    consumer_models(console, scope[2], doc, profile(settings, request["consumer_source"]))
    console.install_plugins(scope[2], self_service_prepare.consumer_plugins(doc))
    existing = console._call(
        scope[2], "/console/api/workspaces/current/tool-provider/builtin/" + PROVIDER + "/credentials"
    )
    if existing:
        verify_credential(console, settings, scope)
    else:
        consumer_plugin(
            console,
            scope[2],
            SimpleNamespace(public_base_url=settings.public_base_url),
            candidate_knowledge.token(settings.internal_token_secret, scope),
            doc,
        )
    return doc


def verify(console, settings, request, scope, app_id):
    import yaml

    from .dify_dsl_pull import workflow_content

    expected = workflow_content(yaml.safe_dump(document(request["consumer_source"], scope[3])))
    observed = console._call(scope[2], "/console/api/apps/" + app_id + "/workflows/draft")
    observed = workflow_content(yaml.safe_dump({"workflow": observed}))
    if any(
        observed.get(k) != expected.get(k)
        for k in ("graph", "features", "environment_variables", "conversation_variables")
    ):
        raise auth.SetupRefused("ENVIRONMENT_TASK_OBSERVATION_REQUIRED")
    verify_credential(console, settings, scope)


def verify_credential(console, settings, scope):
    from aiops_diagnostics.consumer.consumer_knowledge_selection import PROVIDER

    credentials = console._call(
        scope[2], "/console/api/workspaces/current/tool-provider/builtin/" + PROVIDER + "/credentials"
    )
    managed = [r for r in credentials if r.get("name") == "aiops-managed" and r.get("is_default")]
    if (
        len(credentials) != 1
        or len(managed) != 1
        or managed[0].get("credentials", {}).get("gateway_url") != settings.public_base_url.rstrip("/")
    ):
        raise auth.SetupRefused("SETUP_CONSUMER_PLUGIN_FAILED")
    options = console._call(
        scope[2],
        "/console/api/workspaces/current/plugin/parameters/dynamic-options?"
        + urlencode(
            {
                "plugin_id": "trendpower/consumer_knowledge",
                "provider": PROVIDER,
                "action": "consumer_knowledge",
                "parameter": "knowledge_base_ids",
                "provider_type": "tool",
            }
        ),
    )
    if [r.get("value") for r in options.get("options", [])] != [scope[3]]:
        raise auth.SetupRefused("SETUP_CONSUMER_PLUGIN_FAILED")
