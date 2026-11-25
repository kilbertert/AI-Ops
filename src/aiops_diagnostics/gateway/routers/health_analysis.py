"""充电健康报告作业与管家运维分析报告卡路由。"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, status

from aiops_diagnostics.common.scalar_rules import format_stored_time
from aiops_diagnostics.conversation.faq import PLATFORM_OPERATOR
from aiops_diagnostics.gateway.dependencies import GatewayDeps, request_language
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.schemas import HealthReportJobRequest, OperationsAnalysisRequest
from aiops_diagnostics.identity.scope_context import ScopeContext


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:

    @app.post("/v1/health-report-jobs", status_code=status.HTTP_202_ACCEPTED)
    def create_health_report_job(
        payload: HealthReportJobRequest,
        caller: ScopeContext = Depends(deps.authenticated_caller),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        verdict = api._order_authorization(context, caller, payload.order_no)
        if verdict == api.UNAVAILABLE:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ORDER_AUTHORIZATION_UNAVAILABLE",
                "order authorization unavailable",
                retryable=True,
            )
        if verdict == api.NOT_OWNED:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ORDER_NOT_FOUND",
                "order not found",
            )
        try:
            job = context.runtime.start_health_report(caller, payload.order_no, language=language)
        except (ValueError, RuntimeError) as exc:
            raise api._runtime_unavailable(
                "REPORT_JOB_UNAVAILABLE", "health report job unavailable", exc
            ) from exc
        return api._health_job_response(job)

    @app.get("/v1/health-report-jobs/{job_id}")
    def get_health_report_job(
        job_id: str,
        caller: ScopeContext = Depends(deps.authenticated_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            job = context.runtime.get_health_report(caller, job_id)
        except (ValueError, RuntimeError) as exc:
            raise api._runtime_unavailable(
                "REPORT_JOB_UNAVAILABLE", "health report job unavailable", exc
            ) from exc
        if job is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "REPORT_JOB_NOT_FOUND",
                "health report job not found",
            )
        return api._health_job_response(job)

    @app.post("/v1/standard/analyses", status_code=status.HTTP_202_ACCEPTED)
    def create_operations_analysis(
        payload: OperationsAnalysisRequest,
        identity: tuple[ScopeContext, Any] = Depends(deps.operator_diagnosis_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """管家端专用运维分析面：站点集合 + 时间窗 → 一张确定性报告卡。"""
        caller, decision = identity
        if str(decision.platform) != PLATFORM_OPERATOR:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN,
                "OPERATOR_ENTRY_REQUIRED",
                "operator entry required",
            )
        site_ids = tuple(payload.site_ids) if payload.site_ids is not None else None
        try:
            if payload.card == "failure_rate":
                job = context.runtime.start_operations_analysis(
                    caller,
                    site_ids=site_ids,
                    window_start=format_stored_time(payload.window_start),
                    window_end=format_stored_time(payload.window_end),
                    language=language,
                )
            else:
                job = context.runtime.start_operations_card(
                    caller,
                    card=payload.card,
                    site_ids=site_ids,
                    window_start=format_stored_time(payload.window_start),
                    window_end=format_stored_time(payload.window_end),
                    language=language,
                )
        except (ValueError, RuntimeError) as exc:
            raise api._runtime_unavailable("ANALYSIS_UNAVAILABLE", "analysis unavailable", exc) from exc
        return api._analysis_job_response(job, language)

    @app.get("/v1/standard/analyses/{analysis_id}")
    def get_operations_analysis(
        analysis_id: str,
        identity: tuple[ScopeContext, Any] = Depends(deps.operator_diagnosis_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        caller, decision = identity
        if str(decision.platform) != PLATFORM_OPERATOR:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN,
                "OPERATOR_ENTRY_REQUIRED",
                "operator entry required",
            )
        try:
            job = context.runtime.get_operations_analysis(caller, analysis_id)
        except (ValueError, RuntimeError) as exc:
            raise api._runtime_unavailable("ANALYSIS_UNAVAILABLE", "analysis unavailable", exc) from exc
        if job is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ANALYSIS_NOT_FOUND",
                "analysis not found",
            )
        return api._analysis_job_response(job, language)
