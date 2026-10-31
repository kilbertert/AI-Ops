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


def model_credentials(console, workspace_id, model):
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


def dependencies(console, workspace_id, profile, gateway_url):
    model_credentials(console, workspace_id, profile["model"])
    manifest_path = Path(profile["target"]["manifest_file"])
    validate_private_file(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    try:
        ensure_node_provider(console, workspace_id, gateway_url, manifest)
    except ValueError as exc:
        # 声明加了工具（#1205 的 `cpms_site_search`）但工作区里的 provider 还是旧 schema：
        # 这不是"开通失败"，是**这个工作区的提供方比声明旧**。迁移是一次运营动作
        # （`ensure_node_provider(migrate=True)`），不在这里替运营决定；但页面必须看得懂原因，
        # 而不是一个 SETUP_STEP_FAILED。2026-10-31 两个客户都是这么卡住的。
        raise SetupRefused("SETUP_PROVIDER_OUTDATED") from exc
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


def customer_knowledge_base(context, tenant_id: str, operation: dict, save) -> str:
    """先持久化创建身份；结果不明时只对账，不再次POST或认领旧的同名库。"""
    import secrets

    client = getattr(context.runtime, "kb_search_client", None)
    if client is None:
        raise SetupRefused("SETUP_KNOWLEDGE_CONFIG_REQUIRED")
    tenant = client.for_tenant(tenant_id)
    if "knowledge_create" not in operation:
        operation["knowledge_create"] = {
            "name": "对客知识库-" + secrets.token_hex(16),
            "before": [r["knowledge_base_id"] for r in tenant.list_knowledge_bases()],
        }
        save("consumer:knowledge")
    pending = operation["knowledge_create"]

    def reconcile():
        candidates = [
            r["knowledge_base_id"]
            for r in tenant.list_knowledge_bases()
            if r.get("name") == pending["name"] and r["knowledge_base_id"] not in pending["before"]
        ]
        if len(candidates) != 1:
            raise SetupRefused("SETUP_KNOWLEDGE_CREATE_UNCONFIRMED")
        return candidates[0]

    if pending.get("attempted"):
        return reconcile()
    pending["attempted"] = True
    save("consumer:knowledge")
    try:
        created = tenant.create_knowledge_base(pending["name"], "客户对客资料（开通自动创建）")
    except Exception:
        return reconcile()
    knowledge_base_id = str(created.get("id") or created.get("knowledge_base_id") or "")
    if not knowledge_base_id or knowledge_base_id in pending["before"]:
        raise SetupRefused("SETUP_KNOWLEDGE_CREATE_UNCONFIRMED")
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
    matches[0].pop("credential_id", None)
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


#: 幂等导入的命名空间。与 `deploy/dify-36/patches/1.17.1/09-idempotent-app-import.patch`
#: 里的那个常量**必须逐字相同**：应用 id 由补丁按 `uuid5(NS, tenant_id + ":" + request_id)`
#: 派生，我们只是提前算出同一个值用于核对。
IMPORT_NAMESPACE = "6e5a60f0-9495-4c43-a714-d954c2609aeb"


def consumer_request_id(job_id):
    """客户端导入的 `request_id`：UUID 形状，且对同一 job 稳定。

    补丁对 `request_id` 的约束是 ``^[a-f0-9-]{36}$``（见 `AppImportPayload`）。原来的
    ``job_id + ":consumer"`` 带冒号与后缀，真实 Dify 会以
    ``string_pattern_mismatch`` 拒绝 —— 而替身不校验，所以它一直是绿的。
    """
    from uuid import UUID, uuid5

    return str(uuid5(UUID(IMPORT_NAMESPACE), "consumer:" + job_id))


def consumer_app_id(workspace_id, job_id):
    """与补丁一致的派生：``uuid5(NS, tenant_id + ":" + request_id)``。

    Dify 的 `tenant_id` 就是工作区 id（`_current_user_and_tenant_id` 返回的那个），
    所以这里用 `workspace_id`。**两侧必须算同一个值**，否则回读不到同一应用，
    响应丢失后就会再建一个。
    """
    from uuid import UUID, uuid5

    return str(uuid5(UUID(IMPORT_NAMESPACE), workspace_id + ":" + consumer_request_id(job_id)))


def copy_consumer_app(console, source, workspace_id, job_id, request, document):
    """把已重绑定的 Chatflow 文档导入目标工作区，返回应用 id。

    幂等靠与管家同一条机制：``request_id`` 参与派生应用 id，响应丢失后回读同一应用。
    """
    capability = console._call(workspace_id, "/console/api/apps/imports")
    if capability.get("aiops_idempotent_import") != 1:
        raise SetupRefused("SETUP_IMPORT_UPGRADE_REQUIRED")
    expected_id = consumer_app_id(workspace_id, job_id)
    result = console._call(
        workspace_id,
        "/console/api/apps/imports",
        method="POST",
        body={
            "mode": "yaml-content",
            "yaml_content": yaml.safe_dump(document, allow_unicode=True),
            "name": request["name"],
            "request_id": consumer_request_id(job_id),
        },
    )
    if (
        result.get("status") not in {"completed", "completed-with-warnings"}
        or result.get("app_id") != expected_id
    ):
        raise SetupRefused("SETUP_IMPORT_PENDING")
    return result["app_id"]


# ── 客户端依赖：插件、目录凭据、模型凭据、业务连接 ────────────────────────


def consumer_endpoint(context, tenant_id: str) -> str:
    """把**已批准的环境连接**登记成这个租户的受管业务地址，并返回它。

    没有已批准连接就拒绝这一端（`SETUP_BUSINESS_CONNECTION_REQUIRED`）：
    开通不能靠"上次那个全局 URL"，因为那可能属于别的客户。
    """
    from aiops_diagnostics.consumer.consumer_knowledge_grants import ConsumerEndpoints, approved_endpoint

    settings = context.settings
    base = approved_endpoint(settings.charging_status_base_url, tenant_id)
    if not base:
        raise SetupRefused("SETUP_BUSINESS_CONNECTION_REQUIRED")
    from aiops_diagnostics.gateway.gateway_config import canonical_gateway_url

    base = canonical_gateway_url(base)
    ConsumerEndpoints(settings.database_file).register(
        tenant_id, settings.execution_environment, base, "setup"
    )
    stored = ConsumerEndpoints(settings.database_file).get(tenant_id, settings.execution_environment)
    if stored != base:
        # 写完回读：登记失败却继续发布，等于"发布了但第一次查单就 500"。
        raise SetupRefused("SETUP_BUSINESS_CONNECTION_REQUIRED")
    return base


def consumer_plugin(console, workspace_id: str, settings, catalogue_token: str) -> str:
    """按 Dify 1.17.1 的脱敏凭据列表对账，写入后校验并启用受管凭据。"""
    from aiops_diagnostics.consumer.consumer_knowledge_selection import PROVIDER

    if not getattr(settings, "consumer_knowledge_plugin", ""):
        raise SetupRefused("SETUP_CONSUMER_PLUGIN_REQUIRED")
    console.install_plugins(workspace_id, [settings.consumer_knowledge_plugin])
    base = f"/console/api/workspaces/current/tool-provider/builtin/{PROVIDER}"
    credentials = {
        "gateway_url": settings.public_base_url.rstrip("/"),
        "catalogue_token": catalogue_token,
    }

    def managed():
        rows = console._call(workspace_id, base + "/credentials")
        if not isinstance(rows, list):
            raise SetupRefused("SETUP_CONSUMER_PLUGIN_FAILED")
        selected = [r for r in rows if r.get("name") == "aiops-managed"]
        if len(selected) > 1:
            raise SetupRefused("SETUP_CONSUMER_PLUGIN_FAILED")
        return selected[0] if selected else None

    existing = managed()
    if existing:
        # 密钥回读是掩码；更新接口会用传入的真实凭据校验，不能拿掩码比较密钥。
        console._call(
            workspace_id,
            base + "/update",
            method="POST",
            body={
                "credential_id": existing["id"],
                "name": "aiops-managed",
                "credentials": credentials,
            },
        )
    else:
        console._call(
            workspace_id,
            base + "/add",
            method="POST",
            body={
                "name": "aiops-managed",
                "type": "api-key",
                "credentials": credentials,
                "visibility": "all_team_members",
            },
        )
    saved = managed()
    if not saved or (saved.get("credentials") or {}).get("gateway_url") != credentials["gateway_url"]:
        raise SetupRefused("SETUP_CONSUMER_PLUGIN_FAILED")
    console._call(workspace_id, base + "/default-credential", method="POST", body={"id": saved["id"]})
    if not managed().get("is_default"):
        raise SetupRefused("SETUP_CONSUMER_PLUGIN_FAILED")
    return PROVIDER


def consumer_models(console, workspace_id: str, document: dict, profile) -> list[str]:
    """确保源编排用到的每个模型在本工作区都有凭据，并回读确认。

    只装管家 profile 的那一个模型是不够的：现役客服编排可能用另一个 provider。
    平台只对**已批准**的 provider 提供凭据（profile 里的那个）；其余 provider 要求本工作区
    已经有可用凭据 —— 没有就拒绝，不复制源客户的密钥、也不假装成功。
    """
    nodes = ((document.get("workflow") or {}).get("graph") or {}).get("nodes") or []
    wanted = []
    for node in nodes:
        data = node.get("data") or {}
        if data.get("type") != "llm":
            continue
        model = data.get("model") or {}
        provider, name = str(model.get("provider") or ""), str(model.get("name") or "")
        if provider and name and (provider, name) not in wanted:
            wanted.append((provider, name))
    if not wanted:
        raise SetupRefused("SETUP_MODEL_CONNECTION_REQUIRED")
    approved_models = [profile.get("model") or {}, *profile.get("consumer_models", [])]
    ready = []
    for provider, name in wanted:
        approved = next(
            (m for m in approved_models if (m.get("provider"), m.get("name")) == (provider, name)), {}
        )
        if (provider, name) == (approved.get("provider"), approved.get("name")):
            model_credentials(console, workspace_id, approved)
            ready.append(f"{provider}/{name}")
            continue
        path = f"/console/api/workspaces/current/model-providers/{provider}/models/credentials"
        query = "?" + urlencode({"model": name, "model_type": "llm"})
        saved = console._call(workspace_id, path + query)
        if saved.get("current_credential_id"):
            ready.append(f"{provider}/{name}")
            continue
        raise SetupRefused("SETUP_MODEL_CONNECTION_REQUIRED")
    return ready
