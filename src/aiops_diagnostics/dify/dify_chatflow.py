"""Dify Chatflow 的 Service API：固定发布版，核对同次运行与消息后交付完整答案。"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.common.bounded_http import (
    TRANSPORT_FAILURES,
    HttpFailure,
    RequestSpec,
    open_response,
    quote_segment,
    request_json,
)
from aiops_diagnostics.dify.dify_chat_workflow import (
    WORKFLOW_ID_PATTERN,
    DifyWorkflowError,
    _events,
    _failure,
    _mapping,
)
from aiops_diagnostics.gateway_config import canonical_gateway_url


@dataclass(frozen=True)
class ChatflowReply:
    workflow_run_id: str
    task_id: str
    conversation_id: str
    message_id: str
    answer: str
    metadata: dict[str, Any]


def _invalid() -> DifyWorkflowError:
    return DifyWorkflowError("DIFY_CHATFLOW_INVALID", "chatflow execution evidence is invalid")


class DifyChatflowClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 120) -> None:
        self.base_url = canonical_gateway_url(base_url).rstrip("/")
        if not 1 <= timeout_seconds <= 180:
            raise ValueError("chatflow timeout must be between 1 and 180 seconds")
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        *,
        key: str,
        workflow_id: str,
        user: str,
        query: str,
        inputs: dict[str, Any],
        conversation_id: str = "",
        photo_id: str = "",
        on_started: Callable[[str, str, str, str], None],
    ) -> ChatflowReply:
        for value in [
            workflow_id,
            *([conversation_id] if conversation_id else []),
            *([photo_id] if photo_id else []),
        ]:
            if not WORKFLOW_ID_PATTERN.fullmatch(value):
                raise ValueError("chatflow requires valid workflow/conversation UUIDs")
        if not user.strip() or not key.strip() or not query.strip() or len(query) > 4000:
            raise ValueError("chatflow requires credentials, user and bounded query")
        request = RequestSpec(
            url=self.base_url + "/v1/chat-messages",
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            body=json.dumps(
                {
                    "query": query,
                    "inputs": inputs,
                    "user": user,
                    "conversation_id": conversation_id,
                    "workflow_id": workflow_id,
                    "response_mode": "streaming",
                    "auto_generate_name": False,
                    **(
                        {
                            "files": [
                                {"type": "image", "transfer_method": "local_file", "upload_file_id": photo_id}
                            ]
                        }
                        if photo_id
                        else {}
                    ),
                }
            ).encode(),
            timeout=self.timeout_seconds,
            follow_redirects=False,
        )
        identity: tuple[str, str, str, str] | None = None
        finished = False
        answer = ""
        metadata = None
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with open_response(request) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise _invalid()
                for event in _events(response, deadline):
                    kind = event.get("event")
                    if kind == "error":
                        raise DifyWorkflowError("DIFY_CHATFLOW_FAILED", "chatflow did not succeed")
                    if identity:
                        for field, expected in zip(
                            ("workflow_run_id", "task_id", "conversation_id", "message_id"),
                            identity,
                            strict=True,
                        ):
                            if field in event and event[field] != expected:
                                raise _invalid()
                    if kind in {"workflow_started", "workflow_finished"}:
                        data = event.get("data")
                        if not isinstance(data, dict) or data.get("id") != event.get("workflow_run_id"):
                            raise _invalid()
                        if data.get("workflow_id") != workflow_id:
                            raise DifyWorkflowError("DIFY_WORKFLOW_MISMATCH", "dify ran a different workflow")
                        if kind == "workflow_started":
                            if identity is not None:
                                raise _invalid()
                            identity = tuple(
                                event.get(k)
                                for k in ("workflow_run_id", "task_id", "conversation_id", "message_id")
                            )
                            if not all(
                                isinstance(v, str) and WORKFLOW_ID_PATTERN.fullmatch(v) for v in identity
                            ):
                                raise _invalid()
                            if conversation_id and identity[2] != conversation_id:
                                raise _invalid()
                            on_started(*identity)
                        else:
                            if (
                                not identity
                                or finished
                                or data.get("status") != "succeeded"
                                or event.get("workflow_run_id") != identity[0]
                                or event.get("task_id") != identity[1]
                                or event.get("conversation_id") != identity[2]
                                or event.get("message_id") != identity[3]
                            ):
                                raise DifyWorkflowError("DIFY_CHATFLOW_FAILED", "chatflow did not succeed")
                            finished = True
                    elif kind in {"message", "message_replace"}:
                        if (
                            not identity
                            or any(
                                event.get(k) != identity[i]
                                for i, k in enumerate(
                                    ("workflow_run_id", "task_id", "conversation_id", "message_id")
                                )
                                if k != "workflow_run_id"
                            )
                            or metadata is not None
                            or not isinstance(event.get("answer"), str)
                        ):
                            raise _invalid()
                        answer = event["answer"] if kind == "message_replace" else answer + event["answer"]
                        if len(answer) > 32000:
                            raise _invalid()
                    elif kind == "message_end":
                        if (
                            not identity
                            or metadata is not None
                            or not answer.strip()
                            or any(
                                event.get(k) != identity[i]
                                for i, k in enumerate(
                                    ("workflow_run_id", "task_id", "conversation_id", "message_id")
                                )
                                if k != "workflow_run_id"
                            )
                        ):
                            raise _invalid()
                        metadata = event.get("metadata", {})
                        if not isinstance(metadata, dict):
                            raise _invalid()
                    # Dify 1.17.1先message_end，再workflow_finished；两者齐全才交付。
                    if identity and finished and metadata is not None:
                        return ChatflowReply(*identity, answer, metadata)
        except TRANSPORT_FAILURES as exc:
            raise _failure(HttpFailure("unavailable")) from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise _invalid() from exc
        # 已收到部分文字也不能把断流、缺完成事件当成完整回复。
        raise _invalid()

    def upload_photo(self, *, key: str, user: str, image: bytes) -> str:
        boundary = "----wf01" + secrets.token_hex(16)
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="user"\r\n\r\n{user}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="photo.jpg"\r\n'
                "Content-Type: image/jpeg\r\n\r\n"
            ).encode()
            + image
            + f"\r\n--{boundary}--\r\n".encode()
        )
        result = request_json(
            RequestSpec(
                url=self.base_url + "/v1/files/upload",
                method="POST",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "multipart/form-data; boundary=" + boundary,
                },
                body=body,
                timeout=20,
                follow_redirects=False,
            ),
            mapping=_mapping(),
            envelope=lambda body, mapping: body,
        )
        if not isinstance(result, dict) or not WORKFLOW_ID_PATTERN.fullmatch(str(result.get("id", ""))):
            raise _invalid()
        return result["id"]

    def stop(self, *, key: str, task_id: str, user: str) -> bool:
        try:
            request_json(
                RequestSpec(
                    url=f"{self.base_url}/v1/chat-messages/{quote_segment(task_id)}/stop",
                    method="POST",
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                    body=json.dumps({"user": user}).encode(),
                    timeout=10,
                    follow_redirects=False,
                ),
                mapping=_mapping(),
                envelope=lambda body, mapping: body,
            )
        except DifyWorkflowError:
            return False
        return True
