"""正常公司会话后的显式目录授权；连接草稿不改变生效目录。"""

import hashlib
import json
import sqlite3
from pathlib import Path

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from aiops_diagnostics.common.http_auth import validate_internal_token
from aiops_diagnostics.gateway import environment_drafts, environment_plans, environment_tasks
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
    path = _path(context)
    if not path:
        raise auth.SetupRefused("ENVIRONMENT_MANAGEMENT_UNAVAILABLE")
    try:
        body = registry.load(path)
    except registry.RegistryError as exc:
        raise auth.SetupRefused("ENVIRONMENT_MANAGEMENT_UNAVAILABLE") from exc
    if body["schema"] != registry.MANAGED_SCHEMA:
        raise auth.SetupRefused("ENVIRONMENT_REGISTRY_MIGRATION_REQUIRED")
    try:
        allowed = environment_drafts.access(body, _identity(context, caller))
    except registry.RegistryError as exc:
        raise auth.SetupRefused(str(exc)) from exc
    return caller, body, allowed


def _path(context):
    return getattr(context.settings, "environment_management_registry_file", "") or getattr(
        context.settings, "environment_registry_file", ""
    )


def _identity(context, caller):
    return (context.settings.environment, caller.tenant_id, caller.user_id)


def drafts(context, session, payload=None):
    caller, _, _ = _access(context, session)
    try:
        if payload is None:
            return environment_drafts.catalogue(_path(context), _identity(context, caller))
        if set(payload) != {"draft", "revision", "registry_revision"}:
            raise registry.RegistryError("ENVIRONMENT_DRAFT_INVALID")
        return environment_drafts.save(
            _path(context),
            _identity(context, caller),
            payload["draft"],
            expected_revision=payload["revision"],
            registry_revision=payload["registry_revision"],
        )
    except registry.RegistryError as exc:
        if str(exc).endswith("REVISION_CONFLICT"):
            raise SetupConflict(str(exc)) from exc
        raise auth.SetupRefused(str(exc)) from exc
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise auth.SetupRefused("ENVIRONMENT_DRAFT_UNAVAILABLE") from exc


def plan(context, session, payload):
    caller, _, _ = _access(context, session)
    try:
        reference_file = getattr(context.settings, "environment_onboarding_file", "")
        if set(payload) == {"id"}:
            return environment_plans.read(
                _path(context), _identity(context, caller), payload["id"], reference_file
            )
        if set(payload) != {"environment", "revision", "registry_revision"}:
            raise environment_plans.PlanRefused("ENVIRONMENT_PLAN_INVALID")
        return environment_plans.preflight(
            _path(context),
            _identity(context, caller),
            payload["environment"],
            payload["revision"],
            payload["registry_revision"],
            reference_file,
        )
    except environment_plans.PlanRefused as exc:
        if str(exc).endswith("REVISION_CONFLICT"):
            raise SetupConflict(str(exc)) from exc
        raise auth.SetupRefused(str(exc)) from exc
    except (registry.RegistryError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise auth.SetupRefused("ENVIRONMENT_PLAN_UNAVAILABLE") from exc


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
            if "*" in allowed or r["id"] in allowed
        ],
    }


def change(context, session, payload):
    caller, body, allowed = _access(context, session)
    if "*" not in allowed and payload.environment not in allowed:
        raise auth.SetupRefused("ENVIRONMENT_MANAGEMENT_FORBIDDEN")
    if payload.revision != body["revision"]:
        raise SetupConflict("ENVIRONMENT_REGISTRY_REVISION_CONFLICT")
    row = next((r for r in body["environments"] if r["id"] == payload.environment), None)
    if row is None:
        raise auth.SetupRefused("ENVIRONMENT_NOT_REGISTERED")
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
            Path(_path(context)),
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


def task(context, session, payload):
    caller, _, _ = _access(context, session)
    identity, path = _identity(context, caller), _path(context)
    reference = getattr(context.settings, "environment_onboarding_file", "")
    try:
        if len(json.dumps(payload)) > 65536:
            raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_INVALID")
        if set(payload) == {"plan_id"}:
            if not getattr(context.settings, "environment_task_key", ""):
                raise auth.SetupRefused("ENVIRONMENT_TASK_WORKER_UNAVAILABLE")
            return environment_tasks.submit(path, identity, payload["plan_id"], reference)
        if set(payload) == {"environment"}:
            return environment_tasks.inspect(
                path, identity, environment=payload["environment"], reference=reference
            )
        if set(payload) in ({"id"}, {"id", "cancel"}) and payload.get("cancel", True) is True:
            return environment_tasks.inspect(
                path, identity, identifier=payload["id"], cancel="cancel" in payload, reference=reference
            )
        raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_INVALID")
    except (environment_tasks.TaskRefused, environment_plans.PlanRefused) as exc:
        if str(exc).endswith(("BLOCKED", "EXPIRED", "LEASE_LOST")):
            raise SetupConflict(str(exc)) from exc
        raise auth.SetupRefused(str(exc)) from exc
    except (registry.RegistryError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise auth.SetupRefused("ENVIRONMENT_TASK_UNAVAILABLE") from exc


def register(app, context, session_dependency):
    @app.post("/v1/dify/management/environment-tasks")
    def manage_task(payload: dict, response: Response, session=Depends(session_dependency)):  # noqa: B008
        response.headers["Cache-Control"] = "no-store"
        return task(context, session, payload)

    @app.post("/v1/internal/environment-tasks")
    def execute_task(payload: dict, request: Request, response: Response):
        key = getattr(context.settings, "environment_task_key", "")
        if not key or not _path(context):
            return JSONResponse(
                status_code=404, content={"error": {"code": "ENVIRONMENT_TASK_WORKER_UNAVAILABLE"}}
            )
        if not validate_internal_token(
            request.headers.get("X-Internal-Token"), request.headers.get("X-Request-Timestamp"), key
        ):
            return JSONResponse(
                status_code=401, content={"error": {"code": "ENVIRONMENT_TASK_WORKER_CREDENTIAL_REQUIRED"}}
            )
        response.headers["Cache-Control"] = "no-store"
        try:
            if len(json.dumps(payload)) > 65536:
                raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
            return environment_tasks.worker(
                _path(context), getattr(context.settings, "environment_onboarding_file", ""), payload
            )
        except environment_tasks.TaskRefused as exc:
            raise SetupConflict(str(exc)) from exc
        except (
            registry.RegistryError,
            environment_plans.PlanRefused,
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
        ) as exc:
            raise auth.SetupRefused("ENVIRONMENT_TASK_UNAVAILABLE") from exc

    @app.post("/v1/dify/management/environment-plans")
    def check_plan(payload: dict, response: Response, session=Depends(session_dependency)):  # noqa: B008
        response.headers["Cache-Control"] = "no-store"
        return plan(context, session, payload)

    @app.get("/v1/dify/management/environment-drafts")
    def read_drafts(response: Response, session=Depends(session_dependency)):  # noqa: B008
        response.headers["Cache-Control"] = "no-store"
        return drafts(context, session)

    @app.post("/v1/dify/management/environment-drafts")
    def save_draft(payload: dict, response: Response, session=Depends(session_dependency)):  # noqa: B008
        response.headers["Cache-Control"] = "no-store"
        return drafts(context, session, payload)

    @app.get("/v1/dify/management/environments")
    def read(response: Response, session=Depends(session_dependency)):  # noqa: B008
        response.headers["Cache-Control"] = "no-store"
        return catalogue(context, session)

    @app.post("/v1/dify/management/environments")
    def update(payload: EnvironmentChange, response: Response, session=Depends(session_dependency)):  # noqa: B008
        response.headers["Cache-Control"] = "no-store"
        return change(context, session, payload)
