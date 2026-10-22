"""登记真实 Dify API provider；从不切换共享账号的 current workspace。"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from aiops_diagnostics.dify_console_admin import ConsoleApiError, DifyConsoleAdmin
from aiops_diagnostics.gateway_config import canonical_gateway_url
from aiops_diagnostics.ops_bindings import OpsBinding, token_digest
from aiops_diagnostics.ops_tools import ChargingInfoInput, OrderListInput

PROVIDER = "aiops-ops"
PROVIDER_PATH = "/console/api/workspaces/current/tool-provider/api"


def ops_openapi(base_url: str, tenant_id: str) -> dict[str, Any]:
    base = canonical_gateway_url(base_url)
    paths = {}
    for name, model, description in (
        ("charging_info", ChargingInfoInput, "查询本租户订单当前业务状态，不代表设备实时状态或故障归因。"),
        ("order_list", OrderListInput, "分页查询本租户订单，仅返回选单所需字段。"),
    ):
        paths[f"/v1/dify/ops/{tenant_id}/{name}"] = {
            "post": {
                "operationId": name,
                "summary": description,
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": model.model_json_schema()}},
                },
                "responses": {
                    "200": {"description": "只读结果与证据"},
                    "403": {"description": "服务绑定或已发布权限拒绝"},
                },
            }
        }
    return {
        "openapi": "3.0.3",
        "info": {"title": "AI-Ops 租户运维只读工具", "version": "1.0.0"},
        "servers": [{"url": base}],
        "paths": paths,
    }


def ensure_ops_provider(
    console: DifyConsoleAdmin, binding: OpsBinding, token: str, gateway_url: str
) -> dict[str, Any]:
    if token_digest(token) != binding.key_sha256 or len(token) < 32:
        raise ValueError("工具凭据与本地绑定不符或长度不足")
    schema = ops_openapi(gateway_url, binding.tenant_id)
    try:
        providers = console._call(
            binding.workspace_id,
            "/console/api/workspaces/current/tool-providers?type=api",
            follow_redirects=False,
        )
        if not isinstance(providers, list):
            raise ValueError("无法读取 Dify 工具目录")
        existing = (
            console._call(
                binding.workspace_id,
                f"{PROVIDER_PATH}/get?{urlencode({'provider': PROVIDER})}",
                follow_redirects=False,
            )
            if any(isinstance(row, dict) and row.get("name") == PROVIDER for row in providers)
            else None
        )
    except ConsoleApiError:
        # 不回显 Dify 错误体：创建失败可能包含传给它的密钥。
        raise ValueError("无法读取 Dify 工具提供方") from None
    if existing is not None:
        try:
            matches = json.loads(existing["schema"]) == schema
        except (KeyError, TypeError, ValueError):
            matches = False
        if not matches:
            raise ValueError("已有 aiops-ops schema 不同，拒绝覆盖")
        # Dify 返回掩码凭据；schema 相同不能证明密钥相同，真实调用单独验收。
        return {"action": "unchanged", "provider": PROVIDER, "credentials_verified": False}
    payload = {
        "provider": PROVIDER,
        "schema_type": "openapi",
        "schema": json.dumps(schema, ensure_ascii=False),
        "icon": {"type": "emoji", "content": "🔎", "background": "#E0F2FE"},
        "credentials": {
            "auth_type": "api_key_header",
            "api_key_header": "Authorization",
            "api_key_value": token,
            "api_key_header_prefix": "bearer",
        },
        "privacy_policy": "",
        "custom_disclaimer": "按登记租户服务权限只读查询，不代表操作人本人范围。",
    }
    try:
        result = console._call(
            binding.workspace_id,
            f"{PROVIDER_PATH}/add",
            method="POST",
            follow_redirects=False,
            body=payload,
        )
    except ConsoleApiError:
        raise ValueError("Dify 提供方创建失败或结果未知；重新读取确认，未覆盖既有配置") from None
    if not isinstance(result, dict) or result.get("result") != "success":
        raise ValueError("Dify 未确认提供方创建成功")
    # Dify 1.17.1 同名重建不清旧凭据缓存；官方update会失效该缓存。
    try:
        refreshed = console._call(
            binding.workspace_id,
            f"{PROVIDER_PATH}/update",
            method="POST",
            follow_redirects=False,
            body={**payload, "original_provider": PROVIDER},
        )
    except ConsoleApiError:
        raise ValueError("Dify 提供方已创建，但凭据缓存刷新未确认；请核对提供方后再调用") from None
    if not isinstance(refreshed, dict) or refreshed.get("result") != "success":
        raise ValueError("Dify 提供方已创建，但凭据缓存刷新未确认")
    return {"action": "created", "provider": PROVIDER, "credentials_verified": False}
