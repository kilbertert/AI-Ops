"""工作区依赖和应用创建；外部副作用均可用持久操作ID对账。"""

import json
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlencode

import yaml

from aiops_diagnostics.operator.ops_provider import ensure_node_provider
from aiops_diagnostics.private_files import validate_private_file

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
