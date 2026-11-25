"""中央候选知识准备；资源进度沿原环境任务账本保存。"""

import time

from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.knowledge.kb_onboarding import (
    KbAdminClient,
    OnboardError,
    derive_email,
    verify_migrated_owner,
)
from aiops_diagnostics.knowledge.knowledge_retrieval import KbServiceClient, KnowledgeSearchUnavailable
from aiops_diagnostics.knowledge.tenant_scope import knowledge_tenant

from . import self_service_prepare as prepare
from .dify_ensure_workspace import EnsureError, reconcile_by_name
from .dify_workspaces import DifyWorkspaceError, DifyWorkspaces
from .self_service_identity import SetupRefused


def run(context, path, reference, payload):
    if set(payload) != {"id", "owner", "prepare"} or payload["prepare"] != "knowledge":
        raise tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    identifier, owner = payload["id"], payload["owner"]

    def current(*, fresh=False):
        return tasks.workspace_request(
            path, reference, identifier, owner, action="knowledge-prepare", reconciling=not fresh
        )

    request = current()
    settings = context.settings
    tenant, environment = request["tenant_id"], request["plan"]["environment"]
    resources = request.get("resources", {})
    state, error, workspace_id, knowledge_id = "observed", "", "", ""

    def boundary():
        latest = current(fresh=True)
        binding()
        if latest["cancel_requested"]:
            raise SetupRefused("ENVIRONMENT_TASK_CANCELLED")

    def save(_step):
        boundary()
        tasks.save_knowledge_progress(path, reference, identifier, owner, resources)

    def binding():
        from .environment_management import knowledge_binding

        if request["binding"] != knowledge_binding(settings) or request["target_binding"] != resources.get(
            "target_binding", request["target_binding"]
        ):
            raise SetupRefused("SETUP_KNOWLEDGE_CONFIG_CHANGED")

    try:
        binding()
        resources["target_binding"] = request["target_binding"]
        workspaces = DifyWorkspaces(settings.database_file)
        workspace_id = workspaces.resolve(tenant, environment)
        if workspace_id != request["workspace_id"]:
            raise SetupRefused("SETUP_KNOWLEDGE_SCOPE_MISMATCH")
        if workspaces.tenant_for_workspace(workspace_id, environment, across_environments=True) != tenant:
            raise SetupRefused("SETUP_KNOWLEDGE_SCOPE_MISMATCH")
        rows = reconcile_by_name(
            console_base_url=settings.dify_console_base_url,
            console_api_key=settings.dify_console_api_key,
            name="",
            workspace_id=workspace_id,
        )
        if len(rows) != 1:
            raise SetupRefused("SETUP_WORKSPACE_PENDING")
        stored = context.store.get_ragflow_tenant(tenant, environment)
        expected = derive_email(knowledge_tenant(tenant, environment), domain=settings.ragflow_tenant_domain)
        if (
            stored
            and stored["ragflow_email"] != expected
            and not resources.get("knowledge_base_id")
            and not resources.get("knowledge_create", {}).get("attempted")
        ):
            # 旧owner的库须由受控迁移明确登记，不能以新空库替代旧业务资料。
            raise SetupRefused("SETUP_KNOWLEDGE_SCOPE_MISMATCH")
        pending = resources.get("knowledge_create", {})
        # 已发出的建库只对账，不因取消/证据到期重新注册、修模型或发POST。
        if not pending.get("attempted"):
            boundary()
            prepare.knowledge(context, tenant, environment=environment, before_step=boundary)
            resources["tenant_ready"] = True
            resources["verified_binding"] = request["binding"]
            save("knowledge:tenant")
        else:
            stored = context.store.get_ragflow_tenant(tenant, environment)
            expected = derive_email(
                knowledge_tenant(tenant, environment), domain=settings.ragflow_tenant_domain
            )
            if not stored:
                raise SetupRefused("SETUP_KNOWLEDGE_SCOPE_MISMATCH")
            if stored["ragflow_email"] != expected:
                try:
                    verify_migrated_owner(
                        KbAdminClient(settings.kb_service_base_url, environment=environment),
                        tenant,
                        stored,
                        settings.ragflow_credential_key,
                    )
                except OnboardError as exc:
                    raise SetupRefused("SETUP_KNOWLEDGE_SCOPE_MISMATCH") from exc
        known_id = resources.get("knowledge_base_id", "")
        knowledge_id = known_id or prepare.customer_knowledge_base(
            context, tenant, resources, save, environment=environment
        )
        client = KbServiceClient(settings.kb_service_base_url, tenant_id=tenant, environment=environment)
        matches = [r for r in client.list_knowledge_bases() if r["knowledge_base_id"] == knowledge_id]
        if len(matches) != 1 or (
            not known_id and matches[0]["name"] != resources["knowledge_create"]["name"]
        ):
            raise SetupRefused("SETUP_KNOWLEDGE_CREATE_UNCONFIRMED")
        resources["knowledge_base_id"] = knowledge_id
        tasks.save_knowledge_progress(path, reference, identifier, owner, resources)
        if resources.get("verified_binding") != request["binding"]:
            boundary()
            prepare.knowledge(context, tenant, environment=environment, before_step=boundary)
            resources["verified_binding"] = request["binding"]
            tasks.save_knowledge_progress(path, reference, identifier, owner, resources)
    except (
        SetupRefused,
        EnsureError,
        DifyWorkspaceError,
        KnowledgeSearchUnavailable,
        tasks.TaskRefused,
    ) as exc:
        code = (
            getattr(exc, "code", str(exc))
            if not isinstance(exc, KnowledgeSearchUnavailable)
            else "SETUP_KNOWLEDGE_CREATE_UNCONFIRMED"
        )
        if code in {"ENVIRONMENT_TASK_LEASE_LOST", "ENVIRONMENT_TASK_UNAVAILABLE"}:
            raise
        if code in {
            "SETUP_KNOWLEDGE_CREATE_UNCONFIRMED",
            "DIFY_RECONCILE_UNAVAILABLE",
            "SETUP_WORKSPACE_PENDING",
            "SETUP_KNOWLEDGE_REGISTER_UNCONFIRMED",
        }:
            return tasks.worker(
                path,
                reference,
                {"id": identifier, "owner": owner, "error": "ENVIRONMENT_TASK_TARGET_UNCONFIRMED"},
            )
        state, error = "blocked", code
    now = time.time()
    result = {
        "scope": "candidate-knowledge-only",
        "environment": environment,
        "tenant_id": tenant,
        "workspace_id": workspace_id,
        "knowledge_base_id": knowledge_id,
        "state": state,
        "error_code": error,
        "executable": False,
        "created_at": now,
        "expires_at": now + 900,
        "checks": [
            {"id": "workspace", "state": "passed" if workspace_id else "blocked"},
            {"id": "knowledge", "state": "passed" if state == "observed" else "blocked"},
            *[{"id": k, "state": "pending"} for k in ("application", "business")],
        ],
    }
    return tasks.finish_workspace(path, reference, identifier, owner, result)
