"""现役业务的一次性结构化模型请求；不启动子进程、线程或工具循环。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from aiops_diagnostics.bounded_http import (
    ErrorMapping,
    RequestSpec,
    bearer_auth_header,
    join_url,
    json_body,
    request_json,
)
from aiops_diagnostics.config import AgentSettings, ProviderConfig
from aiops_diagnostics.private_files import (
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


def _unavailable(_):
    return RuntimeError("model upstream unavailable")


def structured_request(*, provider, api_key, messages, name, schema):
    schema = {"name": name, "schema": schema, "strict": True}
    if provider.wire_api == "responses":
        path = "/responses"
        payload = {
            "model": provider.model,
            "input": messages,
            "max_output_tokens": 1024,
            "text": {"format": {"type": "json_schema", **schema}},
            "stream": False,
            "store": False,
        }
    elif provider.wire_api == "chat":
        path = "/chat/completions"
        payload = {
            "model": provider.model,
            "messages": messages,
            "max_tokens": 1024,
            "response_format": {"type": "json_schema", "json_schema": schema},
            "stream": False,
        }
    else:
        raise ValueError("unsupported model transport")
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
