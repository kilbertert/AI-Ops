"""正常公司会话后的显式目录授权；连接草稿不改变生效目录。"""

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from aiops_diagnostics.common.http_auth import validate_internal_token
from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify.dify_ensure_workspace import EnsureError, ensure_workspace, reconcile_by_name
from aiops_diagnostics.dify.dify_inner_api import DifyInnerApi, InnerApiSettings
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaceError, DifyWorkspaces
from aiops_diagnostics.dify.self_service_store import SetupConflict
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.onboarding import candidate_preparation as environment_candidate_preparation
from aiops_diagnostics.onboarding import consumer_prep as environment_consumer
from aiops_diagnostics.onboarding import drafts as environment_drafts
from aiops_diagnostics.onboarding import (
    host_enrollment,
    installation,
    knowledge,
    knowledge_connection,
    operator_prep,
    publication,
    registration,
    registry,
    validation,
    wizard_dispatch,
)
from aiops_diagnostics.onboarding import orchestration as environment_onboarding
from aiops_diagnostics.onboarding import overview as environment_overview
from aiops_diagnostics.onboarding import plans as environment_plans
from aiops_diagnostics.onboarding import tasks as environment_tasks
from aiops_diagnostics.onboarding.context import (
    COLD_ACTION,
)
from aiops_diagnostics.onboarding.context import (
    _identity as _identity,
)
from aiops_diagnostics.onboarding.context import (
    _path as _path,
)
from aiops_diagnostics.onboarding.context import (
    knowledge_binding as knowledge_binding,
)


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


def drafts(context, session, payload=None):
    caller, _, _ = _access(context, session)
    try:
        if payload is None:
            return environment_overview.catalogue_with_overview(
                context, _path(context), _identity(context, caller)
            )
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


def overview(context, session):
    caller, _, _ = _access(context, session)
    try:
        return environment_overview.build_overview_locked(context, _path(context), _identity(context, caller))
    except registry.RegistryError as exc:
        raise auth.SetupRefused(str(exc)) from exc
    except (OSError, ValueError, RuntimeError, sqlite3.Error, TypeError, KeyError, AttributeError) as exc:
        raise auth.SetupRefused("ENVIRONMENT_OVERVIEW_UNAVAILABLE") from exc


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
        if set(payload) == {"restore_publication"}:
            if not caller.platform:
                raise auth.SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
            return publication.restore_task(context, identity, payload["restore_publication"])
        if set(payload) == {
            "validation_task",
            "publication_plan",
            "confirm_readonly",
            "confirm_customer_knowledge",
            "acceptance_ref",
        }:
            if not caller.platform:
                raise auth.SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
            return publication.submit(context, identity, payload)
        if set(payload) == {"registration_task", "validation_plan", "capabilities", "case_ref"}:
            if not caller.platform:
                raise auth.SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
            return validation.submit(context, identity, payload)
        if set(payload) == {"installation_task", "registration_plan"}:
            if not caller.platform:
                raise auth.SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
            return registration.submit(
                context, identity, payload["installation_task"], payload["registration_plan"]
            )
        if set(payload) == {"candidate_task", "installation"} and payload["installation"] is True:
            if not caller.platform:
                raise auth.SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
            return installation.submit(context, identity, payload["candidate_task"])
        if "onboard_plan" in payload:
            if not caller.platform:
                raise auth.SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
            allowed_keys = {
                "onboard_plan",
                "tenant_id",
                "source_app_id",
                "source_version",
                "initial_installation",
                "case_ref",
                "capabilities",
            }
            required_keys = {"onboard_plan", "tenant_id", "source_app_id", "source_version"}
            if not required_keys <= set(payload) or not set(payload) <= allowed_keys:
                raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_INVALID")
            if "initial_installation" in payload and payload["initial_installation"] is not True:
                raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_INVALID")
            source = environment_consumer.sources(context, caller, payload["source_app_id"])
            if (
                type(payload["source_version"]) is not int
                or source["version_no"] != payload["source_version"]
            ):
                raise SetupConflict("SETUP_SOURCE_CHANGED")
            environment_consumer.document(source, "candidate-validation")
            return environment_onboarding.submit(context, identity, payload, source)
        if set(payload) in (
            {"candidate_plan", "tenant_id", "source_app_id", "source_version"},
            {"candidate_plan", "tenant_id", "source_app_id", "source_version", "initial_installation"},
        ):
            if not caller.platform:
                raise auth.SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
            # 「包含初装」只接受显式true确认；否则载荷里不得出现该键。
            if "initial_installation" in payload and payload["initial_installation"] is not True:
                raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_INVALID")
            source = environment_consumer.sources(context, caller, payload["source_app_id"])
            if (
                type(payload["source_version"]) is not int
                or source["version_no"] != payload["source_version"]
            ):
                raise SetupConflict("SETUP_SOURCE_CHANGED")
            environment_consumer.document(source, "candidate-validation")
            return environment_candidate_preparation.submit(context, identity, payload, source)
        if set(payload) in (
            {"consumer_sources"},
            {"connection_task", "tenant_id", "source_app_id", "source_version"},
        ):
            if not caller.platform:
                raise auth.SetupRefused("SETUP_PLATFORM_PERMISSION_REQUIRED")
            if set(payload) == {"consumer_sources"}:
                if payload["consumer_sources"] is not True:
                    raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_INVALID")
                return environment_consumer.sources(context, caller)
            source = environment_consumer.sources(context, caller, payload["source_app_id"])
            if type(payload["source_version"]) is not int or payload["source_version"] < 1:
                raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_INVALID")
            if source["version_no"] != payload["source_version"]:
                raise SetupConflict("SETUP_SOURCE_CHANGED")
            # 校验模板后才入任务，不接受浏览器提交图、模型凭据或服务器地址。
            environment_consumer.document(source, "candidate-validation")
            return environment_tasks.submit_workspace(
                path,
                identity,
                payload["connection_task"],
                payload["tenant_id"],
                reference,
                application=True,
                consumer=True,
                consumer_source=source,
                binding=environment_consumer.binding(context.settings, source),
            )
        if set(payload) == {"plan_id"}:
            if not getattr(context.settings, "environment_task_key", ""):
                raise auth.SetupRefused("ENVIRONMENT_TASK_WORKER_UNAVAILABLE")
            return environment_tasks.submit(path, identity, payload["plan_id"], reference)
        if set(payload) == {"verify_task", "plan_id"}:
            if not getattr(context.settings, "environment_task_key", ""):
                raise auth.SetupRefused("ENVIRONMENT_TASK_WORKER_UNAVAILABLE")
            return environment_tasks.submit_verify(
                path, identity, payload["plan_id"], payload["verify_task"], reference
            )
        if set(payload) == {"site_task", "tenant_id"}:
            if not getattr(context.settings, "environment_task_key", ""):
                raise auth.SetupRefused("ENVIRONMENT_TASK_WORKER_UNAVAILABLE")
            if not (
                getattr(context.settings, "dify_inner_api_base_url", "")
                and getattr(context.settings, "dify_inner_api_key", "")
                and getattr(context.settings, "dify_console_base_url", "")
                and getattr(context.settings, "dify_console_api_key", "")
            ):
                raise auth.SetupRefused("ENVIRONMENT_WORKSPACE_CONFIG_REQUIRED")
            return environment_tasks.submit_workspace(
                path, identity, payload["site_task"], payload["tenant_id"], reference
            )
        if set(payload) == {"workspace_task", "tenant_id"}:
            if not getattr(context.settings, "environment_task_key", ""):
                raise auth.SetupRefused("ENVIRONMENT_TASK_WORKER_UNAVAILABLE")
            if not all(
                getattr(context.settings, key, "")
                for key in (
                    "kb_service_base_url",
                    "ragflow_credential_key",
                    "ragflow_embedding_base_url",
                    "ragflow_embedding_api_key",
                    "dify_console_base_url",
                    "dify_console_api_key",
                )
            ):
                raise auth.SetupRefused("SETUP_KNOWLEDGE_CONFIG_REQUIRED")
            return environment_tasks.submit_workspace(
                path,
                identity,
                payload["workspace_task"],
                payload["tenant_id"],
                reference,
                knowledge=True,
                binding=knowledge_binding(context.settings),
                target_binding=knowledge_binding(context.settings, target_only=True),
            )
        if set(payload) == {"environment"}:
            return environment_tasks.inspect(
                path, identity, environment=payload["environment"], reference=reference
            )
        if set(payload) == {"knowledge_task", "tenant_id"}:
            if not all(
                getattr(context.settings, key, "")
                for key in (
                    "internal_token_secret",
                    "dify_knowledge_api_key",
                    "dify_console_base_url",
                    "dify_console_api_key",
                    "kb_service_base_url",
                    "environment_task_key",
                )
            ):
                raise auth.SetupRefused("SETUP_KNOWLEDGE_CONFIG_REQUIRED")
            return environment_tasks.submit_workspace(
                path,
                identity,
                payload["knowledge_task"],
                payload["tenant_id"],
                reference,
                connect=True,
                binding=knowledge_connection.binding(context.settings),
            )
        if set(payload) == {"connection_task", "tenant_id"}:
            if not getattr(context.settings, "operator_profiles_file", ""):
                raise auth.SetupRefused("SETUP_MODEL_CONFLICT")
            return environment_tasks.submit_workspace(
                path,
                identity,
                payload["connection_task"],
                payload["tenant_id"],
                reference,
                application=True,
                binding=operator_prep.binding(context.settings),
            )
        if set(payload) in ({"id"}, {"id", "cancel"}) and payload.get("cancel", True) is True:
            return environment_tasks.inspect(
                path, identity, identifier=payload["id"], cancel="cancel" in payload, reference=reference
            )
        raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_INVALID")
    except SetupConflict:
        raise
    except (environment_tasks.TaskRefused, environment_plans.PlanRefused) as exc:
        if str(exc).endswith(("BLOCKED", "EXPIRED", "LEASE_LOST")):
            raise SetupConflict(str(exc)) from exc
        raise auth.SetupRefused(str(exc)) from exc
    except (registry.RegistryError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise auth.SetupRefused("ENVIRONMENT_TASK_UNAVAILABLE") from exc


def dispatch_installation_claim(context, payload):
    path = _path(context)
    if payload.get("installation_claim") is not True or set(payload) != {"installation_claim", "owner"}:
        raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    owner = payload["owner"]
    if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner):
        raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    if not environment_drafts._database(path).exists():
        return {"claimed": False, "task": None}
    with (
        environment_drafts._locked(path),
        gateway_db.connection(environment_drafts._database(path), write=True) as conn,
    ):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='environment_tasks'").fetchone():
            return {"claimed": False, "task": None}
        rows = conn.execute(
            "SELECT id FROM environment_tasks WHERE json_extract(request,'$.action') "
            "IN ('site-install','candidate-install','candidate-register','candidate-validate',"
            "'environment-publish') "
            "AND state IN ('waiting','executing','checking') AND lease_until<=? "
            "AND (state!='checking' OR updated_at<=? "
            "OR json_extract(request,'$.action')!='site-install') "
            "AND NOT EXISTS(SELECT 1 FROM environment_tasks live "
            "WHERE live.environment=environment_tasks.environment AND live.id!=environment_tasks.id "
            "AND live.state IN ('running','executing') AND live.lease_until>?) "
            "ORDER BY updated_at LIMIT 20",
            (time.time(), time.time() - 60, time.time()),
        ).fetchall()
        for selected in rows:
            row = environment_tasks._row(conn, selected[0])
            request = json.loads(row["request"])
            try:
                if request["action"] == "environment-publish":
                    publication.current(context, conn, row["id"], fresh=not request["attempted"])
                elif request["action"] == "candidate-validate":
                    validation.current(context, conn, row["id"], fresh=not request["attempted"])
                elif request["action"] == "candidate-register":
                    registration.current(context, conn, row["id"], fresh=not request["attempted"])
                elif request["action"] == COLD_ACTION:
                    installation.current_cold(context, conn, row["id"], fresh=not request["attempted"])
                else:
                    installation.current(context, conn, row["id"], fresh=not request["attempted"])
            except (ValueError, RuntimeError, OSError, KeyError, TypeError):
                conn.execute(
                    "UPDATE environment_tasks SET state=?,error=?,updated_at=? WHERE id=?",
                    (
                        "cancelled"
                        if row["cancel"]
                        and request["action"] == installation.ACTION
                        and not request.get("dispatched")
                        else "blocked",
                        "ENVIRONMENT_TASK_OBSERVATION_REQUIRED",
                        time.time(),
                        row["id"],
                    ),
                )
                continue
            conn.execute(
                "UPDATE environment_tasks SET state='executing',owner=?,lease_until=?,updated_at=? "
                "WHERE id=?",
                (owner, time.time() + 900, time.time(), row["id"]),
            )
            return {"claimed": True, "id": row["id"], "action": request["action"]}
        return {"claimed": False, "task": None}


def prepare_workspace(context, payload):
    if set(payload) != {"id", "owner", "prepare"} or payload["prepare"] != "workspace":
        raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
    path, reference = _path(context), context.settings.environment_onboarding_file
    request = environment_tasks.workspace_request(path, reference, payload["id"], payload["owner"])
    settings = context.settings
    if request.get("candidate_parent"):
        with gateway_db.connection(environment_drafts._database(path), readonly=True) as conn:
            parent = json.loads(environment_tasks._row(conn, request["candidate_parent"])["request"])
        if (
            parent["bindings"]["workspace"]
            != environment_candidate_preparation._bindings(settings, parent["consumer_source"])["workspace"]
        ):
            raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_PLAN_EXPIRED")
    workspaces = DifyWorkspaces(settings.database_file)
    environment, tenant = request["plan"]["environment"], request["tenant_id"]
    workspace_id, state, error = "", "observed", ""
    try:
        existing = workspaces.row(tenant, environment)
        if request["cancel_requested"] and (existing is None or existing["setup_state"] == "failed"):
            raise EnsureError("ENVIRONMENT_TASK_CANCELLED", "创建前已取消")
        try:
            request = environment_tasks.workspace_request(
                path,
                reference,
                payload["id"],
                payload["owner"],
                reconciling=existing is not None and existing["setup_state"] != "failed",
            )
        except environment_tasks.TaskRefused as exc:
            if str(exc) not in {"ENVIRONMENT_TASK_PLAN_EXPIRED", "ENVIRONMENT_TASK_OBSERVATION_REQUIRED"}:
                raise
            raise EnsureError(str(exc), "首次创建前计划已过期") from exc
        if request["cancel_requested"] and (existing is None or existing["setup_state"] == "failed"):
            raise EnsureError("ENVIRONMENT_TASK_CANCELLED", "创建前已取消")
        outcome = ensure_workspace(
            tenant_id=tenant,
            environment=environment,
            workspaces=workspaces,
            inner=DifyInnerApi(
                InnerApiSettings(settings.dify_inner_api_base_url, settings.dify_inner_api_key)
            ),
            console_base_url=settings.dify_console_base_url,
            console_api_key=settings.dify_console_api_key,
            # 新候选用完整复合编号，避免同公司名、同编号尾部碰撞；旧映射按保存名字复用。
            company="",
            owner=payload["id"],
            reconcile_unknown_only=True,
        )
        if not outcome.workspace_id:
            raise EnsureError("DIFY_WORKSPACE_CREATE_UNCONFIRMED", "工作区未确认")
        row = workspaces.row(tenant, environment)
        matches = reconcile_by_name(
            console_base_url=settings.dify_console_base_url,
            console_api_key=settings.dify_console_api_key,
            name=row["workspace_name"],
            workspace_id=outcome.workspace_id if not row["workspace_name"] else "",
        )
        if len(matches) > 1:
            raise EnsureError("DIFY_WORKSPACE_AMBIGUOUS", "同名工作区不唯一，需人工核对")
        if len(matches) != 1 or matches[0].get("id") != outcome.workspace_id:
            raise EnsureError("DIFY_WORKSPACE_CREATE_UNCONFIRMED", "工作区归属未确认")
        if (
            workspaces.tenant_for_workspace(outcome.workspace_id, environment, across_environments=True)
            != tenant
        ):
            raise EnsureError("DIFY_WORKSPACE_AMBIGUOUS", "工作区映射归属不唯一")
        if not row["workspace_name"]:
            workspaces.remember_name(tenant, environment, outcome.workspace_id, matches[0]["name"])
        workspace_id = outcome.workspace_id
    except (EnsureError, DifyWorkspaceError) as exc:
        row = workspaces.row(tenant, environment)
        if exc.code in {
            "DIFY_WORKSPACE_AMBIGUOUS",
            "DIFY_CONSOLE_KEY_REJECTED",
            "DIFY_RECONCILE_TOO_LARGE",
            "ENVIRONMENT_TASK_CANCELLED",
            "ENVIRONMENT_TASK_PLAN_EXPIRED",
            "ENVIRONMENT_TASK_OBSERVATION_REQUIRED",
        } or (row and row["setup_state"] == "failed"):
            state, error = "blocked", exc.code
        else:
            return environment_tasks.worker(
                path,
                reference,
                {
                    "id": payload["id"],
                    "owner": payload["owner"],
                    "error": "ENVIRONMENT_TASK_TARGET_UNCONFIRMED",
                },
            )
    now = time.time()
    result = {
        "scope": "candidate-workspace-only",
        "environment": environment,
        "tenant_id": tenant,
        "workspace_id": workspace_id,
        "state": state,
        "error_code": error,
        "executable": False,
        "created_at": now,
        "expires_at": now + 900,
        "checks": [
            {"id": "workspace", "state": "passed" if state == "observed" else "blocked"},
            *[{"id": k, "state": "pending"} for k in ("application", "knowledge", "business")],
        ],
    }
    return environment_tasks.finish_workspace(path, reference, payload["id"], payload["owner"], result)


def wizard(context, session, payload):
    """V2 自助开通向导统一入口（ADR-0358）：覆盖全部 13 项白名单动作并严守零明文持久化边界。"""
    return wizard_dispatch.dispatch_wizard(
        context,
        session,
        payload,
        access_fn=_access,
        path_fn=_path,
        identity_fn=_identity,
    )


def register(app, context, session_dependency):
    @app.post("/v1/dify/management/environment-wizard")
    def manage_wizard(payload: dict, response: Response, session=Depends(session_dependency)):  # noqa: B008
        response.headers["Cache-Control"] = "no-store"
        return wizard(context, session, payload)

    @app.get("/v1/internal/environment-bootstrap/script/{token}")
    def bootstrap_script(token: str, request: Request, response: Response):
        path = _path(context)
        if not path:
            return JSONResponse(
                status_code=404, content={"error": {"code": "ENVIRONMENT_BOOTSTRAP_UNAVAILABLE"}}
            )
        callback_base = str(request.base_url).rstrip("/")
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        if forwarded_proto == "https" and callback_base.startswith("http://"):
            callback_base = "https://" + callback_base[len("http://") :]
        try:
            script = host_enrollment.render_bootstrap_script_for_token(path, token, callback_base)
        except host_enrollment.EnrollmentRefused as exc:
            code = str(exc)
            st = 410 if code == "ENVIRONMENT_BOOTSTRAP_TOKEN_EXPIRED_OR_USED" else 400
            return JSONResponse(status_code=st, content={"error": {"code": code}})
        return PlainTextResponse(
            content=script,
            media_type="text/x-shellscript",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/internal/environment-bootstrap/callback")
    def bootstrap_callback(payload: dict, request: Request, response: Response):
        path = _path(context)
        if not path:
            return JSONResponse(
                status_code=404, content={"error": {"code": "ENVIRONMENT_BOOTSTRAP_UNAVAILABLE"}}
            )
        response.headers["Cache-Control"] = "no-store"
        token = request.headers.get("X-Bootstrap-Token", "").strip()
        try:
            return host_enrollment.complete_bootstrap_callback(path, token, payload)
        except host_enrollment.EnrollmentRefused as exc:
            code = str(exc)
            st = (
                409
                if code == "ENVIRONMENT_HOST_FINGERPRINT_MISMATCH"
                else 410
                if code == "ENVIRONMENT_BOOTSTRAP_TOKEN_EXPIRED_OR_USED"
                else 400
            )
            return JSONResponse(status_code=st, content={"error": {"code": code}})

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
            if "publication" in payload:
                return publication.dispatch(context, payload)
            if "validation" in payload:
                return validation.dispatch(context, payload)
            if "registration" in payload:
                return registration.dispatch(context, payload)
            if "wizard_job" in payload:
                from aiops_diagnostics.onboarding import wizard_jobs, wizard_provision

                job_op = str(payload.get("wizard_job") or "")
                reg_path = _path(context)
                if job_op == "claim":
                    return {
                        "job": wizard_jobs.claim_next_job(
                            reg_path, lease_seconds=float(payload.get("lease_seconds") or 600.0)
                        )
                    }
                if job_op == "progress":
                    return wizard_jobs.update_job_progress(
                        reg_path,
                        str(payload.get("job_id") or ""),
                        step=str(payload.get("step") or ""),
                        step_state=str(payload.get("step_state") or "running"),
                        steps_payload=payload.get("steps"),
                        result_patch=payload.get("result_patch"),
                    )
                if job_op == "complete":
                    return wizard_jobs.complete_job(
                        reg_path,
                        str(payload.get("job_id") or ""),
                        passed=bool(payload.get("passed")),
                        result=dict(payload.get("result") or {}),
                        error_code=str(payload.get("error_code") or ""),
                        failed_step=str(payload.get("failed_step") or ""),
                        steps_payload=payload.get("steps"),
                    )
                if job_op == "prepare_bundle":
                    return wizard_provision.prepare_control_plane_bundle(context, reg_path, payload)
                if job_op == "connect_consumer_plugin":
                    return wizard_provision.connect_consumer_plugin(context, reg_path, payload)
                if job_op == "activate_registry":
                    return wizard_provision.activate_environment_registry(context, reg_path, payload)
                if job_op == "save_release_catalog":
                    from aiops_diagnostics.onboarding import release_governance

                    assets_dir = release_governance.assets_dir_for_registry(reg_path)
                    return release_governance.save_release_catalog(
                        assets_dir, list(payload.get("entries") or [])
                    )
                if job_op == "save_release_state":
                    from aiops_diagnostics.onboarding import release_governance

                    assets_dir = release_governance.assets_dir_for_registry(reg_path)
                    return release_governance.save_environment_release_state(
                        assets_dir,
                        str(payload.get("environment") or ""),
                        dict(payload.get("state") or {}),
                    )
                raise environment_tasks.TaskRefused("ENVIRONMENT_TASK_WORKER_INVALID")
            if "installation_claim" in payload:
                return dispatch_installation_claim(context, payload)
            if "installation" in payload:
                return installation.dispatch(context, payload)
            if "advance_candidates" in payload:
                prep_res = environment_candidate_preparation.advance(context, payload)
                onboard_res = environment_onboarding.advance(context)
                return {
                    "advanced": prep_res["advanced"] + onboard_res["advanced"],
                    "turn": prep_res["turn"],
                }
            if "prepare" in payload:
                if payload["prepare"] in {"operator", "consumer"}:
                    return operator_prep.run(
                        context, _path(context), context.settings.environment_onboarding_file, payload
                    )
                if payload["prepare"] == "connection":
                    return knowledge_connection.run(
                        context, _path(context), context.settings.environment_onboarding_file, payload
                    )
                if payload["prepare"] == "knowledge":
                    return knowledge.run(
                        context, _path(context), context.settings.environment_onboarding_file, payload
                    )
                return prepare_workspace(context, payload)
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

    @app.get("/v1/dify/management/environment-overview")
    def read_overview(response: Response, session=Depends(session_dependency)):  # noqa: B008
        response.headers["Cache-Control"] = "no-store"
        return overview(context, session)

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
