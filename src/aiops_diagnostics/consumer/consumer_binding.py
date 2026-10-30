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


def _route_active(runtime, environment, app_id) -> bool:
    """这条 app 的路由现在是否仍然 active（撤回 = 不再交付）。"""
    path = str(getattr(runtime.gateway_settings, "chat_route_bindings_file", "") or "")
    if not path:
        return True
    from pathlib import Path

    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    rows = [
        row
        for row in ChatRouteBindingStore(Path(path)).all()
        if row.environment == environment and row.app_id == app_id
    ]
    return bool(rows) and all(row.active for row in rows)


def require_pinned(runtime, caller, binding):
    """**在途**判据：路由仍 active、调用者仍有权限 —— 但版本可以已经推进。

    与 :func:`current` 的分工：新会话必须用当前版本；一条已经在跑的 run 用它**签发时**的绑定
    继续跑完（`execute` 里 6 处 `current()` 曾经让"生成中发布新版本"直接失败，复核实测
    `qa_status=failed / CHATFLOW_BINDING_CHANGED`）。
    """
    selected = chat_execution.select_binding(
        runtime.gateway_settings, caller, scenario=chat_execution.SCENARIO_CONSUMER_SERVICE
    )
    # 允许的差别只有"路由推进了一版"（运营正常发布）：在途 run 用它签发时的绑定跑完。
    # 换凭据、换 app、撤回、以及**没有推进却变了**（内容/凭据被换）都必须拒。
    if selected is None or not _route_active(runtime, binding.environment, binding.app_id):
        raise ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "chatflow binding was retracted", http_status=410
        )
    if selected.key != binding.key:
        raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "chatflow credential changed", http_status=410)
    # 版本推进是允许的；没有推进却变了（内容被换）仍然拒。
    if identity(selected) != identity(binding) and selected.route_revision <= binding.route_revision:
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "chatflow binding was retracted", http_status=410
        )
    return binding


def _pinned_route_active(runtime, run) -> bool:
    """签发时那条路由现在是否仍然 active（撤回 = 不再交付）。

    普通发布只推进版本；撤回会把 `state` 改成 `retracted`。这两件事在数据上本来就可区分，
    旧回读却把它们当成同一件事（版本不等就 410），于是**一次正常发布就让历史答案失效**。
    """
    return _route_active(runtime, run.environment, run.app_id)


def deliverable_binding(runtime, caller, run):
    """已签发答复回读：**按签发时那一版交付**，并核对现行权限。

    三步，缺一不可：

    1. 调用者此刻仍有这条路由的权限（`select_binding` 为 None ⇒ 撤权/撤回 ⇒ 拒）；
    2. 路由仍 active（撤回 ⇒ 拒）；
    3. 用**不可变版本快照**重建签发时的绑定，指纹必须与 run 记录的一致
       —— 换凭据、内容被换、版本缺失都会让指纹对不上，于是拒。

    第 3 步是复核指出的关键：返回当前 `selected` 等于用新版冒充旧绑定；旧 run 必须拿到
    它签发时那份冻结配置（含当时的 `workflow_id` 与文案版本）。
    """
    selected = chat_execution.select_binding(
        runtime.gateway_settings, caller, scenario=chat_execution.SCENARIO_CONSUMER_SERVICE
    )
    if selected is None:
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "chatflow binding changed", http_status=410
        )
    if identity(selected) == run.binding_digest:
        return selected
    path = str(getattr(runtime.gateway_settings, "chat_route_bindings_file", "") or "")
    if not path or not _route_active(runtime, run.environment, run.app_id):
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "chatflow binding was retracted", http_status=410
        )
    from pathlib import Path

    row = chat_execution.route_row(Path(path), run.environment, run.app_id)
    if row is None:
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "chatflow binding changed", http_status=410
        )
    try:
        pinned = chat_execution.binding_for_version(
            Path(path),
            row,
            run.agent_version_no,
            str(getattr(runtime.gateway_settings, "chat_dify_key_dir", "") or ""),
        )
    except Exception as exc:  # noqa: BLE001 - 版本不可用就是不能交付
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "pinned publication version is unavailable", http_status=410
        ) from exc
    from dataclasses import replace

    pinned = replace(pinned, route_revision=run.route_revision)
    if identity(pinned) != run.binding_digest:
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "pinned binding no longer reproduces this answer", http_status=410
        )
    return pinned
