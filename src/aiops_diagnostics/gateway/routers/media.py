"""助手图片上传与短期签名知识媒体代理路由。"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request, Response, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from aiops_diagnostics.consumer import consumer_chat_service, consumer_photo
from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.gateway.dependencies import GatewayDeps
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.identity.scope_context import ScopeContext


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:
    """挂载 ``/v1/assistant/photos`` 与 ``/v1/media/{signed_id}`` 路由。"""

    @app.post("/v1/assistant/photos", status_code=201)
    async def upload_consumer_photo(
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
    ) -> JSONResponse:
        caller, decision = identity
        if not context.settings.consumer_chatflow_enabled or str(decision.platform) != "consumer":
            raise StandardAPIError(404, "PHOTO_UNAVAILABLE", "photo input unavailable")
        api._require_consumer_self_order(caller, "consumer", "pending")
        photo = ""
        completed = False
        try:
            binding = chat_execution.select_binding(context.settings, caller, scenario="consumer_service")
            if binding is None or not binding.photo_enabled:
                raise StandardAPIError(404, "PHOTO_UNAVAILABLE", "photo input unavailable")
            photo = context.runtime.consumer_photos.put(
                caller.scope_fingerprint, consumer_chat_service.fingerprint(binding), b""
            )
            body = bytearray()
            try:
                async with asyncio.timeout(20):
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > consumer_photo.MAX_UPLOAD:
                            raise StandardAPIError(413, "PHOTO_TOO_LARGE", "photo too large")
            except TimeoutError as exc:
                raise StandardAPIError(408, "PHOTO_UPLOAD_TIMEOUT", "photo upload timed out") from exc
            image = await run_in_threadpool(
                consumer_photo.normalize_photo,
                bytes(body),
                request.headers.get("Content-Type", "").split(";")[0],
            )
            consumer_chat_service.current(context.runtime, caller, binding)
            context.runtime.consumer_photos.complete_upload(photo, image)
            completed = True
            return JSONResponse(
                status_code=201,
                headers={"Cache-Control": "no-store"},
                content={"photo_id": photo, "expires_in_seconds": 300},
            )
        except ValueError as exc:
            raise StandardAPIError(400, "PHOTO_INVALID", "invalid photo") from exc
        except chat_execution.ChatRunUnavailable as exc:
            raise StandardAPIError(exc.http_status, exc.code, str(exc)) from exc
        finally:
            if photo and not completed:
                context.runtime.consumer_photos.discard(photo)

    @app.get("/v1/media/{signed_id}")
    def get_media(
        signed_id: str,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> Response:
        """提供带短期 HMAC 签名的知识库媒体资源，越权与不存在统一 404。"""
        media_response = context.runtime.serve_media(signed_id, range_header=range_header)
        if media_response is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "MEDIA_NOT_FOUND",
                "media resource not found",
            )
        return Response(
            status_code=media_response.status_code,
            headers=media_response.headers,
            content=media_response.body,
        )
