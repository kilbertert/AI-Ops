"""登记现役 CPMS 节点提供方，不分发共享业务凭据。"""

from __future__ import annotations

import json

from aiops_diagnostics.gateway_config import canonical_gateway_url

PROVIDER_PATH = "/console/api/workspaces/current/tool-provider/api"


def ensure_node_provider(console, workspace, gateway_url, manifest):
    """从已实现的节点清单登记 CPMS 工具；提供方不持公司或整租户凭据。"""
    from aiops_diagnostics.execution.chat_execution import OPERATOR_TOOL_NAMES

    tools = manifest["tools"]
    if not tools or any(t["name"] not in OPERATOR_TOOL_NAMES for t in tools):
        raise ValueError("invalid CPMS node tool template")
    schema = {
        "openapi": "3.0.3",
        "info": {"title": "CPMS 节点只读工具", "version": "1.0.0"},
        "servers": [{"url": canonical_gateway_url(gateway_url)}],
        "paths": {
            f"/v1/operator-tools/{t['name']}": {
                "post": {
                    "operationId": t["name"],
                    "summary": t["description"],
                    "description": "需要节点任务授权；请使用编辑器试运行，不使用共享业务凭据。",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": json.loads(t["input_schema"])}},
                    },
                    "responses": {
                        "200": {"description": "本次授权订单的CPMS证据"},
                        "403": {"description": "任务授权或正式工具权限不成立"},
                    },
                }
            }
            for t in tools
        },
    }
    providers = console._call(
        workspace, "/console/api/workspaces/current/tool-providers?type=api", follow_redirects=False
    )
    if not isinstance(providers, list):
        raise ValueError("provider list unavailable")
    if any(p.get("name") == "cpms-node" for p in providers):
        existing = console._call(workspace, PROVIDER_PATH + "/get?provider=cpms-node", follow_redirects=False)
        if json.loads(existing["schema"]) != schema:
            raise ValueError("existing cpms-node schema differs; migrate explicitly")
        return {"action": "unchanged", "provider": "cpms-node"}
    result = console._call(
        workspace,
        PROVIDER_PATH + "/add",
        method="POST",
        follow_redirects=False,
        body={
            "provider": "cpms-node",
            "schema_type": "openapi",
            "schema": json.dumps(schema, ensure_ascii=False),
            "icon": {"type": "emoji", "content": "🔎", "background": "#E0F2FE"},
            "credentials": {"auth_type": "none"},
            "privacy_policy": "",
            "custom_disclaimer": "工具由授权节点执行；Dify原生预览没有任务授权时拒绝取数。",
        },
    )
    if not isinstance(result, dict) or result.get("result") != "success":
        raise ValueError("node provider creation not confirmed")
    return {"action": "created", "provider": "cpms-node"}
