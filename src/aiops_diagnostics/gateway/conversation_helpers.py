"""Gateway 会话轮次认领/释放、订单归属守卫与运维复盘快捷启动辅助。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import status
from fastapi.responses import JSONResponse

from aiops_diagnostics.common.scalar_rules import format_stored_time
from aiops_diagnostics.conversation.conversation_store import ConversationBusy, ConversationError
from aiops_diagnostics.data.sources import SourceError
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    NOT_OWNED,
    ORDER_AUTH_ROUTE,
    ORDER_AUTHENTICATION_UNAVAILABLE,
    OWNED,
    UNAVAILABLE,
)
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.response_views import (
    _analysis_job_response,
    _record_route_metric,
    _runtime_unavailable,
)
from aiops_diagnostics.gateway.schemas import AssistantQuestionRequest
from aiops_diagnostics.identity.caller_auth import CallerAuthError
from aiops_diagnostics.identity.scope_context import ScopeContext, ScopeError
from aiops_diagnostics.operator.operator_cpms import OperatorRefused
from aiops_diagnostics.publication.shortcut_lifecycle import OPERATIONS_REVIEW_CODE

_LOGGER = logging.getLogger("aiops.gateway")

#: How far back a *clicked* operations-review action looks. The server owns
#: this number because a window is a 口径: if the frontend picks it, there is
#: one definition per client and two clients can disagree about the same card.
#: Callers that must choose a window use ``POST /v1/standard/analyses``, which
#: takes it explicitly.
#:
#: 24 hours is a **engineering default, not a business calibration** — nothing
#: in the repo has ever defined a review window, and changing it is this line.
OPERATIONS_REVIEW_WINDOW_HOURS = 24


def _require_conversation(
    context: Any,
    caller: ScopeContext,
    conversation_id: str,
) -> dict[str, Any]:
    """Load a scope-owned conversation or raise the uniform 404."""
    try:
        conversation = context.conversation_store.get(conversation_id, caller.scope_fingerprint)
        if conversation.get("operator_binding", "").endswith(":preview"):
            raise ConversationError("preview conversation is not a formal conversation")
        return conversation
    except ConversationError as exc:
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "CONVERSATION_NOT_FOUND",
            "conversation not found",
        ) from exc


def _operator_conversation(
    context: Any,
    caller: ScopeContext,
    binding: Any,
    conversation: dict[str, Any] | None,
    *,
    trial: bool,
    agent_id: str = "operator_main",
) -> dict[str, Any]:
    key = f"{binding.key}:{binding.app_id}:{'preview' if trial else 'formal'}"
    resolved_version_agent = binding.agent_id or "agt_" + hashlib.sha256(key.encode()).hexdigest()[:32]
    version = f"{resolved_version_agent}#v{binding.agent_version_no}"
    if conversation is None:
        return context.conversation_store.create(
            scope_fingerprint=caller.scope_fingerprint,
            business_entry="operator",
            agent_version_key=version,
            operator_binding=key,
            agent_id=agent_id,
        )
    if conversation.get("operator_binding") != key:
        raise StandardAPIError(404, "CONVERSATION_NOT_FOUND", "conversation not found")
    return conversation


def _resolve_conversation(
    context: Any,
    caller: ScopeContext,
    decision: Any,
    conversation_id: str,
) -> dict[str, Any]:
    """Load the conversation for this caller, enforcing scope AND entry.

    Entry is part of the binding (T4/#172): the same user switching from the
    consumer entry to the operator entry gets a fresh conversation set, and a
    mismatched or missing conversation is a uniform 404 — indistinguishable
    from never-existing.
    """
    try:
        conversation = context.conversation_store.get(conversation_id, caller.scope_fingerprint)
    except ConversationError as exc:
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "CONVERSATION_NOT_FOUND",
            "conversation not found",
        ) from exc
    if conversation.get("business_entry") != str(getattr(decision, "platform", "")):
        # The platform decision IS the resolved business entry for this call.
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "CONVERSATION_NOT_FOUND",
            "conversation not found",
        )
    return conversation


def _require_operator_company_token(request: Any) -> None:
    if request.headers.get("X-Third-Session") or request.headers.get("third-session"):
        raise StandardAPIError(403, "OPERATOR_COMPANY_TOKEN_REQUIRED", "use the company operator login")
    if request.headers.get("X-Business-Entry", "").strip().lower() != "operator":
        raise StandardAPIError(403, "OPERATOR_COMPANY_TOKEN_REQUIRED", "use the company operator login")


async def _operator_json(request: Any) -> dict[str, Any]:
    raw = bytearray()
    try:
        async with asyncio.timeout(15):
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 262144:
                    raise OperatorRefused("OPERATOR_BODY_TOO_LARGE")
    except TimeoutError as exc:
        raise OperatorRefused("OPERATOR_BODY_TIMEOUT") from exc
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise OperatorRefused("OPERATOR_INVALID_BODY") from exc
    if not isinstance(body, dict):
        raise OperatorRefused("OPERATOR_INVALID_BODY")
    return body


def _order_authorization(
    context: Any,
    caller: ScopeContext,
    order_no: str,
) -> str:
    """Ask whether this caller may see this order: `OWNED` / `NOT_OWNED` / `UNAVAILABLE`.

    One place decides what the answer means, and one place records the case that
    used to be invisible. An order-authorization *failure* (a down directory, an
    unreachable source, a defect in the authorizer) and an ordinary "this caller
    does not own this order" were both `False`: from the logs and the metrics,
    a live authorization outage looked exactly like a batch of users asking for
    orders that are not theirs. The failing case now warns with an error code and
    records a metric row.

    The seven call sites keep their own *behaviour* — five refuse, two fall back
    — because that difference is a product decision each surface made. What they
    no longer do is re-decide what the *authorizer's* answer means.
    """
    try:
        allowed = context.order_authorizer.can_access(caller, order_no)
    except (CallerAuthError, ScopeError, SourceError, ValueError) as exc:
        # The code is in the message, not only in the metric: an operator greps
        # the log for the same string the contract and the metrics use.
        _LOGGER.warning("%s (check failed): %s", ORDER_AUTHENTICATION_UNAVAILABLE, type(exc).__name__)
        _record_route_metric(
            context,
            caller,
            route_type=ORDER_AUTH_ROUTE,
            outcome="failed",
            error_code=ORDER_AUTHENTICATION_UNAVAILABLE,
        )
        return UNAVAILABLE
    return OWNED if allowed else NOT_OWNED


def _require_visible_order(context: Any, caller: ScopeContext, order_no: str) -> None:
    """既有订单可见性判据；越权与不存在同形。返回 None，失败即抛。"""
    verdict = _order_authorization(context, caller, order_no)
    if verdict == UNAVAILABLE:
        raise StandardAPIError(
            503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order authorization unavailable", retryable=True
        )
    if verdict == NOT_OWNED:
        raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")


def _begin_conversation_turn(
    context: Any,
    caller: ScopeContext | None,
    conversation: dict[str, Any] | None,
    kind: str,
    question: str,
    *,
    attachment_ids: list[str] | None = None,
    claim_id: str | None = None,
) -> int | None:
    """Claim the conversation's generation slot; 409 when busy."""
    if conversation is None:
        return None
    try:
        return context.conversation_store.begin_turn(
            conversation["conversation_id"],
            conversation["scope_fingerprint"],
            kind=kind,
            question=question,
            attachment_ids=attachment_ids,
            claim_id=claim_id,
        )
    except ConversationBusy as exc:
        _record_route_metric(
            context,
            caller,
            route_type=kind,  # "qa" | "diagnosis" — never misattribute a busy turn
            outcome="busy",
            error_code="CONVERSATION_BUSY",
            conversation_id=conversation.get("conversation_id"),
        )
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "CONVERSATION_BUSY",
            "this conversation is already generating a reply",
            retryable=False,
        ) from exc
    except ConversationError as exc:
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "CONVERSATION_NOT_FOUND",
            "conversation not found",
        ) from exc


def _release_conversation_turn(
    context: Any,
    conversation: dict[str, Any] | None,
    turn_no: int | None,
    *,
    preserve_completed: bool = False,
    claim_id: str | None = None,
) -> None:
    """Drop an unfinished turn row and free the slot (best effort)."""
    if conversation is None or turn_no is None:
        return
    with contextlib.suppress(ConversationError):
        context.conversation_store.release_turn(
            conversation["conversation_id"],
            conversation["scope_fingerprint"],
            turn_no,
            preserve_completed=preserve_completed,
            claim_id=claim_id,
        )


def _start_operations_review(
    context: Any,
    caller: ScopeContext,
    decision: Any,
    payload: AssistantQuestionRequest,
    *,
    language: str,
) -> JSONResponse:
    """Start the operations-review card the operator clicked, and answer 202.

    The body mirrors the ``type: "diagnosis"`` shape (an id + the polled
    window) rather than inventing a second convention: the client polls the
    same analysis surface it would have used anyway, and this action adds no
    new contract — only a way to reach an existing one.

    ``site_ids=None`` means "the caller's whole resolved site set", the same
    convention ``POST /v1/standard/analyses`` uses: the worker intersects it
    with the caller's live scope, so a click can never read wider than the
    caller may see.
    """
    del decision, payload
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=OPERATIONS_REVIEW_WINDOW_HOURS)
    try:
        job = context.runtime.start_operations_analysis(
            caller,
            site_ids=None,
            window_start=format_stored_time(window_start),
            window_end=format_stored_time(now),
            language=language,
        )
    except (ValueError, RuntimeError) as exc:
        raise _runtime_unavailable("ANALYSIS_UNAVAILABLE", "analysis unavailable", exc) from exc
    base = _analysis_job_response(job, language)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            **base,
            "type": "analysis",
            "shortcut_code": OPERATIONS_REVIEW_CODE,
        },
    )
