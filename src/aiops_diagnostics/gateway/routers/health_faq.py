"""健康检查与固定 FAQ 问答路由（无需创建作业、严格只读确定性文案）。"""

from __future__ import annotations

import platform
from typing import Any

from fastapi import Depends, FastAPI, status

from aiops_diagnostics import __version__
from aiops_diagnostics.conversation.faq import FAQ_NOT_FOUND, FAQError
from aiops_diagnostics.gateway.dependencies import GatewayDeps, localized_copy_language
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.schemas import FAQAnswerRequest
from aiops_diagnostics.identity.scope_context import ScopeContext


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:
    """挂载 ``/health`` 与 ``/v1/faq/*`` 路由。"""
    del api

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "aiops-gateway",
            "version": __version__,
            "api_version": "v1",
            "platform": platform.system().lower(),
            "business_mutations": "disabled",
        }

    @app.get("/v1/faq/recommendations")
    def faq_recommendations(
        identity: tuple[ScopeContext, Any] = Depends(deps.faq_identity),  # noqa: B008
        language: str = Depends(localized_copy_language),  # noqa: B008
    ) -> dict[str, Any]:
        _, decision = identity
        return {
            **decision.public(),
            "language": context.faq_catalog.served_language(decision.platform, language),
            "faq_version": context.faq_catalog.version,
            "recommendations": context.faq_catalog.recommendations(decision.platform, language),
        }

    @app.get("/v1/faq/catalog")
    def faq_catalog(
        identity: tuple[ScopeContext, Any] = Depends(deps.faq_identity),  # noqa: B008
        language: str = Depends(localized_copy_language),  # noqa: B008
    ) -> dict[str, Any]:
        _, decision = identity
        return {
            **decision.public(),
            "language": context.faq_catalog.served_language(decision.platform, language),
            "faq_version": context.faq_catalog.version,
            "entries": context.faq_catalog.catalog(decision.platform, language),
        }

    @app.post("/v1/faq/answer")
    def faq_answer(
        payload: FAQAnswerRequest,
        identity: tuple[ScopeContext, Any] = Depends(deps.faq_identity),  # noqa: B008
        language: str = Depends(localized_copy_language),  # noqa: B008
    ) -> dict[str, Any]:
        _, decision = identity
        try:
            answer = context.faq_catalog.answer(decision.platform, payload.question_id, language)
        except FAQError as exc:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, FAQ_NOT_FOUND, "FAQ question was not found"
            ) from exc
        return {
            **decision.public(),
            "language": context.faq_catalog.entry_served_language(
                decision.platform, payload.question_id, language
            ),
            "faq_version": context.faq_catalog.version,
            "question_id": answer["question_id"],
            "question": answer["question"],
            "answer": answer["answer"],
            "format": answer["format"],
        }
