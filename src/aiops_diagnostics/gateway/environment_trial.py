"""停用目标的受管管家试运行；正常身份与工件限定本次任务，不改变普通准入。"""

import asyncio
import hashlib
import ipaddress
import json
import time
from pathlib import Path

from fastapi import Request
from starlette.responses import JSONResponse

from aiops_diagnostics.common.bounded_http import (
    ErrorMapping,
    RequestSpec,
    parse_code_data_envelope,
    request_json,
)
from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.common.http_auth import validate_internal_token
from aiops_diagnostics.dify.dify_trial import TrialInitiator, build_trial_scope
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.dify.self_service_store import SelfServiceStore
from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.execution.node_capabilities import require_compatible
from aiops_diagnostics.execution.node_publication import all_targets
from aiops_diagnostics.gateway.environment_artifacts import runtime_files
from aiops_diagnostics.gateway.environment_projection import validate_candidate
from aiops_diagnostics.gateway.metrics_store import ROUTE_TRIAL
from aiops_diagnostics.operator.operator_cpms import OperatorRefused


class TrialRefused(OperatorRefused):
    pass


def identity(context, bearer):
    def refused(_):
        return TrialRefused("ENVIRONMENT_TRIAL_LOGIN_REQUIRED")

    base = Settings.from_config(context.settings.server_config_file).upms.base_url
    body = request_json(
        RequestSpec(
            url=base.rstrip("/") + "/user/info",
            headers={"Authorization": "Bearer " + bearer, "client-type": "admin"},
            timeout=8,
            max_read_bytes=1048576,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(
            auth_rejected=refused,
            http_error=refused,
            unavailable=refused,
            invalid_body=refused,
            invalid_envelope=refused,
        ),
        envelope=parse_code_data_envelope,
    )
    user = body.get("sysUser") if isinstance(body, dict) else None
    if (
        not isinstance(user, dict)
        or not user.get("id")
        or not user.get("tenantId")
        or str(user["tenantId"]) == TrialInitiator.PLATFORM_TENANT_SENTINEL
    ):
        raise TrialRefused("ENVIRONMENT_TRIAL_LOGIN_REQUIRED")
    initiator = TrialInitiator(
        str(user["id"]), str(user["tenantId"]), str(user.get("type") or ""), str(user.get("username") or "")
    )
    return build_trial_scope(initiator, operator_scope=context.runtime._trial_operator_scope)


def target(context, app_id, expected_sha256):
    settings = context.settings
    root = Path(f"/etc/aiops-access-{settings.environment}")
    installation = Path(f"/opt/aiops-access-{settings.environment}")
    if (
        not settings.server_config_file
        or Path(settings.server_config_file) != root / "production.env"
        or settings.environment_registry_file != str(root / "registry/environment.json")
        or settings.operator_profiles_file != str(root / "runtime/operator-profiles.json")
        or settings.execution_environment != settings.environment
        or (installation / "pending.json").exists()
        or not (installation / "current/manifest.json").is_file()
    ):
        raise TrialRefused("ENVIRONMENT_TRIAL_NOT_INSTALLED")
    build = json.loads((installation / "current/manifest.json").read_text())
    if build["version"] != __import__("aiops_diagnostics").__version__:
        raise TrialRefused("ENVIRONMENT_TRIAL_NOT_INSTALLED")
    validate_candidate(
        root / "production.env",
        root / "registry/environment.json",
        settings.environment,
        runtime=root / "runtime",
    )
    files, manifest = runtime_files(root / "runtime/runtime.tar", expected_sha256, settings.environment)
    registration = json.loads(files["registrations.json"])
    if registration["operator_app_id"] != app_id:
        raise TrialRefused("ENVIRONMENT_TRIAL_APP_MISMATCH")
    row = SelfServiceStore(settings.database_file).app(app_id)
    if (
        not row
        or row["entry"] != "operator"
        or row["environment"] != settings.environment
        or row["tenant_id"] != manifest["tenant_id"]
        or row["workspace_id"] != registration["workspace_id"]
        or row["profile_id"] != "operator-" + settings.environment
        or row["revision"] != 1
        or row["created_by"] != "environment-runtime"
    ):
        raise TrialRefused("ENVIRONMENT_TRIAL_APP_MISMATCH")
    workspace = DifyWorkspaces(settings.database_file).row(manifest["tenant_id"], settings.environment)
    consumer = SelfServiceStore(settings.database_file).app(registration["consumer_app_id"])
    if (
        not workspace
        or workspace["workspace_id"] != registration["workspace_id"]
        or workspace["setup_state"] != "created"
        or not consumer
        or any(
            consumer[k] != expected
            for k, expected in {
                "tenant_id": manifest["tenant_id"],
                "environment": settings.environment,
                "workspace_id": registration["workspace_id"],
                "entry": "consumer",
                "created_by": "environment-runtime",
                "revision": 1,
            }.items()
        )
    ):
        raise TrialRefused("ENVIRONMENT_TRIAL_APP_MISMATCH")
    expected = next(
        (t for t in all_targets(settings) if t.app_id == app_id and t.environment == settings.environment),
        None,
    )
    if expected is None:
        raise TrialRefused("ENVIRONMENT_TRIAL_APP_MISMATCH")
    require_compatible(settings.database_file, expected, json.loads(files["operator-manifest.json"]))
    binding = chat_execution.trial_binding(settings, app_id)
    fingerprint = tuple(
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            root / "production.env",
            root / "registry/environment.json",
            installation / "current/manifest.json",
        )
    )
    return expected, binding, files["operator-manifest.json"].decode(), row, fingerprint, workspace, consumer


def start(context, payload):
    from aiops_diagnostics.gateway.gateway_runtime import JobRegistration

    if (
        set(payload) != {"app_id", "runtime_sha256", "company_token", "question"}
        or any(not isinstance(payload[k], str) or not payload[k] for k in payload)
        or len(payload["question"]) > 4000
        or len(payload["company_token"]) > 8192
    ):
        raise TrialRefused("ENVIRONMENT_TRIAL_INVALID")
    app_id, sha, bearer = payload["app_id"], payload["runtime_sha256"], payload["company_token"]
    snapshot = target(context, app_id, sha)
    expected, binding, manifest, row = snapshot[:4]
    caller = identity(context, bearer)
    if caller.effective_tenant_id != row["tenant_id"]:
        raise TrialRefused("ENVIRONMENT_TRIAL_SCOPE_MISMATCH")
    end = time.monotonic() + 300

    def authorize():
        if time.monotonic() >= end:
            raise TrialRefused("ENVIRONMENT_TRIAL_EXPIRED")
        try:
            current = target(context, app_id, sha)
            fresh = identity(context, bearer)
        except (ValueError, RuntimeError, OSError) as exc:
            raise TrialRefused("ENVIRONMENT_TRIAL_CHANGED") from exc
        if current != snapshot or fresh.scope_fingerprint != caller.scope_fingerprint:
            raise TrialRefused("ENVIRONMENT_TRIAL_CHANGED")
        return fresh

    qa = context.operator_cpms.start(
        caller,
        binding,
        "",
        payload["question"],
        bearer=bearer,
        authorize=authorize,
        base=expected.cpms_base_url,
        proxy=expected.proxy_base_url,
        registration=JobRegistration(route_type=ROUTE_TRIAL),
        trial_manifest=manifest,
        candidate=True,
    )
    return {
        "trial_id": qa["qa_id"],
        "status": qa["status"],
        "environment": binding.environment,
        "scope": "inactive-operator-business-trial",
        "activated": False,
    }


def read(context, payload):
    if set(payload) != {"app_id", "trial_id"} or any(not isinstance(v, str) for v in payload.values()):
        raise TrialRefused("ENVIRONMENT_TRIAL_INVALID")
    run = context.agent_run_store.by_qa(payload["trial_id"])
    with context.operator_cpms.lock:
        context.operator_cpms._prune()
        lease = context.operator_cpms.candidate_authorizations.get(run.run_id) if run else None
    if not lease or run.publication_app_id != payload["app_id"]:
        raise TrialRefused("ENVIRONMENT_TRIAL_NOT_FOUND")
    authorize = lease[0]
    caller = authorize()
    qa = context.store.get_assistant_question(payload["trial_id"], caller.scope_fingerprint)
    if not qa:
        raise TrialRefused("ENVIRONMENT_TRIAL_NOT_FOUND")
    result = None
    if qa["status"] == "completed":
        result = context.operator_cpms.authorize_readback(
            caller, payload["trial_id"], lease[1], candidate_authorize=authorize
        )
    return {
        "trial_id": payload["trial_id"],
        "status": qa["status"],
        "result": result,
        "error_code": qa.get("error_code"),
        "scope": "inactive-operator-business-trial",
        "activated": False,
    }


def register(app, context):
    from aiops_diagnostics.consumer.candidate_trial import CandidateLeases
    from aiops_diagnostics.dify.environment_consumer_trial import CandidateTrials

    context.consumer_candidate_leases = CandidateLeases(context)
    context.consumer_candidate_trials = CandidateTrials(context)
    context.runtime.consumer_candidate_trials = context.consumer_candidate_trials

    @app.post("/v1/internal/environment-trial")
    async def manage_trial(request: Request):
        key = context.settings.environment_task_key
        if not key or not (
            context.settings.environment_registry_file
            or getattr(context.settings, "environment_management_registry_file", "")
        ):
            return JSONResponse(status_code=404, content={"error": {"code": "ENVIRONMENT_TRIAL_DISABLED"}})
        try:
            local = request.client and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if not local or not validate_internal_token(
            request.headers.get("X-Internal-Token"), request.headers.get("X-Request-Timestamp"), key
        ):
            return JSONResponse(
                status_code=401, content={"error": {"code": "ENVIRONMENT_TRIAL_CREDENTIAL_REQUIRED"}}
            )
        raw = bytearray()
        try:
            async with asyncio.timeout(15):
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 16384:
                        return JSONResponse(
                            status_code=413, content={"error": {"code": "ENVIRONMENT_TRIAL_INVALID"}}
                        )
            from aiops_diagnostics.gateway.environment_registry import _unique_object

            payload = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(payload, dict):
                raise TrialRefused("ENVIRONMENT_TRIAL_INVALID")
            action = payload.pop("action")
            if not isinstance(action, str):
                raise TrialRefused("ENVIRONMENT_TRIAL_INVALID")
            if action == "business-step":
                from .environment_business import ValidationRefused, step

                try:
                    result = await asyncio.to_thread(
                        step, context, payload, start_trial=start, read_trial=read, resolve_identity=identity
                    )
                except ValidationRefused as exc:
                    result = {
                        "capability": payload.get("capability"),
                        "state": "blocked",
                        "error_code": exc.code,
                    }
                except OperatorRefused as exc:
                    result = {
                        "capability": payload.get("capability"),
                        "state": "blocked",
                        "error_code": exc.code,
                    }
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            if action == "consumer-validation-evidence":
                from aiops_diagnostics.dify.environment_consumer_trial import validation_evidence

                from .environment_business import ValidationRefused

                try:
                    result = await asyncio.to_thread(validation_evidence, context, payload)
                except ValidationRefused as exc:
                    result = {
                        "status": "failed",
                        "capability": "consumer",
                        "state": "blocked",
                        "error_code": exc.code,
                    }
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            if action.startswith("consumer-"):
                operations = {
                    "consumer-describe": context.consumer_candidate_trials.describe,
                    "consumer-open": context.consumer_candidate_leases.open,
                    "consumer-start": context.consumer_candidate_trials.start,
                    "consumer-read": context.consumer_candidate_trials.read,
                    "consumer-cancel": lambda value: context.consumer_candidate_trials.read(
                        value, cancel=True
                    ),
                }
                if action not in operations:
                    raise TrialRefused("ENVIRONMENT_TRIAL_INVALID")
                result = await asyncio.to_thread(operations[action], payload)
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            result = (
                await asyncio.to_thread(start, context, payload)
                if action == "start"
                else await asyncio.to_thread(read, context, payload)
                if action == "read"
                else None
            )
            if result is None:
                raise TrialRefused("ENVIRONMENT_TRIAL_INVALID")
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError, KeyError, RuntimeError, OSError, TimeoutError):
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "ENVIRONMENT_TRIAL_REFUSED"}},
                headers={"Cache-Control": "no-store"},
            )
