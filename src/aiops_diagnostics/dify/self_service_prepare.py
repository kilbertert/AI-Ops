"""工作区依赖和应用创建；外部副作用均可用持久操作ID对账。"""

import json
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlencode

import yaml

from aiops_diagnostics.common.private_files import validate_private_file
from aiops_diagnostics.operator.ops_provider import ensure_node_provider

from .dify_ensure_workspace import ensure_workspace
from .dify_inner_api import DifyInnerApi, InnerApiSettings
from .dify_workspaces import DifyWorkspaces
from .self_service_identity import SetupRefused


def workspace(settings, tenant, name, console):
    outcome = ensure_workspace(
        tenant_id=tenant,
        environment=settings.environment,
        workspaces=DifyWorkspaces(settings.database_file),
        inner=DifyInnerApi(InnerApiSettings(settings.dify_inner_api_base_url, settings.dify_inner_api_key)),
        console_base_url=settings.dify_console_base_url,
        console_api_key=settings.dify_console_api_key,
        company=name,
    )
    if not outcome.workspace_id:
        raise SetupRefused("SETUP_WORKSPACE_PENDING")
    return outcome.workspace_id


def dependencies(console, workspace_id, profile, gateway_url):
    model = profile["model"]
    console.install_plugins(workspace_id, [model["plugin"]])
    path = "/console/api/workspaces/current/model-providers/" + model["provider"] + "/models/credentials"
    query = "?" + urlencode({"model": model["name"], "model_type": "llm"})
    existing = console._call(workspace_id, path + query)
    credential_path = Path(model["credential_file"])
    validate_private_file(credential_path)
    credentials = json.loads(credential_path.read_text())
    if not existing.get("available_credentials"):
        console._call(
            workspace_id,
            path,
            method="POST",
            body={
                "model": model["name"],
                "model_type": "llm",
                "name": "aiops-managed",
                "credentials": credentials,
            },
        )
    saved = console._call(workspace_id, path + query)
    if not saved.get("current_credential_id"):
        available = saved.get("available_credentials", [])
        selected = next((c for c in available if c.get("credential_name") == "aiops-managed"), None)
        if not selected:
            raise SetupRefused("SETUP_MODEL_CONFLICT")
        console._call(
            workspace_id,
            path + "/switch",
            method="POST",
            body={"model": model["name"], "model_type": "llm", "credential_id": selected["credential_id"]},
        )
        saved = console._call(workspace_id, path + query)
    observed = saved.get("credentials", {})
    if not saved.get("current_credential_id") or saved.get("load_balancing", {}).get("enabled"):
        raise SetupRefused("SETUP_MODEL_CONFLICT")
    for key in ("endpoint_url", "max_tokens_to_sample", "function_calling_type", "stream_function_calling"):
        if observed.get(key) != credentials.get(key):
            raise SetupRefused("SETUP_MODEL_CONFLICT")
    manifest_path = Path(profile["target"]["manifest_file"])
    validate_private_file(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    ensure_node_provider(console, workspace_id, gateway_url, manifest)
    providers = console._call(workspace_id, "/console/api/workspaces/current/tool-providers?type=api")
    matches = [p for p in providers if p.get("name") == "cpms-node"]
    if len(matches) != 1:
        raise SetupRefused("SETUP_PROVIDER_UNAVAILABLE")
    return matches[0]["id"]


def create_app(console, workspace_id, job_id, request, profile, provider_id):
    from uuid import UUID, uuid5

    capability = console._call(workspace_id, "/console/api/apps/imports")
    if capability.get("aiops_idempotent_import") != 1:
        raise SetupRefused("SETUP_IMPORT_UPGRADE_REQUIRED")

    expected_id = str(uuid5(UUID("6e5a60f0-9495-4c43-a714-d954c2609aeb"), workspace_id + ":" + job_id))
    doc = yaml.safe_load(
        files("aiops_diagnostics.dify").joinpath("dify_workspace_template.dsl.yml").read_text()
    )
    doc["app"].update({"name": request["name"], "description": "管家订单诊断", "mode": "agent-chat"})
    mc = doc["model_config"]
    model = profile["model"]
    mc["pre_prompt"] = request["prompt"]
    mc["model"] = {
        "provider": model["provider"],
        "name": model["name"],
        "mode": "chat",
        "completion_params": model["completion_params"],
    }
    mc["agent_mode"] = {
        "enabled": True,
        "strategy": "function_call",
        "max_iteration": 10,
        "tools": [
            {
                "provider_id": provider_id,
                "provider_name": "cpms-node",
                "provider_type": "api",
                "tool_name": name,
                "enabled": True,
                "tool_parameters": {},
            }
            for name in request["tools"]
        ],
    }
    result = console._call(
        workspace_id,
        "/console/api/apps/imports",
        method="POST",
        body={
            "mode": "yaml-content",
            "yaml_content": yaml.safe_dump(doc, allow_unicode=True),
            "name": request["name"],
            "request_id": job_id,
        },
    )
    if (
        result.get("status") not in {"completed", "completed-with-warnings"}
        or result.get("app_id") != expected_id
    ):
        raise SetupRefused("SETUP_IMPORT_PENDING")
    return result["app_id"]


def knowledge(context, tenant_id):
    from aiops_diagnostics.knowledge.kb_onboarding import (
        KbAdminClient,
        OnboardError,
        decrypt_credential,
        onboard_tenant,
    )

    settings = context.settings
    if not all(
        (
            settings.kb_service_base_url,
            settings.ragflow_credential_key,
            settings.ragflow_embedding_base_url,
            settings.ragflow_embedding_api_key,
        )
    ):
        raise SetupRefused("SETUP_KNOWLEDGE_CONFIG_REQUIRED")
    try:
        kb = KbAdminClient(settings.kb_service_base_url)
        outcome = onboard_tenant(
            tenant_id=tenant_id,
            environment=settings.environment,
            store=context.store,
            kb=kb,
            credential_key=settings.ragflow_credential_key,
            embedding_base_url=settings.ragflow_embedding_base_url,
            embedding_api_key=settings.ragflow_embedding_api_key,
            embedding_model=settings.ragflow_embedding_model,
            tenant_domain=settings.ragflow_tenant_domain,
        )
        stored = context.store.get_ragflow_tenant(tenant_id, settings.environment)
        kb.verify_login(
            tenant_id=tenant_id,
            password=decrypt_credential(stored["credential_ciphertext"], settings.ragflow_credential_key),
        )
        if not outcome.model_verified:
            raise SetupRefused("SETUP_KNOWLEDGE_FAILED")
    except OnboardError as exc:
        raise SetupRefused("SETUP_KNOWLEDGE_FAILED") from exc


def knowledge_connection(context, workspace_id, console):
    from .knowledge_connection import ensure

    result = []

    def work():
        result.append(ensure(console, workspace_id, context.settings))

    guard = context.runtime.knowledge_dataset_sync_guard
    guard.run(work=work)
    if not result:
        raise SetupRefused("SETUP_KNOWLEDGE_CONNECTION_FAILED")
    return result[0]


# ── 客户端（consumer）准备 ────────────────────────────────────────────────
#
# 客户端和管家走的是两条**不同**的创建路径，这不是没统一，而是它们本来就是两种东西：
# 管家应用由平台模板 + 清单工具生成（`create_app`），客户端应用是**现役 Chatflow 的副本**。
# 用模板生成一个客服应用等于重写一份编排，而运营已经调好的编排在现役应用里。


def customer_knowledge_base(context, tenant_id: str, name: str) -> str:
    """该客户的对客知识库 id：按名字复用，没有就建一个空库。

    **空库是有意为之**：新建客户没有资料是常态，拿别的客户的资料补上才是事故。
    客服在这种库上会答"暂无资料"，这正是要的行为（规划 §5）。
    """
    client = getattr(context.runtime, "kb_search_client", None)
    if client is None:
        raise SetupRefused("SETUP_KNOWLEDGE_CONFIG_REQUIRED")
    from aiops_diagnostics.knowledge.knowledge_retrieval import KnowledgeSearchUnavailable

    tenant = client.for_tenant(tenant_id)
    try:
        for row in tenant.list_knowledge_bases():
            if row.get("name") == name:
                return str(row["knowledge_base_id"])
        created = tenant.create_knowledge_base(name, "客户对客资料（开通自动创建）")
    except KnowledgeSearchUnavailable as exc:
        raise SetupRefused("SETUP_KNOWLEDGE_FAILED") from exc
    knowledge_base_id = str(created.get("id") or created.get("knowledge_base_id") or "")
    if not knowledge_base_id:
        raise SetupRefused("SETUP_KNOWLEDGE_FAILED")
    return knowledge_base_id


def rebind_knowledge(document: dict, knowledge_ids, *, forbidden_ids=()) -> dict:
    """把副本里的知识选择节点**整段替换**成目标客户自己的库。

    为什么是"替换"而不是"映射"：任何映射规则都可能把源客户的库 ID 留下来（映射没命中时
    最自然的实现就是"保持原样"），而留下的那个 ID 在目标客户的空间里指向别人的资料。
    整段替换让"源 UUID 留在副本里"这件事在结构上不可能发生；剩下的唯一入口是调用方
    传进来，所以调用方给的 ID 必须来自目标租户自己的目录，并在这里再过一道 `forbidden_ids`。
    """
    from aiops_diagnostics.consumer.consumer_knowledge_selection import PROVIDER, TOOL

    selected = list(dict.fromkeys(str(value) for value in knowledge_ids if str(value).strip()))
    if not selected:
        raise SetupRefused("SETUP_KNOWLEDGE_BINDING_REQUIRED")
    if set(selected) & set(forbidden_ids):
        raise SetupRefused("SETUP_KNOWLEDGE_CROSS_TENANT")
    workflow = document.get("workflow")
    nodes = (workflow or {}).get("graph", {}).get("nodes") if isinstance(workflow, dict) else None
    if not isinstance(nodes, list):
        raise SetupRefused("SETUP_SOURCE_NOT_CONSUMER")
    matches = [
        node.get("data") or {}
        for node in nodes
        if isinstance(node, dict)
        and (node.get("data") or {}).get("type") == "tool"
        and (node.get("data") or {}).get("provider_id") == PROVIDER
        and (node.get("data") or {}).get("tool_name") == TOOL
    ]
    if len(matches) != 1:
        # 没有受控检索节点（旧 FAQ 流程）或出现多个：都不是可以自动绑定的形态。
        raise SetupRefused("SETUP_KNOWLEDGE_BINDING_REQUIRED")
    matches[0]["tool_configurations"] = {"knowledge_base_ids": {"type": "constant", "value": selected}}
    return document


def consumer_source_ids(document: dict) -> tuple[str, ...]:
    """来源应用自己选了哪些库 —— 只用于**排除**，从不用于复制。"""
    from aiops_diagnostics.consumer.consumer_knowledge_selection import selected_libraries

    workflow = document.get("workflow") or {}
    for node in (workflow.get("graph") or {}).get("nodes") or []:
        data = node.get("data") or {}
        if data.get("type") == "tool" and data.get("tool_name") == "consumer_knowledge":
            try:
                return selected_libraries(data)
            except ValueError:
                return ()
    return ()


def copy_consumer_app(console, source, workspace_id, job_id, request, document):
    """把已重绑定的 Chatflow 文档导入目标工作区，返回应用 id。

    幂等靠与管家同一条机制：``request_id`` 参与派生应用 id，响应丢失后回读同一应用。
    """
    from uuid import UUID, uuid5

    capability = console._call(workspace_id, "/console/api/apps/imports")
    if capability.get("aiops_idempotent_import") != 1:
        raise SetupRefused("SETUP_IMPORT_UPGRADE_REQUIRED")
    expected_id = str(
        uuid5(UUID("6e5a60f0-9495-4c43-a714-d954c2609aeb"), workspace_id + ":" + job_id + ":consumer")
    )
    result = console._call(
        workspace_id,
        "/console/api/apps/imports",
        method="POST",
        body={
            "mode": "yaml-content",
            "yaml_content": yaml.safe_dump(document, allow_unicode=True),
            "name": request["name"],
            "request_id": job_id + ":consumer",
        },
    )
    if (
        result.get("status") not in {"completed", "completed-with-warnings"}
        or result.get("app_id") != expected_id
    ):
        raise SetupRefused("SETUP_IMPORT_PENDING")
    return result["app_id"]
