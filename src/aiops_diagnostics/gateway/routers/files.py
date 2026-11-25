"""后台 AI 抽屉附件上传、元信息查询、缩略图预览与下载中心任务路由（#1707）。"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Response, UploadFile, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from aiops_diagnostics.gateway.dependencies import GatewayDeps
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.identity.scope_context import ScopeContext
from aiops_diagnostics.operator.attachments import (
    MAX_DOCUMENT_BYTES,
    AttachmentError,
    parse_attachment,
    public_file_dict,
)
from aiops_diagnostics.operator.download_center import DownloadCenterError
from aiops_diagnostics.publication.agent_content import (
    UnknownAssistantAgentError,
    resolve_operator_agent_id,
)

_CONV_ID_RE = re.compile(r"^conv_[A-Za-z0-9]{1,57}$")


def _require_operator(decision: Any) -> None:
    if str(decision.platform) != "operator":
        raise StandardAPIError(
            status.HTTP_403_FORBIDDEN,
            "PLATFORM_FORBIDDEN",
            "operator entry is required",
        )


async def _read_bounded_upload(upload: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise StandardAPIError(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "ATTACHMENT_TOO_LARGE",
                "上传文件大小超过上限",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:

    @app.post("/v1/assistant/files", status_code=status.HTTP_201_CREATED)
    async def upload_assistant_file(
        file: Annotated[UploadFile, File(...)],
        conversation_id: Annotated[str | None, Form()] = None,
        agent_id: Annotated[str | None, Form()] = None,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
    ) -> JSONResponse:
        caller, decision = identity
        _require_operator(decision)
        try:
            resolved_agent_id = resolve_operator_agent_id(agent_id)
        except UnknownAssistantAgentError as exc:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "AGENT_NOT_FOUND", "assistant agent not found"
            ) from exc

        cleaned_conv_id: str | None = None
        if conversation_id is not None and conversation_id.strip():
            cleaned_conv_id = conversation_id.strip()
            if not _CONV_ID_RE.fullmatch(cleaned_conv_id):
                raise StandardAPIError(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "INVALID_REQUEST",
                    "conversation_id is invalid",
                )
            conv = api._require_conversation(context, caller, cleaned_conv_id)
            if conv.get("business_entry") != "operator":
                raise StandardAPIError(
                    status.HTTP_404_NOT_FOUND,
                    "CONVERSATION_NOT_FOUND",
                    "conversation not found",
                )

        try:
            with context.assistant_files.rate_limiter.acquire(caller.scope_fingerprint):
                raw = await _read_bounded_upload(file, MAX_DOCUMENT_BYTES)
                parsed = await run_in_threadpool(parse_attachment, raw, file.filename, file.content_type)
                user_id = str(getattr(caller.subject, "b_user_id", "") or "")
                username = user_id or "operator"
                storage_url, record_id = await run_in_threadpool(
                    context.download_center.upload_and_register_import,
                    tenant_id=caller.effective_tenant_id,
                    user_id=user_id,
                    username=username,
                    file_name=parsed.file_name,
                    file_bytes=raw,
                    content_type=parsed.mime_type,
                )
                row = context.assistant_files.create(
                    scope_fingerprint=caller.scope_fingerprint,
                    tenant_id=caller.effective_tenant_id,
                    user_id=user_id,
                    business_entry="operator",
                    conversation_id=cleaned_conv_id,
                    agent_id=resolved_agent_id,
                    parsed=parsed,
                    storage_url=storage_url,
                    download_center_record_id=record_id,
                )
        except AttachmentError as exc:
            raise StandardAPIError(exc.http_status, exc.code, exc.message, retryable=exc.retryable) from exc
        except DownloadCenterError as exc:
            raise StandardAPIError(exc.http_status, exc.code, exc.message, retryable=exc.retryable) from exc

        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            headers={"Cache-Control": "no-store"},
            content=public_file_dict(row),
        )

    @app.get("/v1/assistant/files/{file_id}")
    def get_assistant_file(
        file_id: str,
        response: Response,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        caller, decision = identity
        _require_operator(decision)
        row = context.assistant_files.get(file_id, caller.scope_fingerprint, business_entry="operator")
        if row is None:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "ATTACHMENT_NOT_FOUND", "attachment not found")
        response.headers["Cache-Control"] = "no-store"
        return public_file_dict(row)

    @app.get("/v1/assistant/files/{file_id}/preview")
    def get_assistant_file_preview(
        file_id: str,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
    ) -> Response:
        caller, decision = identity
        _require_operator(decision)
        row = context.assistant_files.get(file_id, caller.scope_fingerprint, business_entry="operator")
        if row is None:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "ATTACHMENT_NOT_FOUND", "attachment not found")
        blob = row.get("thumbnail_blob")
        if row.get("category") != "image" or not blob:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ATTACHMENT_PREVIEW_UNAVAILABLE",
                "preview is only available for image attachments",
            )
        return Response(
            content=bytes(blob),
            media_type=str(row.get("thumbnail_mime_type") or "image/jpeg"),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/assistant/files/{file_id}/download-task")
    def create_file_download_task(
        file_id: str,
        response: Response,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        caller, decision = identity
        _require_operator(decision)
        row = context.assistant_files.get(file_id, caller.scope_fingerprint, business_entry="operator")
        if row is None:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "ATTACHMENT_NOT_FOUND", "attachment not found")
        user_id = str(getattr(caller.subject, "b_user_id", "") or "")
        username = user_id or "operator"
        export_name = f"AI排查附件_{row['file_name']}"[:200]
        try:
            record_id = context.download_center.register_download_export(
                tenant_id=caller.effective_tenant_id,
                user_id=user_id,
                username=username,
                file_name=export_name,
                storage_url=str(row["storage_url"]),
            )
        except DownloadCenterError as exc:
            raise StandardAPIError(exc.http_status, exc.code, exc.message, retryable=exc.retryable) from exc
        response.headers["Cache-Control"] = "no-store"
        return {
            "download_center_record_id": record_id,
            "direction": -1,
            "status": "ready",
            "file_name": export_name,
            "notify_file_center": True,
        }
