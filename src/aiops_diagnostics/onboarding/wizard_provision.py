"""控制面 V2.1 自助开通租户工作区、双端知识与运行制品包装配器（运行于 41 进程内，ADR-0358）。

职责边界：
1. 在 41 控制面进程内部复用既有 `DifyWorkspaces`、`KbAdminClient`、`DifyConsoleAdmin`、`service_keys`
   与 `registry` 原语，为新接入环境及租户准备幂等、隔离的 Dify/KB/双端应用资源；
2. 仅从 `wizard-templates/<env>.env` 与当前受管 `settings` 组合客户机 `template.env` 与
   `nginx-user-ingress-auth.conf`（`0600` 落盘 `/var/lib/aiops-41/onboarding-assets/wizard-<env>/`），
   严禁通过 HTTP 响应回传任何明文密码、API Key 或内部密钥；
3. 提供发布步的 `activate_environment_registry`：将环境与租户在 41 主目录切换为 `enabled=True`
   并生成目标端启用版 `environment.json`。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import shutil
import tarfile
import uuid
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values

from aiops_diagnostics.common.private_files import ensure_private_directory, write_private_text
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.consumer import agent_contract as flow
from aiops_diagnostics.consumer.knowledge_access import KnowledgeGrants, catalogue_token
from aiops_diagnostics.dify import (
    dify_ensure_workspace,
    dify_inner_api,
    knowledge_connection,
    self_service_prepare,
    service_keys,
)
from aiops_diagnostics.dify.dify_console_admin import ConsoleApiError, ConsoleApiSettings, DifyConsoleAdmin
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaceError, DifyWorkspaces
from aiops_diagnostics.dify.self_service_store import SelfServiceStore
from aiops_diagnostics.execution import execution_manifest
from aiops_diagnostics.execution.chat_route_bindings import ENGINE_DIFY, ENGINE_NODE, ChatRouteBindingStore
from aiops_diagnostics.gateway import gateway_schema
from aiops_diagnostics.knowledge import knowledge_delegation
from aiops_diagnostics.knowledge.kb_onboarding import (
    KbAdminClient,
    OnboardError,
    derive_email,
    generate_password,
)
from aiops_diagnostics.knowledge.tenant_scope import knowledge_tenant
from aiops_diagnostics.onboarding import (
    candidate_knowledge,
    consumer_prep,
    data_preflight,
    infra_orchestrator,
    operator_prep,
    registry,
    wizard_runtime,
)
from aiops_diagnostics.operator import operator_agent, operator_tools
from aiops_diagnostics.operator.ops_provider import ensure_node_provider
from aiops_diagnostics.publication.agent_lifecycle import AgentConfig, AgentStore


class WizardProvisionError(RuntimeError):
    """控制面一键装配或正式目录启用失败的稳定错误码异常。"""


def _ensure_workspace_record(db_path: Path, tenant_id: str, env: str, workspace_id: str, owner: str) -> None:
    """在指定 gateway.db 内幂等写入已创建的 Dify workspace 归属记录。"""
    ws_store = DifyWorkspaces(db_path)
    existing_ws = ""
    with suppress(DifyWorkspaceError):
        existing_ws = ws_store.resolve(tenant_id, env)
    if existing_ws != workspace_id:
        ws_store.claim(tenant_id, env, owner=owner, lease_seconds=60)
        ws_store.mark_created(tenant_id, env, workspace_id)


NODE_RUNTIME_ENV_ROOT = Path("/etc/aiops-node-runtime/environments")


def _wizard_dir(registry_path: str | Path, environment: str) -> Path:
    assets_root = Path(registry_path).resolve().parent.parent / "onboarding-assets"
    ensure_private_directory(assets_root)
    target = assets_root / f"wizard-{environment}"
    ensure_private_directory(target)
    return target


def _quote_env(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
    return f'"{escaped}"'


def _is_loopback_url(url: str) -> bool:
    host = urlsplit(url.strip()).hostname or ""
    return host in {"127.0.0.1", "localhost", "::1"}


def _read_existing_env_secret(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    vals = dotenv_values(path)
    return str(vals.get(key) or "").strip()


def _resolve_tenant(
    registry_path: str | Path,
    environment: str,
    requested_tenant_id: str,
    requested_tenant_name: str,
    upms_url: str,
) -> tuple[str, str]:
    tid = requested_tenant_id.strip()
    tname = requested_tenant_name.strip()
    if tid and tname:
        return tid, tname
    body = registry.load(registry_path)
    for row in body.get("environments", []):
        if row.get("id") == environment:
            for item in row.get("tenants") or []:
                if not tid or item.get("id") == tid:
                    return str(item.get("id") or tid), str(item.get("name") or tname or tid)
    if upms_url:
        probe = data_preflight._default_upms_tenants_probe(upms_url)
        tenants = list(probe.get("tenants") or [])
        if tid:
            for item in tenants:
                if str(item.get("id") or "") == tid:
                    return tid, str(item.get("name") or tname or tid)
            return tid, tname or tid
        if tenants:
            first = tenants[0]
            return str(first.get("id") or ""), str(first.get("name") or first.get("id") or "")
    if tid:
        return tid, tname or tid
    raise WizardProvisionError("ENVIRONMENT_WIZARD_TENANT_REQUIRED")


def _ensure_kb_pair(
    settings: Any,
    *,
    environment: str,
    tenant_id: str,
) -> tuple[str, str]:
    base_url = str(getattr(settings, "kb_service_base_url", "") or "").strip().rstrip("/")
    if not base_url:
        raise WizardProvisionError("ENVIRONMENT_KB_SERVICE_UNCONFIGURED")
    scoped = knowledge_tenant(tenant_id, environment)
    domain = str(getattr(settings, "ragflow_tenant_domain", "") or "aiops.local").strip() or "aiops.local"
    admin = KbAdminClient(base_url, environment=environment)
    headers = {"tenant-id": scoped}
    cur_code, _ = wizard_runtime._http_json("GET", f"{base_url}/kb/tenants/current", headers)
    if cur_code != 200:
        with suppress(OnboardError):
            admin.register_tenant(
                tenant_id=tenant_id,
                email=derive_email(scoped, domain=domain),
                password=generate_password(),
            )
        with suppress(OnboardError):
            admin.repair_models(tenant_id)
        recheck_code, _ = wizard_runtime._http_json("GET", f"{base_url}/kb/tenants/current", headers)
        if recheck_code != 200:
            raise WizardProvisionError("ENVIRONMENT_KB_TENANT_UNAVAILABLE")

    list_code, list_body = wizard_runtime._http_json("GET", f"{base_url}/kb/knowledge-bases", headers)
    if list_code != 200:
        raise WizardProvisionError("ENVIRONMENT_KB_LIST_FAILED")
    existing = wizard_runtime._extract_kbs_list(list_body)

    def _find_or_create(role: str) -> str:
        prefix = f"{role}-kb-"
        keyword = "对客" if role == "consumer" else "管家"
        for item in existing:
            name = str(item.get("name") or "")
            if name.startswith(prefix) or keyword in name:
                kb_id = str(item.get("id") or item.get("knowledge_base_id") or "")
                if kb_id:
                    return kb_id
        create_name = f"{prefix}{environment}"
        desc = (
            "客户对客客服知识库（自助开通自动创建）"
            if role == "consumer"
            else "管家运维诊断知识库（自助开通自动创建）"
        )
        c_code, c_body = wizard_runtime._http_json(
            "POST",
            f"{base_url}/kb/knowledge-bases",
            headers,
            {"name": create_name, "description": desc},
        )
        if c_code not in (200, 201):
            raise WizardProvisionError("ENVIRONMENT_KB_CREATE_FAILED")
        data_obj = (
            c_body.get("data")
            if isinstance(c_body, dict) and isinstance(c_body.get("data"), dict)
            else c_body
        )
        kb_id = str((data_obj or {}).get("id") or (data_obj or {}).get("knowledge_base_id") or "")
        if not kb_id:
            raise WizardProvisionError("ENVIRONMENT_KB_ID_UNRESOLVED")
        return kb_id

    return _find_or_create("consumer"), _find_or_create("operator")


def _select_consumer_source(settings: Any, context: Any) -> dict[str, Any]:
    """从控制面现役正式航道自动选择最新可用客户端 Chatflow 模板。"""
    route_file = getattr(settings, "chat_route_bindings_file", "")
    exec_env = str(getattr(settings, "execution_environment", "") or "41")
    db_file = str(getattr(settings, "database_file", "") or "")
    candidates: list[dict[str, Any]] = []
    if route_file and Path(route_file).is_file():
        for route in ChatRouteBindingStore(route_file).all():
            if (
                route.environment == exec_env
                and route.entry == "consumer"
                and route.scenario == "consumer_service"
                and route.active
                and route.workflow_id
            ):
                candidates.append(
                    {
                        "app_id": route.app_id,
                        "tenant_id": route.tenant_id,
                        "agent_id": route.agent_id,
                        "version_no": route.agent_version_no,
                    }
                )
    if not candidates and db_file and Path(db_file).is_file():
        for route in ChatRouteBindingStore(db_file).all():
            if (
                route.entry == "consumer"
                and route.scenario == "consumer_service"
                and route.active
                and route.workflow_id
            ):
                candidates.append(
                    {
                        "app_id": route.app_id,
                        "tenant_id": route.tenant_id,
                        "agent_id": route.agent_id,
                        "version_no": route.agent_version_no,
                    }
                )
    if not candidates or getattr(context, "agent_manager", None) is None:
        raise WizardProvisionError("ENVIRONMENT_CONSUMER_SOURCE_UNAVAILABLE")
    for item in candidates:
        try:
            snap = context.agent_manager.store.version(
                item["agent_id"], item["tenant_id"], item["version_no"]
            ).snapshot
            workflow = snap.get("workflow") or {}
            if flow.agent_enabled(workflow):
                return {
                    "app_id": item["app_id"],
                    "name": snap.get("name", item["app_id"]),
                    "tenant_id": item["tenant_id"],
                    "agent_id": item["agent_id"],
                    "version_no": item["version_no"],
                    "workflow": workflow,
                }
        except Exception:
            continue
    raise WizardProvisionError("ENVIRONMENT_CONSUMER_SOURCE_UNAVAILABLE")


def _build_environment_registry_doc(
    *,
    environment: str,
    api_domain: str,
    tenant_id: str,
    enabled: bool,
) -> dict[str, Any]:
    """生成符合 `EnvironmentRegistry` 非托管（`aiops-environment-registry/1`）契约的环境清单。"""
    gw = f"https://{api_domain}"
    return {
        "schema": "aiops-environment-registry/1",
        "revision": 1,
        "environments": [
            {
                "id": environment,
                "enabled": enabled,
                "gateway_url": gw,
                "cpms_base_url": f"{gw}/charging-pile",
                "config_ref": f"/etc/aiops-access-{environment}/production.env",
                "tenants": [{"id": tenant_id, "enabled": enabled}],
            }
        ],
        "audit": [{"revision": 1, "actor": "wizard-worker", "at": format_stored_time(now_utc())}],
    }


def _upsert_control_registry(
    registry_path: str | Path,
    *,
    environment: str,
    customer_name: str,
    api_domain: str,
    tenant_id: str,
    tenant_name: str,
    config_ref: str,
    enabled: bool,
    actor: str,
) -> dict[str, Any]:
    current = registry.load(registry_path)
    rev = int(current.get("revision") or 0)
    managers = list(current.get("managers") or [])
    envs = [dict(row) for row in (current.get("environments") or [])]
    gw = f"https://{api_domain}"
    new_tenant = {"id": tenant_id, "name": tenant_name or tenant_id, "enabled": enabled}
    matched = False
    for row in envs:
        if row.get("id") == environment:
            row["name"] = customer_name or row.get("name") or environment
            row["enabled"] = enabled
            row["gateway_url"] = gw
            row["cpms_base_url"] = f"{gw}/charging-pile"
            row["company_gateway_url"] = gw
            row["config_ref"] = str(row.get("config_ref") or config_ref)
            tenants = [dict(t) for t in (row.get("tenants") or []) if t.get("id") != tenant_id]
            tenants.append(new_tenant)
            row["tenants"] = tenants
            matched = True
            break
    if not matched:
        envs.append(
            {
                "id": environment,
                "name": customer_name or environment,
                "enabled": enabled,
                "gateway_url": gw,
                "cpms_base_url": f"{gw}/charging-pile",
                "company_gateway_url": gw,
                "config_ref": config_ref,
                "tenants": [new_tenant],
            }
        )
    return registry.apply(
        registry_path,
        envs,
        expected_revision=rev,
        actor=actor or "wizard-worker",
        managers=managers,
    )


def prepare_control_plane_bundle(
    context: Any,
    registry_path: str | Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """在 41 进程内幂等装配 Dify 工作区、双知识库、双应用、keys 与客户机运行制品包。"""
    settings = context.settings
    env = infra_orchestrator._validate_environment(str(payload.get("environment") or ""))
    api_domain = infra_orchestrator._validate_domain(str(payload.get("api_domain") or ""))
    customer_name = str(payload.get("customer_name") or env).strip() or env
    actor = str(payload.get("actor") or "wizard-worker").strip() or "wizard-worker"
    local_port = int(payload.get("local_port") or 8788)

    tpl_src = Path(registry_path).resolve().parent / "wizard-templates" / f"{env}.env"
    if not tpl_src.is_file():
        raise WizardProvisionError("ENVIRONMENT_WIZARD_TEMPLATE_MISSING")
    tpl_vals = {k: str(v) for k, v in dotenv_values(tpl_src).items() if v is not None}

    upms_url = f"https://{api_domain}"
    tenant_id, tenant_name = _resolve_tenant(
        registry_path,
        env,
        str(payload.get("tenant_id") or ""),
        str(payload.get("tenant_name") or ""),
        upms_url,
    )

    wdir = _wizard_dir(registry_path, env)
    prev_tpl = wdir / "template.env"
    company_source_key = _read_existing_env_secret(
        prev_tpl, "AIOPS_GATEWAY_COMPANY_SOURCE_KEY"
    ) or secrets.token_hex(24)
    service_token = _read_existing_env_secret(
        prev_tpl, "AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN"
    ) or secrets.token_hex(24)
    env_task_key = str(getattr(settings, "environment_task_key", "") or "").strip() or secrets.token_hex(24)
    internal_secret = str(getattr(settings, "internal_token_secret", "") or "").strip() or secrets.token_hex(
        32
    )
    media_secret = _read_existing_env_secret(
        prev_tpl, "AIOPS_GATEWAY_MEDIA_SIGNING_SECRET"
    ) or secrets.token_hex(24)

    runtime_dir_target = f"/etc/aiops-access-{env}/runtime"
    key_dir_target = f"/var/lib/aiops-access-{env}/keys"
    registry_target = f"/etc/aiops-access-{env}/registry/environment.json"

    raw_dify_chat_tpl = tpl_vals.get("AIOPS_GATEWAY_DIFY_CHAT_BASE_URL", "").strip()
    raw_dify_chat_settings = str(getattr(settings, "dify_chat_base_url", "") or "").strip()
    if raw_dify_chat_tpl and not _is_loopback_url(raw_dify_chat_tpl):
        remote_dify_chat_url = raw_dify_chat_tpl
    elif raw_dify_chat_settings and not _is_loopback_url(raw_dify_chat_settings):
        remote_dify_chat_url = raw_dify_chat_settings
    else:
        remote_dify_chat_url = (
            os.environ.get("AIOPS_WIZARD_PUBLIC_DIFY_CHAT_BASE_URL", "").strip()
            or "https://dify.ranlei.work:10443"
        )
    raw_kb_url = str(getattr(settings, "kb_service_base_url", "") or "").strip()
    remote_kb_url = "" if (not raw_kb_url or _is_loopback_url(raw_kb_url)) else raw_kb_url

    env_lines: dict[str, str] = {
        **tpl_vals,
        "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": env,
        "AIOPS_GATEWAY_BIND": "127.0.0.1",
        "AIOPS_GATEWAY_PORT": str(local_port),
        "AIOPS_GATEWAY_PUBLIC_BASE_URL": f"https://{api_domain}",
        "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": f"https://{api_domain}/charging-pile",
        "AIOPS_UPMS_BASE_URL": f"https://{api_domain}/upms",
        "AIOPS_GATEWAY_DIFY_CHAT_BASE_URL": remote_dify_chat_url,
        "AIOPS_GATEWAY_KB_SERVICE_BASE_URL": remote_kb_url,
        "AIOPS_GATEWAY_ENVIRONMENT_TASK_KEY": env_task_key,
        "AIOPS_GATEWAY_INTERNAL_TOKEN_SECRET": internal_secret,
        "AIOPS_GATEWAY_MEDIA_SIGNING_SECRET": media_secret,
        "AIOPS_GATEWAY_COMPANY_JWT_KEY": str(getattr(settings, "company_jwt_key", "") or "").strip(),
        "AIOPS_GATEWAY_COMPANY_SOURCE_KEY": company_source_key,
        "AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED": "true",
        "AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED": "true",
        "AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX": (
            tpl_vals.get("AIOPS_GATEWAY_THIRD_SESSION_KEY_PREFIX")
            or str(getattr(settings, "third_session_key_prefix", "") or "")
            or "app:3rd_session:"
        ),
        "AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN": service_token,
        "AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE": registry_target,
        "AIOPS_GATEWAY_OPERATOR_PROFILES_FILE": f"{runtime_dir_target}/operator-profiles.json",
        "AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE": f"{runtime_dir_target}/consumer-targets.json",
        "AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE": (
            "" if remote_kb_url else f"{runtime_dir_target}/knowledge-delegation.json"
        ),
        "AIOPS_MYSQL_DATABASE": tpl_vals.get("AIOPS_MYSQL_DATABASE") or "cloud_charging_pile",
        "AIOPS_MAX_ORDER_WINDOW_HOURS": tpl_vals.get("AIOPS_MAX_ORDER_WINDOW_HOURS", "168"),
        "AIOPS_QUERY_TIMEOUT_SECONDS": tpl_vals.get("AIOPS_QUERY_TIMEOUT_SECONDS", "15"),
        "AIOPS_SSH_ENABLED": "false",
    }
    for installer_owned in (
        "AIOPS_GATEWAY_ENVIRONMENT",
        "AIOPS_GATEWAY_DATABASE_FILE",
        "AIOPS_GATEWAY_SERVER_CONFIG_FILE",
        "AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE",
        "AIOPS_GATEWAY_CHAT_DIFY_KEY_DIR",
    ):
        env_lines.pop(installer_owned, None)
    tpl_text = "\n".join(f"{k}={_quote_env(v)}" for k, v in sorted(env_lines.items()) if v != "") + "\n"
    write_private_text(prev_tpl, tpl_text)
    config_sha = hashlib.sha256(prev_tpl.read_bytes()).hexdigest()

    runtime_ptr_keys = {
        "AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE",
        "AIOPS_GATEWAY_OPERATOR_PROFILES_FILE",
        "AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE",
        "AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE",
    }
    bootstrap_lines = {k: v for k, v in env_lines.items() if k not in runtime_ptr_keys}
    bootstrap_text = (
        "\n".join(f"{k}={_quote_env(v)}" for k, v in sorted(bootstrap_lines.items()) if v != "") + "\n"
    )
    bootstrap_file = wdir / "bootstrap.env"
    write_private_text(bootstrap_file, bootstrap_text)
    bootstrap_sha = hashlib.sha256(bootstrap_file.read_bytes()).hexdigest()

    assets_root = Path(registry_path).resolve().parent.parent / "onboarding-assets"
    cfg_dir = assets_root / f"configuration-{config_sha}"
    ensure_private_directory(cfg_dir)
    cfg_file = cfg_dir / "template.env"
    write_private_text(cfg_file, tpl_text)

    auth_conf = wdir / "nginx-user-ingress-auth.conf"
    write_private_text(
        auth_conf,
        f'set $aiops_user_service_authorization "Bearer {service_token}";\n'
        f'set $aiops_user_operator_source_key "{company_source_key}";\n',
    )
    nginx_auth_sha = hashlib.sha256(auth_conf.read_bytes()).hexdigest()

    # 1. kb-service 租户 + 双知识库
    consumer_kb_id, operator_kb_id = _ensure_kb_pair(settings, environment=env, tenant_id=tenant_id)

    # 2. Dify Workspace + 知识连接 + 双端应用 + Service API Key
    dify_db = str(getattr(settings, "database_file", "") or "")
    if not dify_db:
        raise WizardProvisionError("ENVIRONMENT_WIZARD_DIFY_DB_REQUIRED")
    dify_db_path = Path(dify_db)
    ws_outcome = dify_ensure_workspace.ensure_workspace(
        tenant_id=tenant_id,
        environment=env,
        workspaces=DifyWorkspaces(dify_db_path),
        inner=dify_inner_api.DifyInnerApi(
            dify_inner_api.InnerApiSettings(settings.dify_inner_api_base_url, settings.dify_inner_api_key)
        ),
        console_base_url=settings.dify_console_base_url,
        console_api_key=settings.dify_console_api_key,
        company=tenant_name,
    )
    workspace_id = ws_outcome.workspace_id
    console = DifyConsoleAdmin(
        ConsoleApiSettings(settings.dify_console_base_url, settings.dify_console_api_key)
    )

    scope_con = (env, tenant_id, workspace_id, consumer_kb_id)
    existing_conns = console._call(workspace_id, knowledge_connection.PATH)
    conn_name = (
        f"aiops-kb-{hashlib.sha256(candidate_knowledge.knowledge_id(scope_con).encode()).hexdigest()[:31]}"
    )
    conn_rows = existing_conns.get("data") if isinstance(existing_conns, dict) else existing_conns
    conn_id = ""
    if isinstance(conn_rows, list):
        for item in conn_rows:
            if isinstance(item, dict) and item.get("name") == conn_name:
                conn_id = str(item.get("id") or "")
                break
    pending_conn: dict[str, Any] = {"connection_id": conn_id} if conn_id else {}
    conn_settings = SimpleNamespace(
        public_base_url=settings.public_base_url,
        environment=env,
        dify_external_knowledge_api_id=conn_id,
        dify_knowledge_api_key=candidate_knowledge.token(internal_secret, scope_con),
    )
    knowledge_connection.ensure(
        console, workspace_id, conn_settings, pending=pending_conn, save=lambda: None, name=conn_name
    )

    op_model = operator_prep.model(settings)
    self_service_prepare.model_credentials(console, workspace_id, op_model)
    ensure_node_provider(console, workspace_id, f"https://{api_domain}", reconcile=True)
    providers = console._call(workspace_id, "/console/api/workspaces/current/tool-providers?type=api")
    provider_id = next(p["id"] for p in providers if p.get("name") == "cpms-node")

    apps_resp = console._call(workspace_id, "/console/api/apps?page=1&limit=100")
    existing_apps = list(apps_resp.get("data") or []) if isinstance(apps_resp, dict) else []

    op_app_name = f"aiops-operator-{env}"
    op_app = next(
        (a for a in existing_apps if a.get("name") == op_app_name and a.get("mode") == "agent-chat"),
        None,
    )
    if op_app is None:
        op_req_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wiz-op:{env}:{tenant_id}"))
        op_app_id = self_service_prepare.create_app(
            console,
            workspace_id,
            op_req_id,
            {
                "name": op_app_name,
                "prompt": operator_agent.sop(),
                "tools": [t.name for t in operator_tools.tools()],
            },
            {"model": op_model},
            provider_id,
            candidate=False,
        )
    else:
        op_app_id = str(op_app["id"])

    from aiops_diagnostics.consumer.consumer_publication import flags_of

    consumer_source = _select_consumer_source(settings, context)
    con_doc = consumer_prep.document(consumer_source, consumer_kb_id)
    con_model = consumer_prep.profile(settings, consumer_source)["model"]
    con_flags = flags_of(consumer_source["workflow"])
    con_app_name = f"aiops-consumer-{env}"
    con_app = next(
        (a for a in existing_apps if a.get("name") == con_app_name and a.get("mode") == "advanced-chat"),
        None,
    )
    con_req_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wiz-con:{env}:{tenant_id}"))
    con_app_id = (
        str(con_app["id"])
        if con_app is not None
        else self_service_prepare.consumer_app_id(workspace_id, con_req_id)
    )
    consumer_target_entry = {
        "environment": env,
        "tenant_id": tenant_id,
        "app_id": con_app_id,
        "knowledge_base_ids": [consumer_kb_id],
        **con_flags,
    }
    _ensure_workspace_record(dify_db_path, tenant_id, env, workspace_id, actor)
    KnowledgeGrants(dify_db_path).grant(tenant_id, env, consumer_kb_id, actor)
    SelfServiceStore(dify_db_path).register(
        app_id=con_app_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        agent_name=f"consumer-{con_app_id}",
        profile_id="",
        actor=actor,
        entry="consumer",
        environment=env,
        targets={"knowledge_base_ids": [consumer_kb_id], **con_flags},
    )
    delegated = knowledge_delegation.issue(settings, env, tenant_id, con_app_id, consumer_kb_id)
    self_service_prepare.consumer_models(console, workspace_id, con_doc, {"model": con_model})
    console.install_plugins(workspace_id, self_service_prepare.consumer_plugins(con_doc))
    self_service_prepare.consumer_plugin(
        console,
        workspace_id,
        SimpleNamespace(public_base_url=settings.public_base_url),
        delegated["token"],
        con_doc,
    )
    if con_app is None:
        con_app_id = self_service_prepare.copy_consumer_app(
            console,
            consumer_source,
            workspace_id,
            con_req_id,
            {"name": con_app_name},
            con_doc,
            candidate=False,
        )
    with suppress(ConsoleApiError):
        console._call(
            workspace_id, f"/console/api/apps/{con_app_id}/workflows/publish", method="POST", body={}
        )
    for app_id in (op_app_id, con_app_id):
        app_info = console._call(workspace_id, f"/console/api/apps/{app_id}")
        if isinstance(app_info, dict) and app_info.get("enable_api") is not True:
            console._call(
                workspace_id,
                f"/console/api/apps/{app_id}/api-enable",
                method="POST",
                body={"enable_api": True},
            )
    con_app_detail = console._call(workspace_id, f"/console/api/apps/{con_app_id}")
    con_workflow_id = str((con_app_detail.get("workflow") or {}).get("id") or "")
    if not con_workflow_id:
        draft_wf = console._call(workspace_id, f"/console/api/apps/{con_app_id}/workflows/draft")
        con_workflow_id = str((draft_wf or {}).get("id") or "")

    # 3. Dify Service API Keys 落盘（控制面密钥目录 + 本次打包 staging 目录）
    staging_keys = wdir / "dify-keys"
    ensure_private_directory(staging_keys)
    ctrl_key_dir = Path(str(getattr(settings, "chat_dify_key_dir", "") or staging_keys))
    ensure_private_directory(ctrl_key_dir)

    op_key_file = Path(
        service_keys.ensure(console, ctrl_key_dir, workspace_id, op_app_id, database=dify_db_path)
    )
    con_key_file = Path(
        service_keys.ensure(console, ctrl_key_dir, workspace_id, con_app_id, database=dify_db_path)
    )
    for app_id, src_key in ((op_app_id, op_key_file), (con_app_id, con_key_file)):
        dst_key = staging_keys / f"{app_id}.key"
        if src_key.resolve() != dst_key.resolve():
            shutil.copyfile(src_key, dst_key)
        dst_key.chmod(0o600)

    # 4. 读取 41 节点 agent-runs.json 与 exec-manifest.json，并合成客户机 runtime-bundle.tar
    node_dir = NODE_RUNTIME_ENV_ROOT / env
    agent_runs_file = node_dir / "agent-runs.json"
    manifest_file = node_dir / "exec-manifest.json"
    if not agent_runs_file.is_file() or not manifest_file.is_file():
        raise WizardProvisionError("ENVIRONMENT_NODE_RUNTIME_CONFIG_MISSING")
    agent_runs_data = json.loads(agent_runs_file.read_text(encoding="utf-8"))
    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    execution_manifest.from_text(json.dumps(manifest_data))

    node_model = dict(manifest_data["model"])
    node_model_options = dict(manifest_data.get("model_options") or {})
    op_tool_defs = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema()}
        for t in operator_tools.tools()
    ]
    operator_profile_manifest = {
        "schema": execution_manifest.OPERATOR_SCHEMA,
        "name": "operator-cpms-readonly",
        "history_policy": execution_manifest.FIXED_HISTORY_POLICY,
        "model": node_model,
        "model_options": node_model_options,
        "system_messages": [operator_agent.sop()],
        "tools": op_tool_defs,
    }
    operator_agent_manifest = {
        "schema": execution_manifest.AGENT_SCHEMA,
        "name": "operator-cpms-readonly",
        "history_policy": execution_manifest.AGENT_HISTORY,
        "model": node_model,
        "model_options": node_model_options,
        "system_messages": [operator_agent.sop() + "\n" + operator_agent.INSTRUCTIONS],
        "tools": op_tool_defs,
    }
    operator_manifest_text = json.dumps(
        operator_agent_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    execution_manifest.from_text(operator_manifest_text)

    op_cred = json.loads(Path(op_model["credential_file"]).read_text(encoding="utf-8"))
    con_cred = json.loads(Path(con_model["credential_file"]).read_text(encoding="utf-8"))

    delegation_doc = {
        "schema": "aiops-knowledge-delegation/1",
        "base_url": str(settings.public_base_url).rstrip("/"),
        "capabilities": [
            {
                "knowledge_id": delegated["knowledge_id"],
                "token": delegated["token"],
            }
        ],
    }
    op_profile_doc = {
        "schema": "operator-profiles/1",
        "profiles": [
            {
                "id": f"operator-{env}",
                "model": {**op_model, "credential_file": f"{runtime_dir_target}/operator-model.json"},
                "consumer_models": (
                    [{**con_model, "credential_file": f"{runtime_dir_target}/consumer-model.json"}]
                    if con_model != op_model
                    else []
                ),
                "target": {
                    "environment": env,
                    "entry": "operator",
                    "config_file": f"{runtime_dir_target}/agent-runs.json",
                    "manifest_file": f"{runtime_dir_target}/operator-manifest.json",
                    "cpms_base_url": f"https://{api_domain}/charging-pile",
                    "proxy_base_url": "https://gateway-egress:9443",
                },
            }
        ],
    }
    consumer_targets_doc = {"schema": "aiops-consumer-targets/1", "targets": [consumer_target_entry]}
    registrations_doc = {
        "environment": env,
        "gateway_url": f"https://{api_domain}",
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "operator_app_id": op_app_id,
        "consumer_app_id": con_app_id,
        "consumer_kb_id": consumer_kb_id,
        "operator_kb_id": operator_kb_id,
        "enabled": False,
    }
    env_registry_doc = _build_environment_registry_doc(
        environment=env,
        api_domain=api_domain,
        tenant_id=tenant_id,
        enabled=True,
    )
    seed_doc = {
        "environment": env,
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "operator_app_id": op_app_id,
        "consumer_app_id": con_app_id,
        "consumer_workflow_id": con_workflow_id,
        "consumer_kb_id": consumer_kb_id,
        "operator_kb_id": operator_kb_id,
        "consumer_flags": con_flags,
        "operator_manifest_text": operator_manifest_text,
        "agent_runs_path": f"{runtime_dir_target}/agent-runs.json",
        "key_dir": key_dir_target,
        "registrations": registrations_doc,
        "consumer_target": consumer_target_entry,
    }

    runtime_tar = wdir / "runtime-bundle.tar"
    files_to_pack: dict[str, bytes] = {
        "runtime/agent-runs.json": (json.dumps(agent_runs_data, sort_keys=True).encode() + b"\n"),
        "runtime/operator-manifest.json": (
            json.dumps(operator_profile_manifest, ensure_ascii=False, sort_keys=True).encode() + b"\n"
        ),
        "runtime/operator-profiles.json": (json.dumps(op_profile_doc, sort_keys=True).encode() + b"\n"),
        "runtime/consumer-targets.json": (json.dumps(consumer_targets_doc, sort_keys=True).encode() + b"\n"),
        "runtime/registrations.json": (json.dumps(registrations_doc, sort_keys=True).encode() + b"\n"),
        "runtime/operator-model.json": (json.dumps(op_cred, sort_keys=True).encode() + b"\n"),
        "runtime/consumer-model.json": (json.dumps(con_cred, sort_keys=True).encode() + b"\n"),
        "runtime/knowledge-delegation.json": (json.dumps(delegation_doc, sort_keys=True).encode() + b"\n"),
        "runtime/gateway-seed.json": (json.dumps(seed_doc, sort_keys=True).encode() + b"\n"),
        "registry/environment.json": (json.dumps(env_registry_doc, sort_keys=True).encode() + b"\n"),
        f"dify-keys/{op_app_id}.key": (staging_keys / f"{op_app_id}.key").read_bytes(),
        f"dify-keys/{con_app_id}.key": (staging_keys / f"{con_app_id}.key").read_bytes(),
    }
    with runtime_tar.open("wb") as raw_fh, tarfile.open(fileobj=raw_fh, mode="w") as tf:
        for arc_name, raw_bytes in sorted(files_to_pack.items()):
            info = tarfile.TarInfo(arc_name)
            info.mode = 0o600
            info.size = len(raw_bytes)
            tf.addfile(info, io.BytesIO(raw_bytes))
    runtime_tar.chmod(0o600)
    runtime_tar_sha = hashlib.sha256(runtime_tar.read_bytes()).hexdigest()

    # 5. 在 41 主目录同步停用态环境记录，供 Step 4 自验与 Step 5 正式发布平滑衔接
    _upsert_control_registry(
        registry_path,
        environment=env,
        customer_name=customer_name,
        api_domain=api_domain,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        config_ref=str(cfg_file),
        enabled=False,
        actor=actor,
    )

    return {
        "ok": True,
        "environment": env,
        "tenant_id": tenant_id,
        "tenant_name": tenant_name,
        "workspace_id": workspace_id,
        "consumer_kb_id": consumer_kb_id,
        "operator_kb_id": operator_kb_id,
        "consumer_app_id": con_app_id,
        "operator_app_id": op_app_id,
        "config_path": str(cfg_file),
        "config_sha256": config_sha,
        "bootstrap_config_path": str(bootstrap_file),
        "bootstrap_config_sha256": bootstrap_sha,
        "runtime_bundle_path": str(runtime_tar),
        "runtime_bundle_sha256": runtime_tar_sha,
        "nginx_auth_path": str(auth_conf),
        "nginx_auth_sha256": nginx_auth_sha,
    }


def seed_target_gateway_db(db_path: str | Path, seed_doc: dict[str, Any]) -> dict[str, Any]:
    """在目标客户机使用安装好的当前版 schema 初始化并写入双端正式路由绑定。"""
    db = Path(db_path).expanduser().resolve()
    ensure_private_directory(db.parent)
    gateway_schema.ensure(db)

    env = str(seed_doc["environment"])
    tenant_id = str(seed_doc["tenant_id"])
    workspace_id = str(seed_doc["workspace_id"])
    op_app_id = str(seed_doc["operator_app_id"])
    con_app_id = str(seed_doc["consumer_app_id"])
    con_workflow_id = str(seed_doc.get("consumer_workflow_id") or "")
    consumer_kb_id = str(seed_doc["consumer_kb_id"])
    con_flags = dict(seed_doc.get("consumer_flags") or {})
    agent_runs_path = str(seed_doc["agent_runs_path"])
    key_dir = str(seed_doc["key_dir"])

    _ensure_workspace_record(db, tenant_id, env, workspace_id, "wizard-worker")
    KnowledgeGrants(db).grant(tenant_id, env, consumer_kb_id, "wizard-worker")
    store = SelfServiceStore(db)
    store.register(
        app_id=op_app_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        agent_name=f"operator-{env}",
        profile_id=f"operator-{env}",
        actor="wizard-worker",
        entry="operator",
        environment=env,
    )
    store.register(
        app_id=con_app_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        agent_name=f"consumer-{con_app_id}",
        profile_id="",
        actor="wizard-worker",
        entry="consumer",
        environment=env,
        targets={"knowledge_base_ids": [consumer_kb_id], **con_flags},
    )

    agent_store = AgentStore(db)
    bindings = ChatRouteBindingStore(db)

    # 1. 管家端（Node 执行引擎）
    op_existing = bindings.get(env, tenant_id, "operator", "operator_order_diagnosis")
    if op_existing is None:
        op_agt = agent_store.create(
            tenant_id,
            f"operator-{env}",
            "V2.1 self-service onboarded operator agent",
            AgentConfig(
                agent_type="operations",
                prompt=operator_agent.sop(),
                knowledge_base_ids=(),
                model="deepseek-v4-1-flash-261017",
                output_contract="diagnosis-v1",
            ),
            "wizard-worker",
        )
        op_ver = agent_store.publish(
            op_agt.agent_id,
            tenant_id,
            1,
            {
                "app_id": op_app_id,
                "workflow_id": "",
                "node_manifest": str(seed_doc["operator_manifest_text"]),
            },
            "wizard-worker",
        )
        bindings.publish(
            environment=env,
            tenant_id=tenant_id,
            entry="operator",
            scenario="operator_order_diagnosis",
            engine=ENGINE_NODE,
            agent_id=op_agt.agent_id,
            agent_version_no=op_ver.version_no,
            app_id=op_app_id,
            node_config_file=agent_runs_path,
            expected_revision=0,
            updated_by="wizard-worker",
        )

    # 2. C 端（Dify Chatflow 执行引擎）
    con_existing = bindings.get(env, tenant_id, "consumer", "consumer_service")
    if con_existing is None:
        con_agt = agent_store.create(
            tenant_id,
            f"consumer-{env}",
            "V2.1 self-service onboarded consumer agent",
            AgentConfig(
                agent_type="customer",
                prompt="",
                knowledge_base_ids=(consumer_kb_id,),
                model="deepseek-v4-1-flash-261017",
                output_contract="blocks-v1",
            ),
            "wizard-worker",
        )
        con_ver = agent_store.publish(
            con_agt.agent_id,
            tenant_id,
            1,
            {
                "app_id": con_app_id,
                "workflow_id": con_workflow_id,
                "consumer_service": {
                    "callback_version": 6,
                    "photo_enabled": bool(con_flags.get("photo_enabled", False)),
                    "content": {
                        "tenant_id": tenant_id,
                        "environment": env,
                        "state": "published",
                        "mode": "documents",
                    },
                    "knowledge_base_ids": [consumer_kb_id],
                    "live_order_status": bool(con_flags.get("live_order_status", True)),
                    "live_billing_record": bool(con_flags.get("live_billing_record", True)),
                },
            },
            "wizard-worker",
        )
        bindings.publish(
            environment=env,
            tenant_id=tenant_id,
            entry="consumer",
            scenario="consumer_service",
            engine=ENGINE_DIFY,
            agent_id=con_agt.agent_id,
            agent_version_no=con_ver.version_no,
            app_id=con_app_id,
            workflow_id=con_workflow_id,
            workflow_key_ref=f"{key_dir}/{con_app_id}.key",
            expected_revision=0,
            updated_by="wizard-worker",
        )

    return {"ok": True, "environment": env, "tenant_id": tenant_id}


def activate_environment_registry(
    context: Any,
    registry_path: str | Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """正式发布 Step 5：启用 41 主目录及客户机 `environment.json`。"""
    env = infra_orchestrator._validate_environment(str(payload.get("environment") or ""))
    tenant_id = str(payload.get("tenant_id") or "").strip()
    if not tenant_id:
        raise WizardProvisionError("ENVIRONMENT_WIZARD_TENANT_REQUIRED")
    customer_name = str(payload.get("customer_name") or env).strip() or env
    tenant_name = str(payload.get("tenant_name") or tenant_id).strip() or tenant_id
    gateway_url = str(payload.get("gateway_url") or "").strip()
    api_domain = urlsplit(gateway_url).hostname or infra_orchestrator._validate_domain(
        str(payload.get("api_domain") or "")
    )
    actor = str(payload.get("actor") or "wizard-worker").strip() or "wizard-worker"
    wdir = _wizard_dir(registry_path, env)

    updated = _upsert_control_registry(
        registry_path,
        environment=env,
        customer_name=customer_name,
        api_domain=api_domain,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        config_ref=str(wdir / "template.env"),
        enabled=True,
        actor=actor,
    )
    active_env_doc = _build_environment_registry_doc(
        environment=env,
        api_domain=api_domain,
        tenant_id=tenant_id,
        enabled=True,
    )
    active_env_file = wdir / "active-environment.json"
    write_private_text(active_env_file, json.dumps(active_env_doc, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "ok": True,
        "environment": env,
        "tenant_id": tenant_id,
        "registry_revision": int(updated.get("revision") or 0),
        "environment_json_path": str(active_env_file),
        "environment_json_sha256": hashlib.sha256(active_env_file.read_bytes()).hexdigest(),
    }


def connect_consumer_plugin(
    context: Any,
    registry_path: str | Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """客户机 Nginx 入口就绪后，将 Dify consumer_knowledge 插件切回客户机公网域名 + ckc_ 凭据。"""
    _ = registry_path
    settings = context.settings
    env = infra_orchestrator._validate_environment(str(payload.get("environment") or ""))
    api_domain = infra_orchestrator._validate_domain(str(payload.get("api_domain") or ""))
    tenant_id = str(payload.get("tenant_id") or "").strip()
    workspace_id = str(payload.get("workspace_id") or "").strip()
    consumer_kb_id = str(payload.get("consumer_kb_id") or "").strip()
    if not tenant_id or not workspace_id or not consumer_kb_id:
        raise WizardProvisionError("ENVIRONMENT_WIZARD_INVALID")
    internal_secret = str(getattr(settings, "internal_token_secret", "") or "").strip()
    if not internal_secret:
        raise WizardProvisionError("ENVIRONMENT_WIZARD_SECRET_REQUIRED")

    console = DifyConsoleAdmin(
        ConsoleApiSettings(settings.dify_console_base_url, settings.dify_console_api_key)
    )
    consumer_source = _select_consumer_source(settings, context)
    con_doc = consumer_prep.document(consumer_source, consumer_kb_id)
    target_gateway = f"https://{api_domain}"
    token = catalogue_token(internal_secret, env, workspace_id)
    self_service_prepare.consumer_plugin(
        console,
        workspace_id,
        SimpleNamespace(public_base_url=target_gateway),
        token,
        con_doc,
    )
    return {"ok": True, "connected": True, "gateway_url": target_gateway}
