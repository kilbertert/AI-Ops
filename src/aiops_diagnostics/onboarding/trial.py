"""停用目标试运行内部路由入口；叶子运行时原语位于 trial_runtime。"""

import asyncio
import ipaddress
import json

from fastapi import Request
from starlette.responses import JSONResponse

from aiops_diagnostics.common.http_auth import validate_internal_token
from aiops_diagnostics.onboarding.trial_runtime import (
    TrialRefused as TrialRefused,
)
from aiops_diagnostics.onboarding.trial_runtime import (
    identity as identity,
)
from aiops_diagnostics.onboarding.trial_runtime import (
    read as read,
)
from aiops_diagnostics.onboarding.trial_runtime import (
    start as start,
)
from aiops_diagnostics.onboarding.trial_runtime import (
    target as target,
)
from aiops_diagnostics.operator.operator_cpms import OperatorRefused


def register(app, context):
    from aiops_diagnostics.onboarding.candidate_trial import CandidateLeases
    from aiops_diagnostics.onboarding.consumer_trial import CandidateTrials

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
            from aiops_diagnostics.onboarding.registry import _unique_object

            payload = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(payload, dict):
                raise TrialRefused("ENVIRONMENT_TRIAL_INVALID")
            action = payload.pop("action")
            if not isinstance(action, str):
                raise TrialRefused("ENVIRONMENT_TRIAL_INVALID")
            if action in {
                "publication-trial-start",
                "publication-trial-read",
                "publication-trial-complete",
                "publication-operator-start",
            }:
                from aiops_diagnostics.onboarding import release_trial as environment_release_trial

                if action == "publication-operator-start":
                    result = await asyncio.to_thread(
                        environment_release_trial.start_operator, context, payload
                    )
                    return JSONResponse(result, headers={"Cache-Control": "no-store"})
                result = await asyncio.to_thread(
                    {
                        "publication-trial-start": environment_release_trial.start,
                        "publication-trial-read": environment_release_trial.read,
                        "publication-trial-complete": environment_release_trial.complete,
                    }[action],
                    context,
                    payload,
                )
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            if action == "publication-run-window":
                from aiops_diagnostics.onboarding.capacity import run_window

                result = await asyncio.to_thread(run_window, context, payload)
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            if action == "publication-readiness":
                from aiops_diagnostics.onboarding.readiness import check

                result = await asyncio.to_thread(check, context, payload["spec"])
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            if action == "business-step":
                from aiops_diagnostics.onboarding.business import ValidationRefused, step

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
                        "error_code": str(exc),
                    }
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            if action == "consumer-validation-evidence":
                from aiops_diagnostics.onboarding.business import ValidationRefused
                from aiops_diagnostics.onboarding.consumer_trial import validation_evidence

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
            if action == "consumer-run-started":
                from aiops_diagnostics.onboarding.consumer_trial import run_started

                result = await asyncio.to_thread(run_started, context, payload)
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
