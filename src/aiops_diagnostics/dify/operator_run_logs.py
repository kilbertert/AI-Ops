"""受管管家原日志入口；公司身份、当前权限与现有账本共用原接缝。"""

from fastapi import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from aiops_diagnostics.execution.operator_run_logs import read_runs

from . import self_service_identity as auth


class LogPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    app_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    page: int = Field(default=1, ge=1, le=1000)
    limit: int = Field(default=20, ge=1, le=50)


class LogDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    app_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_-]{43}$")


def register(app, context, service, session):
    prefix = "/v1/dify/management/run-logs"

    @app.middleware("http")
    async def private_logs(request, call_next):
        if request.url.path.startswith(prefix):
            if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                return JSONResponse(
                    status_code=415,
                    content={"error": {"code": "JSON_REQUIRED"}},
                    headers={"Cache-Control": "no-store"},
                )
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        return await call_next(request)

    def current_caller(app_id, session):
        manager = auth.identity(context, session)
        # 日志不走惰性纳管：只读已登记应用，避免读取创建 store 或写 schema。
        row = service.store.app(app_id)
        if not row or row["entry"] != "operator" or row["tenant_id"] != manager.tenant_id:
            raise auth.SetupRefused("SETUP_APP_FORBIDDEN")
        # 该接缝现场解析公司凭据并 refresh_scope，绝不从请求体接受 caller/tenant。
        caller, _ = context.self_service_operator_identity(session)
        if (
            caller.delegated
            or caller.caller.b_user_id != manager.user_id
            or caller.effective_tenant_id != manager.tenant_id
        ):
            raise auth.SetupRefused("SETUP_IDENTITY_MISMATCH")
        return caller

    @app.post(prefix)
    def listing(payload: LogPage, session=Depends(session)):  # noqa: B008
        caller = current_caller(payload.app_id, session)
        items, more = read_runs(
            context.agent_run_store, caller, payload.app_id, page=payload.page, limit=payload.limit
        )
        return {"items": items, "page": payload.page, "has_more": more}

    @app.post(prefix + "/detail")
    def detail(payload: LogDetail, session=Depends(session)):  # noqa: B008
        caller = current_caller(payload.app_id, session)
        items, _ = read_runs(context.agent_run_store, caller, payload.app_id, run_id=payload.run_id)
        if not items:
            raise auth.SetupRefused("OPERATOR_RUN_NOT_FOUND")
        return items[0]
