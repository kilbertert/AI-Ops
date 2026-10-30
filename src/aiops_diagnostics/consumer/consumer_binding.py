"""已签发答复的绑定判据：**按签发时那一版交付**，撤回/换凭据才拒。

放在独立模块里，是因为它同时被 `consumer_chat_service`（worker 与回读）和
`consumer_knowledge`（取数回调）使用 —— 两者互相 import 会形成包内循环。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable


def digest(path: str) -> str:
    if not path:
        return ""
    from aiops_diagnostics.common.private_files import validate_private_file

    p = Path(path)
    validate_private_file(p)
    if p.stat().st_size > 2_000_000:
        raise ValueError("consumer content too large")
    return hashlib.sha256(p.read_bytes()).hexdigest()


def identity(binding) -> str:
    """绑定的指纹：字段 + 文案文件内容。旧发布版保持原值（代码升级不让历史答案失效）。"""
    from dataclasses import asdict

    fields = asdict(binding)
    if fields.get("callback_version") == 1:
        fields.pop("callback_version")
    return hashlib.sha256(
        (json.dumps(fields, sort_keys=True) + digest(binding.content_file)).encode()
    ).hexdigest()


def _route(runtime, binding):
    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    path = runtime.gateway_settings.chat_route_bindings_file
    row = (
        ChatRouteBindingStore(Path(path)).get(
            binding.environment, binding.tenant_id, binding.entry, binding.scenario
        )
        if path
        else None
    )
    # 文件绑定不要求管理行存在；已经纳管或撤回的路由不能回落文件。
    if (row is not None and (not row.active or row.app_id != binding.app_id)) or (
        binding.route_revision and row is None
    ):
        raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "route is no longer active", http_status=410)
    return row


def require_pinned(runtime, caller, binding):
    """提示词版本可更新；当前身份、凭据和能力仍必须覆盖签发时的授权。"""
    if not runtime.gateway_settings.consumer_chatflow_enabled:
        raise ChatRunUnavailable("CHATFLOW_DISABLED", "chatflow disabled", http_status=404)
    selected = chat_execution.select_binding(
        runtime.gateway_settings, caller, scenario=chat_execution.SCENARIO_CONSUMER_SERVICE
    )
    stable = (
        "environment",
        "tenant_id",
        "entry",
        "scenario",
        "engine",
        "agent_id",
        "app_id",
        "workflow_key_file",
    )
    capabilities = ("live_order_status", "live_billing_record", "photo_enabled")
    if (
        selected is None
        or any(getattr(selected, key) != getattr(binding, key) for key in stable)
        or any(getattr(binding, key) and not getattr(selected, key) for key in capabilities)
        or not set(binding.knowledge_base_ids) <= set(selected.knowledge_base_ids)
        or selected.route_revision < binding.route_revision
        or (selected.route_revision == binding.route_revision and identity(selected) != identity(binding))
    ):
        raise ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "publication authorization changed", http_status=410
        )
    _route(runtime, binding)
    return binding


def deliverable_binding(runtime, caller, run):
    """从不可变版本还原本轮配置，再按当前授权放行。"""
    from dataclasses import replace

    selected = chat_execution.select_binding(
        runtime.gateway_settings, caller, scenario=chat_execution.SCENARIO_CONSUMER_SERVICE
    )
    if selected is None or selected.key != run.binding_key or selected.app_id != run.app_id:
        raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "publication unavailable", http_status=410)
    if identity(selected) == run.binding_digest:
        return require_pinned(runtime, caller, selected)
    row = _route(runtime, selected)
    if row is None or not run.route_revision:
        raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "file binding changed", http_status=410)
    try:
        pinned = chat_execution.binding_for_version(
            Path(runtime.gateway_settings.chat_route_bindings_file),
            row,
            run.agent_version_no,
            runtime.gateway_settings.chat_dify_key_dir,
        )
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        raise ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "pinned version unavailable", http_status=410
        ) from exc
    pinned = replace(pinned, route_revision=run.route_revision)
    if identity(pinned) != run.binding_digest:
        raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "pinned binding differs", http_status=410)
    return require_pinned(runtime, caller, pinned)
