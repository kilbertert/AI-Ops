"""Dify同源桥后的受控业务管理端点。"""

import hmac
import json
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import dual_entry
from . import self_service_identity as auth
from .self_service import SelfService
from .self_service_store import SetupConflict, public_job


class SetupSource(BaseModel):
    """一端的来源：已发布的应用 + 它当前的正式版本号。"""

    model_config = ConfigDict(extra="forbid")
    app_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    version_no: int = Field(ge=1)


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")
    tenant_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f]+$")
    #: 开通范围。默认双端；只开一端时把另一端从 `sources` 里去掉。
    scope: str = Field(default="both", pattern=r"^(both|consumer|operator)$")
    #: 每端一个来源。缺一端且该端在 `scope` 里 ⇒ `SETUP_SOURCE_REQUIRED`。
    sources: dict[str, SetupSource] = Field(default_factory=dict)
    #: 旧的单来源字段：它只能表达管家（“复制并开通客户管家”的入参）。保留它既有的
    #: 调用方逐字可用；两者都给时以 `sources` 为准。
    source_app_id: str | None = Field(default=None, pattern=r"^[a-f0-9-]{36}$")
    source_version: int | None = Field(default=None, ge=1)


class AppRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str = Field(pattern=r"^[a-f0-9-]{36}$")


class WorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(pattern=r"^[a-f0-9-]{36}$")


class MemberRequest(WorkspaceRequest):
    account_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    email: str = Field(min_length=3, max_length=254)


class PublishRequest(AppRequest):
    revision: int = Field(ge=0)
    draft: str = Field(pattern=r"^[a-f0-9]{64}$")


class VersionRequest(AppRequest):
    revision: int = Field(ge=0)
    version_no: int | None = Field(default=None, ge=1)


def register(app, context):
    service = SelfService(context)
    context.self_service = service
    service.store.recover()
    if context.settings.operator_profiles_file:
        # 迁移现有应用的管理归属，不触碰版本或正式指针。
        #
        # **端/场景/环境/目标必须一起带过去。** 只写应用与 agent 名会让一条客户端登记落成
        # `environment=''`、`targets_json='{}'`：目录里有这一行，而按
        # `(租户, 环境)` 找客户端运行目标时找不到 —— 症状是"现役客户端应用在册但发布时说
        # 它不受管"。管家行不受影响（它不按环境查目标），所以这个缺口只会在第一次纳管
        # 客户端应用时出现。
        for binding in context.runtime.dify_app_registry.all():
            row = service.managed(binding.app_id)
            if row:
                service.store.register(
                    app_id=row["app_id"],
                    tenant_id=row["tenant_id"],
                    workspace_id=row["workspace_id"],
                    agent_name=row["agent_name"],
                    profile_id=row["profile_id"],
                    actor="managed-adoption",
                    entry=row["entry"],
                    scenario=row["scenario"],
                    environment=row["environment"] or context.settings.execution_environment,
                    targets=json.loads(row.get("targets_json") or "{}")
                    if row["entry"] == "consumer"
                    else None,
                )

        # 现役已经发布给消费者的库，按"当时就是明确的绑定"补一条授权行（规划 §5：
        # 已有库迁移需保持现役明确绑定）。只在没有授权行时补，不会覆盖后来的撤销。
        _backfill_knowledge_grants(context, service)

    def session(request: Request, authorization: Annotated[str | None, Header()] = None):
        expected = context.settings.dify_publish_api_key
        supplied = (authorization or "").removeprefix("Bearer ")
        if not context.settings.operator_profiles_file:
            raise auth.SetupRefused("SETUP_DISABLED")
        if not expected or not hmac.compare_digest(expected, supplied):
            raise auth.SetupRefused("SETUP_CREDENTIAL_REQUIRED")
        return request.headers.get("X-AIOps-Console-Session", "")

    @app.exception_handler(auth.SetupRefused)
    async def refused(_request, exc):
        return JSONResponse(
            status_code=403, content={"error": {"code": str(exc)}}, headers={"Cache-Control": "no-store"}
        )

    @app.exception_handler(SetupConflict)
    async def conflict(_request, exc):
        return JSONResponse(
            status_code=409, content={"error": {"code": str(exc)}}, headers={"Cache-Control": "no-store"}
        )

    @app.get("/v1/dify/management/catalogue")
    def catalogue(session=Depends(session)):  # noqa: B008 - FastAPI dependency
        caller = auth.identity(context, session)
        choices = auth.tenants(context, caller) if caller.platform else []
        allowed = {x["id"] for x in choices} | {caller.tenant_id}
        tenant_names = {row["id"]: row["name"] for row in choices}

        def sources_for(entry):
            rows = []
            for route in service.routes.all():
                if route.entry != entry or not route.active or route.tenant_id not in allowed:
                    continue
                if route.scenario != dual_entry.scenario(entry):
                    continue
                managed = service.managed(route.app_id)
                if not managed or managed["entry"] != entry:
                    continue
                agent = context.agent_manager.store.get(route.agent_id, route.tenant_id)
                app_name = service.source_app_name(route.tenant_id, route.app_id, agent.name)
                tenant_name = tenant_names.get(route.tenant_id, "")
                # 标签回答的是「这是谁的哪个应用」：来源应用全部住在**客户工作区**里，
                # 平台账号平时看不到它们，只写智能体名等于给出一个运营无从对照的字符串。
                rows.append(
                    {
                        "app_id": route.app_id,
                        "tenant_id": route.tenant_id,
                        "tenant_name": tenant_name,
                        "app_name": app_name,
                        "name": f"{tenant_name} / {app_name}" if tenant_name else app_name,
                        "version_no": route.agent_version_no,
                    }
                )
            return rows

        entries = [
            {
                "entry": entry,
                "label": dual_entry.label(entry),
                "scenario": dual_entry.scenario(entry),
                "sources": sources_for(entry),
            }
            for entry in dual_entry.ENTRIES
        ]
        return {
            "platform": caller.platform,
            "tenants": choices,
            # 顶层 `sources` 保持管家端（既有前端不改也能跑）；读取方优先读 `entries`。
            "sources": next(item["sources"] for item in entries if item["entry"] == "operator"),
            "entries": entries,
            "workspaces": [
                {
                    "workspace_id": r["workspace_id"],
                    "tenant_id": r["tenant_id"],
                    "name": next((t["name"] for t in choices if t["id"] == r["tenant_id"]), r["tenant_id"]),
                }
                for r in context.dify_workspaces.list_for_environment(context.settings.environment)
                if r["tenant_id"] in allowed and r["setup_state"] in {"ready", "created"}
            ],
            "operations": [public_job(r) for r in service.store.recent(caller.user_id)],
        }

    @app.post("/v1/dify/management/workspace")
    def workspace(payload: WorkspaceRequest, session=Depends(session)):  # noqa: B008
        caller = auth.identity(context, session)
        tenant = service.workspace(caller, payload.workspace_id)
        return {"tenant_id": tenant, "workspace_id": payload.workspace_id, "platform": caller.platform}

    @app.post("/v1/dify/management/workspace/member")
    def member(payload: MemberRequest, session=Depends(session)):  # noqa: B008
        caller = auth.identity(context, session)
        tenant = service.workspace(caller, payload.workspace_id)
        auth.require_target(context, caller, tenant)
        from .dify_console_publish import _request
        from .dify_inner_api import DifyInnerApi, InnerApiSettings
        from .dify_tenant_access import grant_tenant_access

        code, identity = _request(
            context.settings.operator_trial_login_url.rstrip("/") + "/__aiops/publish/check",
            method="POST",
            headers={"Content-Type": "application/json"},
            body={"session": session},
            timeout=5,
        )
        if (
            code != 200
            or not isinstance(identity, dict)
            or identity.get("dify_account_id") != payload.account_id
            or identity.get("company_user_id") != caller.user_id
        ):
            raise auth.SetupRefused("SETUP_IDENTITY_MISMATCH")
        grant_tenant_access(
            tenant_id=tenant,
            environment=context.settings.environment,
            workspaces=context.dify_workspaces,
            inner=DifyInnerApi(
                InnerApiSettings(
                    context.settings.dify_inner_api_base_url, context.settings.dify_inner_api_key
                )
            ),
            account_id=payload.account_id,
            email=payload.email,
        )
        return {"workspace_id": payload.workspace_id}

    @app.post("/v1/dify/management/status")
    def status(payload: AppRequest, session=Depends(session)):  # noqa: B008 - FastAPI dependency
        caller = auth.identity(context, session)
        if not service.managed(payload.app_id) and not service.adopt_draft(caller, payload.app_id):
            return {"managed": False}
        return service.status(caller, payload.app_id)

    @app.post("/v1/dify/management/readiness")
    def readiness(payload: AppRequest, session=Depends(session)):  # noqa: B008 - FastAPI dependency
        caller = auth.identity(context, session)
        row = service.authorize_app(caller, payload.app_id)
        if caller.tenant_id != row["tenant_id"]:
            return {"can_diagnose": False, "reason": "请使用该客户的公司账号预览订单"}
        from aiops_diagnostics.operator.operator_cpms import OperatorRefused

        try:
            context.self_service_operator_identity(session)
        except OperatorRefused as exc:
            if str(exc) == "OPERATOR_SITE_SCOPE_REQUIRED":
                return {
                    "can_diagnose": False,
                    "reason": "当前账号未分配可诊断场站，请联系公司管理员分配或切换账号",
                }
            return {"can_diagnose": False, "reason": "公司登录或场站权限暂时无法核验，请重新登录"}
        return {"can_diagnose": True, "reason": ""}

    @app.post("/v1/dify/management/operations", status_code=202)
    def create(payload: SetupRequest, session=Depends(session)):  # noqa: B008 - FastAPI dependency
        caller = auth.identity(context, session)
        job = service.create(caller, payload)
        service.enqueue(job["id"], session)
        return public_job(service.store.get(job["id"]))

    @app.get("/v1/dify/management/operations/{job_id}")
    def get(job_id: str, session=Depends(session)):  # noqa: B008 - FastAPI dependency
        caller = auth.identity(context, session)
        row = service.store.get(job_id)
        if row is None or row["actor"] != caller.user_id:
            raise auth.SetupRefused("SETUP_OPERATION_NOT_FOUND")
        return public_job(row)

    @app.post("/v1/dify/management/operations/{job_id}/retry", status_code=202)
    def retry(job_id: str, session=Depends(session)):  # noqa: B008 - FastAPI dependency
        get(job_id, session)
        service.enqueue(job_id, session)
        return public_job(service.store.get(job_id))

    @app.post("/v1/dify/management/publish")
    def publish(payload: PublishRequest, session=Depends(session)):  # noqa: B008 - FastAPI dependency
        return service.publish(
            auth.identity(context, session), payload.app_id, payload.revision, payload.draft
        )

    @app.post("/v1/dify/management/versions")
    def versions(payload: AppRequest, session=Depends(session)):  # noqa: B008
        return {"versions": service.versions(auth.identity(context, session), payload.app_id)}

    @app.post("/v1/dify/management/version")
    def change_version(payload: VersionRequest, session=Depends(session)):  # noqa: B008
        return service.change_version(
            auth.identity(context, session), payload.app_id, payload.revision, payload.version_no
        )


def _backfill_knowledge_grants(context, service):
    """首次纳管沿用现役选库；已登记（含已撤销）的库不被重新授权。"""
    from aiops_diagnostics.consumer.consumer_knowledge_grants import KnowledgeGrants

    settings = context.settings
    grants = KnowledgeGrants(settings.database_file)
    for route in service.routes.all():
        if route.entry != "consumer" or not route.active or route.engine != "dify":
            continue
        try:
            snapshot = context.agent_manager.store.version(
                route.agent_id, route.tenant_id, route.agent_version_no
            ).snapshot
        except Exception:  # noqa: BLE001 - 读不到版本只是少补一次授权，不影响启动
            continue
        frozen = snapshot.get("consumer_service") or {}
        for knowledge_base_id in frozen.get("knowledge_base_ids") or []:
            if not isinstance(knowledge_base_id, str) or not knowledge_base_id:
                continue
            grants.grant(
                route.tenant_id,
                settings.execution_environment,
                knowledge_base_id,
                "published-backfill",
                backfill=True,
            )
