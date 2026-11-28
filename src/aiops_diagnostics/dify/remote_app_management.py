"""单控制面（41）跨环境受管应用代理引擎（ADR-0372）。

设计原则：
1. 客户机 `/var/lib/aiops-access-<env>/gateway/gateway.db` 是远端环境运行路由与历史版本的唯一真相源；
   控制面不在本地再存一份会漂移的镜像路由。
2. Dify 管理员私钥、DSL 导出校验、模型/工具白名单编译、工作流发布均留在 41 控制面完成，
   绝不向客户机扩散 Dify 私钥；客户机仅经已钉扎 SSH 运行 `host_release_agent.py` 写入已冻结快照。
3. 客户机写入前降权至 `gateway.db` 真实属主 UID/GID，并按 `chat_route_bindings.revision` 做乐观锁校验，
   写入后客户机 Gateway 每次请求热读 SQLite，无需重启容器。
"""

from __future__ import annotations

import contextlib
import json
import tempfile
from pathlib import Path
from typing import Any

import yaml

from aiops_diagnostics.consumer import consumer_documents, consumer_publication
from aiops_diagnostics.consumer.knowledge_access import KnowledgeGrants, audience, require_owned
from aiops_diagnostics.dify import dual_entry, service_keys
from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify.dify_dsl_pull import (
    DifyDslClient,
    DifyDslSource,
    _declared_dataset_ids,
    workflow_content,
)
from aiops_diagnostics.dify.self_service_store import SetupConflict
from aiops_diagnostics.execution import node_publication
from aiops_diagnostics.knowledge.knowledge_retrieval import KbServiceClient, KnowledgeSearchUnavailable
from aiops_diagnostics.onboarding import host_enrollment

_AGENT_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "ops/environments/gateway/host_release_agent.py"


def _host_agent_source() -> str:
    return _AGENT_SCRIPT_PATH.read_text(encoding="utf-8")


def run_remote_app_command(
    context: Any,
    environment: str,
    subcmd: str,
    payload: dict[str, Any],
    *,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """经已钉扎客户机 SSH 执行 `host_release_agent.py <subcmd> <json>` 并返回解析后的结果。"""
    runner = getattr(context, "remote_app_runner", None)
    if callable(runner):
        result = runner(environment, subcmd, payload)
        if not isinstance(result, dict):
            raise auth.SetupRefused("SETUP_UNAVAILABLE")
        return result

    reg_path = auth.management_registry_path(context)
    if not reg_path:
        raise auth.SetupRefused("ENVIRONMENT_MANAGEMENT_UNAVAILABLE")
    try:
        conn_info = host_enrollment.load_enrolled_host_connection(reg_path, environment)
        proc = host_enrollment.run_pinned_ssh(
            conn_info,
            ["python3", "-", subcmd, json.dumps(payload, ensure_ascii=False)],
            stdin_text=_host_agent_source(),
            timeout=timeout,
        )
    except host_enrollment.EnrollmentRefused as exc:
        raise auth.SetupRefused(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise auth.SetupRefused("ENVIRONMENT_HOST_UNREACHABLE") from exc

    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0 or not stdout:
        raise auth.SetupRefused("SETUP_UNAVAILABLE")
    try:
        parsed = json.loads(stdout.splitlines()[-1])
    except ValueError as exc:
        raise auth.SetupRefused("SETUP_UNAVAILABLE") from exc
    if not isinstance(parsed, dict):
        raise auth.SetupRefused("SETUP_UNAVAILABLE")
    return parsed


def _check_remote_outcome(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("conflict"):
        raise SetupConflict(str(result["conflict"]))
    if result.get("refused"):
        raise auth.SetupRefused(str(result["refused"]))
    if not result.get("ok"):
        raise auth.SetupRefused("SETUP_UNAVAILABLE")
    return result


def _list_remote_kb_rows(context: Any, tenant_id: str, environment: str) -> list[dict[str, Any]]:
    client = getattr(getattr(context, "runtime", None), "kb_search_client", None)
    if client is None:
        base_url = str(getattr(context.settings, "kb_service_base_url", "") or "").strip()
        token = str(getattr(context.settings, "kb_service_api_key", "") or "").strip()
        if not base_url:
            raise auth.SetupRefused("SETUP_KNOWLEDGE_FAILED")
        scoped = KbServiceClient(base_url, tenant_id=tenant_id, environment=environment, service_token=token)
    elif hasattr(client, "base_url"):
        scoped = KbServiceClient(
            client.base_url,
            tenant_id=tenant_id,
            environment=environment,
            service_token=getattr(client, "service_token", ""),
            timeout=getattr(client, "timeout", 15.0),
        )
    else:
        scoped = client.for_tenant(tenant_id)
    try:
        return scoped.list_knowledge_bases()
    except KnowledgeSearchUnavailable as exc:
        raise auth.SetupRefused("SETUP_KNOWLEDGE_FAILED") from exc


def _remote_owned_libraries(
    context: Any,
    tenant_id: str,
    environment: str,
    granted_ids: set[str],
) -> list[dict[str, Any]]:
    rows = _list_remote_kb_rows(context, tenant_id, environment)
    return audience(rows, granted_ids)


def remote_status(service: Any, caller: Any, row: dict[str, Any]) -> dict[str, Any]:
    env = str(row["environment"])
    tenant_id = str(row["tenant_id"])
    app_id = str(row["app_id"])
    entry = str(row["entry"])
    env_name, tenant_name = auth.environment_display(service.context, env, tenant_id)
    state = _check_remote_outcome(
        run_remote_app_command(
            service.context,
            env,
            "app-state",
            {"environment": env, "tenant_id": tenant_id, "app_id": app_id, "entry": entry},
        )
    )
    route = state.get("route") or {}
    snapshot = state.get("snapshot") or {}
    published = bool(route and route.get("state") == "active" and route.get("app_id") == app_id)
    version_no = int(route["agent_version_no"]) if route and route.get("app_id") == app_id else None
    route_revision = int(route["revision"]) if route else 0
    channel_state = "connected" if state.get("channel_observed") else "pending"
    common = {
        "managed": True,
        "entry": entry,
        "app_id": app_id,
        "platform": caller.platform,
        "published": published,
        "version_no": version_no,
        "route_revision": route_revision,
        "environment": env,
        "environment_name": env_name,
        "tenant_name": tenant_name,
        "remote_managed": True,
    }

    if entry == "operator":
        template = state.get("operator_manifest_template") or {}
        published_prompt = ""
        published_tools: list[str] = []
        manifest_text = snapshot.get("node_manifest") if isinstance(snapshot, dict) else None
        manifest_doc: dict[str, Any] = {}
        if isinstance(manifest_text, str) and manifest_text:
            try:
                manifest_doc = json.loads(manifest_text)
                sys_msgs = manifest_doc.get("system_messages") or []
                if sys_msgs:
                    published_prompt = node_publication.business_prompt(str(sys_msgs[0]))
                published_tools = [str(t["name"]) for t in (manifest_doc.get("tools") or []) if "name" in t]
            except (ValueError, TypeError):
                manifest_doc = {}
        model_info = (
            (template.get("model") or manifest_doc.get("model") or {}) if isinstance(template, dict) else {}
        )
        model_options = (
            template.get("model_options") or manifest_doc.get("model_options") or {}
            if isinstance(template, dict)
            else {}
        )
        return {
            **common,
            "published_prompt": published_prompt,
            "published_tools": published_tools,
            "model": str(model_info.get("model_key") or ""),
            "model_options": model_options,
            "profile": f"operator-{env}",
            "channel": channel_state,
        }

    # consumer
    targets = json.loads(row.get("targets_json") or "{}")
    if isinstance(state.get("app"), dict) and state["app"].get("targets_json"):
        with contextlib.suppress(ValueError):
            targets = json.loads(state["app"]["targets_json"])
    model = ""
    draft_sha256 = ""
    knowledge_base_ids: list[str] = []
    try:
        doc = service.console._call(row["workspace_id"], "/console/api/apps/" + app_id)
        if isinstance(doc, dict):
            model = str((doc.get("model_config") or {}).get("model", {}).get("name") or "")
    except Exception:  # noqa: BLE001
        model = ""
    try:
        from aiops_diagnostics.dify import self_service_prepare as prepare

        source = DifyDslSource(
            service.settings.dify_console_base_url,
            app_id,
            service.settings.dify_console_api_key,
            row["workspace_id"],
        )
        workflow = workflow_content(DifyDslClient(source).export_dsl())
        draft_sha256 = consumer_publication.workflow_of(workflow)
        knowledge_base_ids = list(prepare.consumer_source_ids({"workflow": workflow}))
    except Exception:  # noqa: BLE001
        draft_sha256 = ""
    channel_obj = {
        "state": channel_state,
        "note": "" if channel_state == "connected" else "已发布 · 尚未观察到线上流量（可随时对话验证）",
    }
    return {
        **common,
        "published_prompt": "",
        "published_tools": [],
        "model": model,
        "model_options": {},
        "profile": "",
        "knowledge_base_ids": knowledge_base_ids,
        "live_app_id": route.get("app_id") if route and route.get("state") == "active" else None,
        "live_version_no": (
            int(route["agent_version_no"]) if route and route.get("state") == "active" else None
        ),
        "live_order_status": bool(targets.get("live_order_status", True)),
        "live_billing_record": bool(targets.get("live_billing_record", True)),
        "channel": channel_obj["state"],
        "channel_note": channel_obj["note"],
        "draft_sha256": draft_sha256,
        "readiness": dual_entry.readiness(
            {"app_id": app_id, "published": published, "state": ""}, channel_obj
        ),
    }


def remote_publish(
    service: Any,
    caller: Any,
    row: dict[str, Any],
    revision: int,
    expected_draft: str | None = None,
) -> dict[str, Any]:
    from aiops_diagnostics.dify.self_service import draft_fingerprint

    env = str(row["environment"])
    tenant_id = str(row["tenant_id"])
    app_id = str(row["app_id"])
    workspace_id = str(row["workspace_id"])
    entry = str(row["entry"])

    state = _check_remote_outcome(
        run_remote_app_command(
            service.context,
            env,
            "app-state",
            {"environment": env, "tenant_id": tenant_id, "app_id": app_id, "entry": entry},
        )
    )
    route = state.get("route") or {}
    current_rev = int(route["revision"]) if route else 0
    if current_rev != revision:
        raise SetupConflict("SETUP_ROUTE_CHANGED")

    source = DifyDslSource(
        service.settings.dify_console_base_url,
        app_id,
        service.settings.dify_console_api_key,
        workspace_id,
    )

    if entry == "operator":
        doc = service.console._call(workspace_id, "/console/api/apps/" + app_id)
        mc = doc["model_config"]
        if _declared_dataset_ids(mc):
            raise auth.SetupRefused("SETUP_KNOWLEDGE_NOT_SUPPORTED")
        if expected_draft is not None and expected_draft != draft_fingerprint(mc):
            raise SetupConflict("SETUP_DRAFT_CHANGED")
        template = state.get("operator_manifest_template")
        if not isinstance(template, dict) or not template:
            raise auth.SetupRefused("SETUP_EXECUTOR_UNAVAILABLE")

        with tempfile.TemporaryDirectory(prefix="aiops-remote-manifest-") as tmp:
            tpl_file = Path(tmp) / "operator-manifest.json"
            tpl_file.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
            target = node_publication.NodeTarget(
                app_id=app_id,
                tenant_id=tenant_id,
                environment=env,
                entry="operator",
                config_file=f"/etc/aiops-access-{env}/runtime/agent-runs.json",
                manifest_file=str(tpl_file),
            )
            try:
                manifest_text = node_publication.candidate(
                    source,
                    target,
                    operator_preview={"pre_prompt": mc["pre_prompt"], "tools": mc["agent_mode"]["tools"]},
                )
            except ValueError as exc:
                raise auth.SetupRefused(str(exc)) from exc

        model_key = str((template.get("model") or {}).get("model_key") or "deepseek-v4-1-flash-261017")
        agent_config = {
            "agent_type": "operations",
            "prompt": str(mc.get("pre_prompt") or ""),
            "knowledge_base_ids": [],
            "model": model_key,
            "output_contract": "diagnosis-v1",
            "opening_questions": [],
            "quick_commands": [],
            "opening_statement": "",
            "suggested_questions": [],
            "allowed_tools": [],
        }
        outcome = _check_remote_outcome(
            run_remote_app_command(
                service.context,
                env,
                "app-publish",
                {
                    "environment": env,
                    "tenant_id": tenant_id,
                    "app_id": app_id,
                    "workspace_id": workspace_id,
                    "entry": "operator",
                    "agent_name": row.get("agent_name") or f"operator-{env}",
                    "expected_revision": revision,
                    "actor": caller.user_id,
                    "snapshot": {"node_manifest": manifest_text},
                    "workflow_id": "",
                    "agent_config": agent_config,
                    "targets_json": "{}",
                },
            )
        )
        service.context.store.record_publish_audit(
            app_id=app_id,
            action="published",
            reason_code="",
            tenant_id=tenant_id,
            business_entry="operator",
            agent_name=row.get("agent_name") or f"operator-{env}",
            company_user_id=caller.user_id,
            company_username="",
            dify_account_id="",
            version_no=int(outcome["version_no"]),
        )
        return {
            "version_no": int(outcome["version_no"]),
            "action": str(outcome.get("action") or "published"),
            "app_id": app_id,
            "revision": int(outcome["revision"]),
        }

    # consumer publish
    from aiops_diagnostics.consumer.agent_contract import ConsumerFlowError
    from aiops_diagnostics.dify import self_service_prepare as prepare

    document = yaml.safe_load(DifyDslClient(source).export_dsl())
    workflow = workflow_content(yaml.safe_dump(document, allow_unicode=True))
    if not consumer_documents.enabled(workflow):
        raise auth.SetupRefused("SETUP_CONSUMER_CONTENT_REQUIRED")
    if expected_draft is not None and expected_draft != consumer_publication.workflow_of(workflow):
        raise SetupConflict("SETUP_DRAFT_CHANGED")

    selected_kb_ids = list(prepare.consumer_source_ids({"workflow": workflow}))
    granted_ids = set(state.get("granted_knowledge_base_ids") or [])
    owned = _remote_owned_libraries(service.context, tenant_id, env, granted_ids)
    try:
        require_owned(selected_kb_ids, owned)
    except ValueError as exc:
        service.context.store.record_publish_audit(
            app_id=app_id,
            action="refused",
            reason_code="CONSUMER_FLOW_INVALID",
            tenant_id=tenant_id,
            business_entry="consumer",
            agent_name=row["agent_name"],
            company_user_id=caller.user_id,
        )
        raise ConsumerFlowError(str(exc) + "；请核对当前应用草稿的选库，本次提示词与选库未发布") from exc

    # 先在 36 Dify 发布工作流，再写客户机运行路由
    service.console._call(
        workspace_id, f"/console/api/apps/{app_id}/workflows/publish", method="POST", body={}
    )
    app_detail = service.console._call(workspace_id, f"/console/api/apps/{app_id}")
    workflow_id = str((app_detail.get("workflow") or {}).get("id") or "")
    if not workflow_id:
        draft_wf = service.console._call(workspace_id, f"/console/api/apps/{app_id}/workflows/draft")
        workflow_id = str((draft_wf or {}).get("id") or "")
    if not workflow_id:
        raise auth.SetupRefused("DIFY_ROUTE_NOT_ADVANCED")

    key_content = ""
    if service.settings.chat_dify_key_dir:
        key_ref = service_keys.ensure(
            service.console,
            service.settings.chat_dify_key_dir,
            workspace_id,
            app_id,
            database=service.settings.database_file,
        )
        key_path = Path(key_ref)
        if key_path.is_file():
            key_content = key_path.read_text(encoding="utf-8").strip()

    flags = consumer_publication.flags_of(workflow)
    targets = {
        "knowledge_base_ids": selected_kb_ids,
        "live_order_status": bool(flags.get("live_order_status", True)),
        "live_billing_record": bool(flags.get("live_billing_record", True)),
        "photo_enabled": bool(flags.get("photo_enabled", False)),
    }
    consumer_snapshot = {
        "consumer_service": {
            "callback_version": 6,
            "photo_enabled": targets["photo_enabled"],
            "content": {
                "schema": consumer_documents.SCHEMA,
                "tenant_id": tenant_id,
                "environment": env,
                "state": "published",
                "mode": "documents",
            },
            "knowledge_base_ids": selected_kb_ids,
            "live_order_status": targets["live_order_status"],
            "live_billing_record": targets["live_billing_record"],
        }
    }
    con_model_name = (
        str((app_detail.get("model_config") or {}).get("model", {}).get("name") or "")
        or "deepseek-v4-1-flash-261017"
    )
    agent_config = {
        "agent_type": "customer",
        "prompt": "",
        "knowledge_base_ids": selected_kb_ids,
        "model": con_model_name,
        "output_contract": "blocks-v1",
        "opening_questions": [],
        "quick_commands": [],
        "opening_statement": "",
        "suggested_questions": [],
        "allowed_tools": [],
    }
    outcome = _check_remote_outcome(
        run_remote_app_command(
            service.context,
            env,
            "app-publish",
            {
                "environment": env,
                "tenant_id": tenant_id,
                "app_id": app_id,
                "workspace_id": workspace_id,
                "entry": "consumer",
                "agent_name": row.get("agent_name") or f"consumer-{app_id}",
                "expected_revision": revision,
                "actor": caller.user_id,
                "snapshot": consumer_snapshot,
                "workflow_id": workflow_id,
                "agent_config": agent_config,
                "key_content": key_content,
                "targets_json": json.dumps(targets, ensure_ascii=False, sort_keys=True),
            },
        )
    )
    service.context.store.record_publish_audit(
        app_id=app_id,
        action="published",
        reason_code="",
        tenant_id=tenant_id,
        business_entry="consumer",
        agent_name=row["agent_name"],
        company_user_id=caller.user_id,
        company_username="",
        dify_account_id="",
        version_no=int(outcome["version_no"]),
    )
    return {
        "version_no": int(outcome["version_no"]),
        "action": str(outcome.get("action") or "published"),
        "app_id": app_id,
        "revision": int(outcome["revision"]),
    }


def remote_versions(service: Any, caller: Any, row: dict[str, Any]) -> list[int]:
    _ = caller
    env = str(row["environment"])
    outcome = _check_remote_outcome(
        run_remote_app_command(
            service.context,
            env,
            "app-versions",
            {
                "environment": env,
                "tenant_id": str(row["tenant_id"]),
                "app_id": str(row["app_id"]),
                "entry": str(row["entry"]),
                "agent_name": str(row.get("agent_name") or ""),
            },
        )
    )
    return [int(v) for v in (outcome.get("versions") or [])]


def remote_change_version(
    service: Any,
    caller: Any,
    row: dict[str, Any],
    revision: int,
    version: int | None = None,
) -> dict[str, Any]:
    env = str(row["environment"])
    tenant_id = str(row["tenant_id"])
    app_id = str(row["app_id"])
    entry = str(row["entry"])
    outcome = _check_remote_outcome(
        run_remote_app_command(
            service.context,
            env,
            "app-change-version",
            {
                "environment": env,
                "tenant_id": tenant_id,
                "app_id": app_id,
                "workspace_id": str(row["workspace_id"]),
                "entry": entry,
                "expected_revision": revision,
                "version_no": version,
                "actor": caller.user_id,
            },
        )
    )
    service.context.store.record_publish_audit(
        app_id=app_id,
        action=str(outcome.get("action") or ("retracted" if version is None else "restored")),
        tenant_id=tenant_id,
        business_entry=entry,
        agent_name=str(row["agent_name"]),
        company_user_id=caller.user_id,
        version_no=int(outcome["version_no"]),
    )
    return {
        "app_id": app_id,
        "entry": entry,
        "revision": int(outcome["revision"]),
        "state": str(outcome["state"]),
        "version_no": int(outcome["version_no"]),
    }


def remote_knowledge(service: Any, caller: Any, row: dict[str, Any]) -> dict[str, Any]:
    _ = caller
    env = str(row["environment"])
    tenant_id = str(row["tenant_id"])
    app_id = str(row["app_id"])
    state = _check_remote_outcome(
        run_remote_app_command(
            service.context,
            env,
            "app-state",
            {"environment": env, "tenant_id": tenant_id, "app_id": app_id, "entry": "consumer"},
        )
    )
    granted_ids = set(state.get("granted_knowledge_base_ids") or [])
    libraries = _remote_owned_libraries(service.context, tenant_id, env, granted_ids)
    return {
        "libraries": [
            {
                "id": r["knowledge_base_id"],
                "name": r.get("name") or r["knowledge_base_id"],
                "allowed": r["audience"] == "customer",
            }
            for r in libraries
        ]
    }


def remote_set_knowledge_access(
    service: Any,
    caller: Any,
    row: dict[str, Any],
    knowledge_base_id: str,
    allowed: bool,
) -> dict[str, Any]:
    env = str(row["environment"])
    tenant_id = str(row["tenant_id"])
    libraries = remote_knowledge(service, caller, row)
    if knowledge_base_id not in {r["id"] for r in libraries["libraries"]}:
        raise auth.SetupRefused("SETUP_KNOWLEDGE_CROSS_TENANT")
    # 1. 先更新客户机 gateway.db（运行真相源）
    outcome = _check_remote_outcome(
        run_remote_app_command(
            service.context,
            env,
            "app-knowledge-grant",
            {
                "environment": env,
                "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id,
                "allowed": allowed,
                "actor": caller.user_id,
            },
        )
    )
    # 2. 同步镜像到 41 控制面（供总览与知识委派读取）
    grants = KnowledgeGrants(service.settings.database_file)
    if allowed:
        grants.grant(tenant_id, env, knowledge_base_id, caller.user_id)
    else:
        grants.revoke(tenant_id, env, knowledge_base_id)
    return {"id": knowledge_base_id, "allowed": bool(outcome["allowed"])}
