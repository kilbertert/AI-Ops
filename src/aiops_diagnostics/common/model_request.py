"""现役业务的一次性结构化模型请求；不启动子进程、线程或工具循环。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from aiops_diagnostics.common.bounded_http import (
    ErrorMapping,
    RequestSpec,
    bearer_auth_header,
    join_url,
    json_body,
    request_json,
)
from aiops_diagnostics.common.config import AgentSettings, ProviderConfig
from aiops_diagnostics.common.private_files import (
    PrivatePathError,
    validate_private_directory,
    validate_private_file,
)


def resolve_provider_api_key(
    settings: AgentSettings,
    *,
    provider: ProviderConfig | None = None,
    key_slot: str | None = None,
) -> str:
    selected = provider or settings.select_provider(None)
    settings.validate()
    if selected.api_key_env:
        value = os.getenv(selected.api_key_env, "").strip()
        if value:
            return value
    if settings.api_key_file:
        configured_path = Path(settings.api_key_file).expanduser()
        if configured_path.is_symlink():
            raise RuntimeError(f"模型 API key 文件不得是符号链接: {configured_path}")
        key_path = configured_path.resolve()
    else:
        effective_slot = key_slot or selected.resolved_key_slot()
        configured_dir = Path(settings.key_dir).expanduser()
        if configured_dir.is_symlink():
            raise RuntimeError(f"模型 key slot 目录不得是符号链接: {configured_dir}")
        key_dir = configured_dir.resolve()
        try:
            validate_private_directory(key_dir)
        except PrivatePathError as exc:
            raise RuntimeError(str(exc)) from exc
        key_path = key_dir / f"{effective_slot}.key"
    if key_path.is_symlink() or not key_path.is_file():
        raise RuntimeError(
            f"模型 API key 不存在: slot={settings.key_slot}; 请设置 {settings.api_key_env} 或受限 key 文件"
        )
    try:
        validate_private_directory(key_path.parent)
        validate_private_file(key_path)
    except PrivatePathError as exc:
        raise RuntimeError(str(exc)) from exc
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(key_path, flags)
    except OSError as exc:
        raise RuntimeError(f"模型 API key 文件无法安全打开: {key_path}") from exc
    try:
        with os.fdopen(descriptor, encoding="utf-8") as key_file:
            descriptor = -1
            value = key_file.read().strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not value:
        raise RuntimeError(f"模型 API key 文件为空: {key_path}")
    return value


class ModelRequestError(RuntimeError):
    """仅携带受限原因与状态，不能把上游正文或凭据带到日志/页面。"""

    def __init__(self, reason, http_status=None):
        self.reason = reason
        self.http_status = http_status
        self.retryable = reason not in {"quota_exhausted", "auth_rejected"}
        super().__init__(f"model request failed: reason={reason} http_status={http_status}")


def _unavailable(failure):
    # 兼容已实测的供应商403文案；403/429状态本身都不等于欠费。
    quota = {"insufficient_quota", "insufficient_user_quota"}
    if (
        failure.code in quota
        or failure.error_type in quota
        or (failure.status == 403 and "用户额度不足" in failure.detail)
    ):
        reason = "quota_exhausted"
    elif failure.kind == "auth_rejected":
        reason = "auth_rejected"
    elif failure.status == 429:
        reason = "rate_limited"
    elif failure.kind == "unavailable":
        reason = "transport_unavailable"
    elif failure.kind in {"invalid_body", "invalid_envelope"}:
        reason = "invalid_response"
    else:
        reason = "upstream_error"
    return ModelRequestError(reason, failure.status)


def structured_request(
    *, provider, api_key, messages, name, schema, max_output_tokens=1024, reasoning_effort=None
):
    schema = {"name": name, "schema": schema, "strict": True}
    if provider.wire_api == "responses":
        path = "/responses"
        payload = {
            "model": provider.model,
            "input": messages,
            "max_output_tokens": max_output_tokens,
            "text": {"format": {"type": "json_schema", **schema}},
            "stream": False,
            "store": False,
        }
    elif provider.wire_api == "chat":
        path = "/chat/completions"
        payload = {
            "model": provider.model,
            "messages": messages,
            "max_tokens": max_output_tokens,
            "response_format": {"type": "json_schema", "json_schema": schema},
            "stream": False,
        }
    else:
        raise ValueError("unsupported model transport")
    if reasoning_effort is not None:
        if reasoning_effort not in {"none", "minimal", "low", "medium", "high"}:
            raise ValueError("unsupported reasoning effort")
        if provider.wire_api == "responses":
            payload["reasoning"] = {"effort": reasoning_effort}
        else:
            payload["reasoning_effort"] = reasoning_effort
    body = request_json(
        RequestSpec(
            url=join_url(provider.base_url, path),
            method="POST",
            headers={"Authorization": bearer_auth_header(api_key), "Content-Type": "application/json"},
            body=json_body(payload),
            timeout=20,
            max_read_bytes=65536,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(_unavailable, _unavailable, _unavailable, _unavailable, _unavailable),
    )
    try:
        if path == "/responses":
            if body.get("status") != "completed":
                raise ValueError("incomplete model response")
            if any(item["type"] not in {"reasoning", "message"} for item in body["output"]):
                raise ValueError("unexpected model output")
            parts = [part for item in body["output"] if item["type"] == "message" for part in item["content"]]
            if len(parts) != 1 or parts[0]["type"] != "output_text":
                raise ValueError("invalid model response text")
            raw = parts[0]["text"]
        else:
            (choice,) = body["choices"]
            if (
                choice["finish_reason"] != "stop"
                or choice["message"].get("tool_calls")
                or choice["message"].get("refusal")
            ):
                raise ValueError("incomplete model response")
            raw = choice["message"]["content"]
        return json.loads(raw)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid model response") from exc
