"""会话（Conversation）CRUD 与活跃订单绑定路由。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse

from aiops_diagnostics.gateway.dependencies import GatewayDeps
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.schemas import ConversationCreateRequest
from aiops_diagnostics.identity.order_identity import is_order_identifier
from aiops_diagnostics.identity.scope_context import ScopeContext


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:

    @app.post("/v1/conversations")
    def create_conversation(
        payload: ConversationCreateRequest,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        """Create a conversation bound to this caller, entry, and agent."""
        caller, decision = identity
        conversation = context.conversation_store.create(
            scope_fingerprint=caller.scope_fingerprint,
            business_entry=str(decision.platform),
            agent_version_key=payload.agent_version_key,
        )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=api._conversation_response(conversation),
        )

    @app.get("/v1/conversations")
    def list_conversations(
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        caller, _ = identity
        conversations = context.conversation_store.list(caller.scope_fingerprint, limit=limit)
        return {
            "type": "conversation_list",
            "count": len(conversations),
            "conversations": [api._conversation_response(item) for item in conversations],
        }

    @app.get("/v1/conversations/{conversation_id}")
    def get_conversation(
        conversation_id: str,
        response: Response,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        caller, decision = identity
        conversation = api._require_conversation(context, caller, conversation_id)
        if conversation.get("business_entry") != str(decision.platform):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "CONVERSATION_NOT_FOUND",
                "conversation not found",
            )
        history = context.conversation_store.turns(conversation["conversation_id"], caller.scope_fingerprint)
        for item in history:
            if item.get("status_qa_id"):
                qa = context.store.get_assistant_question(item["status_qa_id"], caller.scope_fingerprint)
                if qa is None:
                    raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
                api._require_current_chat_order(context, caller, qa, request)
                if item["answer"] != qa["result"]:
                    raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence differs")
        response.headers["Cache-Control"] = "no-store"
        return {
            **api._conversation_response(conversation),
            "turns": [
                {
                    "turn_no": item["turn_no"],
                    "kind": item["kind"],
                    "question": item["question"],
                    "answer": item["answer"],
                    "created_at": item["created_at"],
                }
                for item in history
            ],
        }

    @app.delete("/v1/conversations/{conversation_id}")
    def delete_conversation(
        conversation_id: str,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        caller, decision = identity
        conversation = api._require_conversation(context, caller, conversation_id)
        if conversation.get("business_entry") != str(decision.platform):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "CONVERSATION_NOT_FOUND",
                "conversation not found",
            )
        context.conversation_store.delete(conversation_id, caller.scope_fingerprint)
        return {"type": "conversation_deleted", "conversation_id": conversation_id}

    @app.post("/v1/conversations/{conversation_id}/active-order")
    def set_conversation_active_order(
        conversation_id: str,
        order_no: str | None = None,
        body: dict[str, Any] | None = None,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        """Bind (or clear) the conversation's confirmed active order."""
        caller, decision = identity
        conversation = api._require_conversation(context, caller, conversation_id)
        if conversation.get("business_entry") != str(decision.platform):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "CONVERSATION_NOT_FOUND",
                "conversation not found",
            )
        if body is None:
            body = {}
        candidate = body.get("order_no", order_no)
        if candidate is None:
            conversation = context.conversation_store.set_active_order(
                conversation_id, caller.scope_fingerprint, None
            )
            return api._conversation_response(conversation)
        candidate = str(candidate)
        if not is_order_identifier(candidate):
            raise StandardAPIError(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "INVALID_REQUEST",
                "order_no is invalid",
            )
        verdict = api._order_authorization(context, caller, candidate)
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
        conversation = context.conversation_store.set_active_order(
            conversation_id, caller.scope_fingerprint, candidate
        )
        return api._conversation_response(conversation)
