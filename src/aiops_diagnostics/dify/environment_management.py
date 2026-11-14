"""正常公司控制台会话后的环境管理；不向浏览器暴露连接或允许修改授权。"""

import hashlib
from pathlib import Path

from fastapi import Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from aiops_diagnostics.gateway import environment_registry as registry

from . import self_service_identity as auth
from .self_service_store import SetupConflict


class EnvironmentChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    revision: int = Field(ge=1, strict=True)
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = Field(strict=True)
    tenant_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


def _access(context, session):
    # 目录管理员仍须正常公司登录；停用业务准入不能令目录管理员无法恢复。
    caller = auth.identity(context, session, require_admission=False, require_platform_permissions=False)
    path = getattr(context.settings, "environment_registry_file", "")
    if not path:
        raise auth.SetupRefused("ENVIRONMENT_MANAGEMENT_UNAVAILABLE")
    try:
        body = registry.load(path)
    except registry.RegistryError as exc:
        raise auth.SetupRefused("ENVIRONMENT_MANAGEMENT_UNAVAILABLE") from exc
    if body["schema"] != registry.MANAGED_SCHEMA:
        raise auth.SetupRefused("ENVIRONMENT_REGISTRY_MIGRATION_REQUIRED")
    allowed = set()
    for manager in body.get("managers", []):
        if (manager["environment"], manager["tenant_id"], manager["user_id"]) == (
            context.settings.environment,
            caller.tenant_id,
            caller.user_id,
        ):
            allowed.update(manager["environments"])
    if not allowed:
        raise auth.SetupRefused("ENVIRONMENT_MANAGEMENT_FORBIDDEN")
    return caller, body, allowed


def catalogue(context, session):
    _, body, allowed = _access(context, session)
    return {
        "revision": body["revision"],
        "environment": context.settings.environment,
        "environments": [
            {
                "id": r["id"],
                "name": r["name"],
                "enabled": r["enabled"],
                "company_gateway_configured": bool(r["company_gateway_url"]),
                "acceptance": "unverified",
                "tenants": r["tenants"],
            }
            for r in body["environments"]
            if r["id"] in allowed
        ],
    }


def change(context, session, payload):
    caller, body, allowed = _access(context, session)
    if payload.environment not in allowed:
        raise auth.SetupRefused("ENVIRONMENT_MANAGEMENT_FORBIDDEN")
    if payload.revision != body["revision"]:
        raise SetupConflict("ENVIRONMENT_REGISTRY_REVISION_CONFLICT")
    row = next(r for r in body["environments"] if r["id"] == payload.environment)
    target = row
    if payload.tenant_id is not None:
        target = next((t for t in row["tenants"] if t["id"] == payload.tenant_id), None)
        if target is None:
            raise auth.SetupRefused("ENVIRONMENT_TENANT_NOT_REGISTERED")
    target.update(name=payload.name, enabled=payload.enabled)
    # 操作者由已认证复合身份生成，不接受浏览器by字段，也不记录令牌。
    actor = (
        "console-"
        + hashlib.sha256(
            f"{context.settings.environment}:{caller.tenant_id}:{caller.user_id}".encode()
        ).hexdigest()
    )
    try:
        revision = registry.apply(
            Path(context.settings.environment_registry_file),
            body["environments"],
            expected_revision=payload.revision,
            actor=actor,
            managers=body["managers"],
        )
    except registry.RegistryError as exc:
        if str(exc) == "ENVIRONMENT_REGISTRY_REVISION_CONFLICT":
            raise SetupConflict(str(exc)) from exc
        raise auth.SetupRefused("ENVIRONMENT_REGISTRY_UPDATE_REFUSED") from exc
    return {"revision": revision}


def register(app, context, session_dependency):
    @app.get("/v1/dify/management/environments")
    def read(response: Response, session=Depends(session_dependency)):  # noqa: B008
        response.headers["Cache-Control"] = "no-store"
        return catalogue(context, session)

    @app.post("/v1/dify/management/environments")
    def update(payload: EnvironmentChange, response: Response, session=Depends(session_dependency)):  # noqa: B008
        response.headers["Cache-Control"] = "no-store"
        return change(context, session, payload)
