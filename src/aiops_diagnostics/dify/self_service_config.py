"""平台维护连接一次，业务应用只保存其引用；配置中不接受浏览器路径。"""

import json
import re
from pathlib import Path

from aiops_diagnostics.common.private_files import validate_private_file

from .self_service_store import SelfServiceStore


def approved_model_names(settings) -> tuple[str, ...]:
    """profile 里**已批准**的端模型名（管家 `model` 与 `consumer_models`）。

    只读名字，不做 `profiles()` 那套目标/清单/凭据校验 —— 那个函数证明的是"这台部署
    能开通"，而这里回答的是"发布门允许哪些模型落在 agent 配置上"。两者失败含义不同：
    profile 坏掉时网关应当在启动校验处报错（`create_gateway_app` 已经先调 `profiles()`），
    而不是让发布门因为读不到而拒绝一个本来合法的发布。读不到就返回空，由调用方保持原值。
    """
    path_value = getattr(settings, "operator_profiles_file", "")
    if not path_value:
        return ()
    try:
        path = Path(path_value)
        validate_private_file(path)
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        return ()
    names: set[str] = set()
    for row in body.get("profiles") or []:
        if not isinstance(row, dict):
            continue
        for model in [row.get("model"), *(row.get("consumer_models") or [])]:
            if isinstance(model, dict) and isinstance(model.get("name"), str) and model["name"]:
                names.add(model["name"])
    return tuple(sorted(names))


def profiles(settings):
    path = Path(settings.operator_profiles_file)
    validate_private_file(path)
    if path.stat().st_size > 262144:
        raise ValueError("operator profiles too large")
    body = json.loads(path.read_text())
    if body.get("schema") != "operator-profiles/1" or not isinstance(body.get("profiles"), list):
        raise ValueError("invalid operator profiles")
    result = {}
    for row in body["profiles"]:
        key = row["id"]
        if not re.fullmatch(r"[a-z0-9_-]{1,48}", key) or key in result:
            raise ValueError("invalid profile identity")
        target = row["target"]
        if target["entry"] != "operator" or target["environment"] != settings.execution_environment:
            raise ValueError("profile environment mismatch")
        from aiops_diagnostics.execution import execution_manifest

        node_config = Path(target["config_file"])
        validate_private_file(node_config)
        if json.loads(node_config.read_text())["environment"] != target["environment"]:
            raise ValueError("node profile environment mismatch")
        if execution_manifest.load(target["manifest_file"]) is None:
            raise ValueError("profile manifest missing")
        consumer_models = row.get("consumer_models", [])
        if not isinstance(consumer_models, list) or len(consumer_models) > 20:
            raise ValueError("invalid consumer models")
        seen = set()
        for model in [row["model"], *consumer_models]:
            if not isinstance(model, dict) or not all(
                model.get(k) for k in ("plugin", "provider", "name", "credential_file")
            ):
                raise ValueError("profile model missing")
            identity = model["provider"], model["name"]
            if identity in seen:
                raise ValueError("duplicate approved model")
            seen.add(identity)
            credential = Path(model["credential_file"])
            validate_private_file(credential)
            values = json.loads(credential.read_text())
            if not values.get("api_key") or values.get("function_calling_type") != "tool_call":
                raise ValueError("profile credentials missing tool capability")
            if int(values["max_tokens_to_sample"]) < int(model["completion_params"]["max_tokens"]):
                raise ValueError("profile output budget invalid")
        result[key] = row
    if not result:
        raise ValueError("operator profiles empty")
    return result


def app_binding(settings, app_id):
    from .dify_app_registry import DifyAppBinding, DifyAppRegistry

    if getattr(settings, "operator_profiles_file", ""):
        row = SelfServiceStore(settings.database_file).app(app_id)
        # 只有管家行才是 operator 绑定。客户端行也住在这张表里，把它的 app_id 当成
        # operator 绑定会让"这一版走节点引擎"变成一句凭空的事实。
        if row and row["entry"] == "operator":
            return DifyAppBinding(row["tenant_id"], "operator", app_id, row["agent_name"])
    return next((b for b in DifyAppRegistry(settings.database_file).all() if b.app_id == app_id), None)
