"""环境接入共享叶子上下文：路径、知识绑定摘要与真空初装入队，供各阶段单向依赖而不回指管理路由。"""

from __future__ import annotations

import hashlib
import json
import time

from aiops_diagnostics.onboarding import plans, tasks

COLD_ACTION = "site-install"


def _path(context):
    return getattr(context.settings, "environment_management_registry_file", "") or getattr(
        context.settings, "environment_registry_file", ""
    )


def _identity(context, caller):
    return (context.settings.environment, caller.tenant_id, caller.user_id)


def knowledge_binding(settings, *, target_only=False):
    return hashlib.sha256(
        json.dumps(
            {
                key: getattr(settings, key, "")
                for key in (
                    "kb_service_base_url",
                    "ragflow_credential_key",
                    "ragflow_embedding_base_url",
                    "ragflow_embedding_api_key",
                    "ragflow_embedding_model",
                    "ragflow_tenant_domain",
                    "dify_console_base_url",
                    "dify_console_api_key",
                )
                if not target_only
                or key in {"kb_service_base_url", "dify_console_base_url", "ragflow_tenant_domain"}
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def submit_cold(conn, environment, identity, parent_id, request):
    """真空目标的初装子任务；与安装面互斥，目标与制品引用随父冻结。"""
    task_id = (
        "task_"
        + plans._digest({"action": COLD_ACTION, "preparation": parent_id, "tenant": request["tenant_id"]})[
            :32
        ]
    )
    if conn.execute(
        "SELECT 1 FROM environment_tasks WHERE environment=? AND id!=? "
        "AND state IN ('waiting','executing','checking') "
        "AND json_extract(request,'$.action') IN "
        "('site-install','candidate-install','candidate-register','candidate-validate',"
        "'environment-publish') LIMIT 1",
        (environment, task_id),
    ).fetchone():
        raise tasks.TaskRefused("ENVIRONMENT_TASK_BUSY")
    conn.execute(
        "INSERT OR IGNORE INTO environment_tasks(id,environment,request,state,result,updated_at) "
        "VALUES(?,?,?,'waiting',?,?)",
        (
            task_id,
            environment,
            json.dumps(
                {
                    "identity": list(identity),
                    "action": COLD_ACTION,
                    "preparation": parent_id,
                    "tenant_id": request["tenant_id"],
                    "plan": request["plan"],
                    "target": request["target"],
                    "installer": request["installer"],
                    "python": request["python"],
                    "attempted": False,
                }
            ),
            json.dumps(
                {
                    "scope": "site-cold-installation-only",
                    "expires_at": request["plan"]["expires_at"],
                    "installation": "not_executed",
                }
            ),
            time.time(),
        ),
    )
    return task_id
