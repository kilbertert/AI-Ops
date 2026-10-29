"""Dify同源桥后的受控业务管理端点。"""

import hmac
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import self_service_identity as auth
from .self_service import SelfService
from .self_service_store import SetupConflict, public_job


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")
    source_app_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    source_version: int = Field(ge=1)
    tenant_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f]+$")


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
                )

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
        sources = []
        for route in service.routes.all():
            if route.entry != "operator" or not route.active or route.tenant_id not in allowed:
                continue
            if not service.managed(route.app_id):
                continue
            agent = context.agent_manager.store.get(route.agent_id, route.tenant_id)
            sources.append({"app_id": route.app_id, "name": agent.name, "version_no": route.agent_version_no})
        return {
            "platform": caller.platform,
            "tenants": choices,
            "sources": sources,
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
        from aiops_diagnostics.operator_cpms import OperatorRefused

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
