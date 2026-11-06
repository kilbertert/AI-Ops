"""登记现役 CPMS 节点提供方，不分发共享业务凭据。"""

from __future__ import annotations

import hashlib
import json

from aiops_diagnostics.gateway.gateway_config import canonical_gateway_url

PROVIDER_PATH = "/console/api/workspaces/current/tool-provider/api"


#: 声明里的空参数集合。工具参数由运行输入提供，模型一个也不能给。
EMPTY_SCHEMA = '{"type":"object","properties":{},"required":[],"additionalProperties":false}'


def _projection_digest(schema):
    body = {k: v for k, v in schema.items() if k != "x-aiops-projection-sha256"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def ensure_node_provider(console, workspace, gateway_url, manifest=None, *, migrate=False, reconcile=False):
    """从**声明**登记 CPMS 工具；提供方不持公司或整租户凭据。

    工具名、描述与参数集合都来自 `operator_tools.json`，不再从节点清单模板里抄 ——
    模板是部署数据，声明才是定义。清单模板存在时仍然核一遍名字，避免「模板与声明各说
    各话」被登记进 Dify。

    ``migrate=True`` 时，已存在的 provider 若 schema 与当前声明不同，就地覆盖。这是
    「schema 不同即抛错」之外的**显式**出路。开通使用reconcile，仅同步同地址、
    无共享鉴权且路径属于当前系统声明的目录，其他目录保留并拒绝。
    """
    from aiops_diagnostics.operator import operator_tools

    declared = operator_tools.tools()
    names = {tool.name for tool in declared}
    if manifest is not None:
        tools = manifest.get("tools") or []
        if not tools or any(t["name"] not in names for t in tools):
            raise ValueError("invalid CPMS node tool template")
    schema = {
        "openapi": "3.0.3",
        "info": {"title": "CPMS 节点只读工具", "version": "1.0.0"},
        "servers": [{"url": canonical_gateway_url(gateway_url)}],
        "paths": {
            f"/v1/operator-tools/{declared_name}": {
                "post": {
                    "operationId": declared_name,
                    "summary": description,
                    "description": "需要节点任务授权；请使用编辑器试运行，不使用共享业务凭据。",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": json.loads(operator_tools.tool(declared_name).input_schema())
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "本次授权订单的CPMS证据"},
                        "403": {"description": "任务授权或正式工具权限不成立"},
                    },
                }
            }
            for declared_name, description in ((t.name, t.description) for t in declared)
        },
    }
    schema["x-aiops-projection-sha256"] = _projection_digest(schema)
    providers = console._call(
        workspace, "/console/api/workspaces/current/tool-providers?type=api", follow_redirects=False
    )
    if not isinstance(providers, list):
        raise ValueError("provider list unavailable")
    if any(p.get("name") == "cpms-node" for p in providers):
        existing = console._call(workspace, PROVIDER_PATH + "/get?provider=cpms-node", follow_redirects=False)
        observed = json.loads(existing["schema"])
        stamped = observed.get("x-aiops-projection-sha256") == _projection_digest(observed)
        if reconcile and not (
            observed.get("openapi") == "3.0.3"
            and observed.get("info", {}).get("title") == schema["info"]["title"]
            and observed.get("servers") == schema["servers"]
            and existing.get("credentials", {}).get("auth_type") == "none"
            and set(observed) <= {"openapi", "info", "servers", "paths", "x-aiops-projection-sha256"}
            and ("x-aiops-projection-sha256" not in observed or stamped)
            and bool(observed.get("paths"))
            and set(observed["paths"]) <= set(schema["paths"])
            and all(
                set(operations) == {"post"}
                and operations["post"].get("operationId") == path.rsplit("/", 1)[-1]
                and set(operations["post"])
                <= {"operationId", "summary", "description", "requestBody", "responses"}
                and (
                    stamped
                    or operations["post"].get("requestBody")
                    in (
                        schema["paths"][path]["post"]["requestBody"],
                        {
                            "required": True,
                            "content": {"application/json": {"schema": json.loads(EMPTY_SCHEMA)}},
                        },
                    )
                )
                for path, operations in observed["paths"].items()
            )
        ):
            raise ValueError("cpms-node is not a managed projection; migrate explicitly")
        if observed != schema:
            if not (migrate or reconcile):
                raise ValueError("existing cpms-node schema differs; migrate explicitly")
            result = console._call(
                workspace,
                PROVIDER_PATH + "/update?provider=cpms-node",
                method="POST",
                follow_redirects=False,
                body={
                    "provider": "cpms-node",
                    "original_provider": "cpms-node",
                    "credentials": {"auth_type": "none"},
                    "icon": existing.get("icon") or {"content": "🔎", "background": "#E0F2FE"},
                    "privacy_policy": existing.get("privacy_policy"),
                    "custom_disclaimer": existing.get("custom_disclaimer") or "",
                    "labels": existing.get("labels") or [],
                    "schema_type": "openapi",
                    "schema": json.dumps(schema, ensure_ascii=False),
                },
            )
            if not isinstance(result, dict) or result.get("result") != "success":
                raise ValueError("node provider migration not confirmed")
            saved = console._call(
                workspace, PROVIDER_PATH + "/get?provider=cpms-node", follow_redirects=False
            )
            if json.loads(saved["schema"]) != schema:
                raise ValueError("node provider migration not confirmed by readback")
            return {"action": "migrated", "provider": "cpms-node"}
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
    if reconcile:
        saved = console._call(workspace, PROVIDER_PATH + "/get?provider=cpms-node", follow_redirects=False)
        if json.loads(saved["schema"]) != schema:
            raise ValueError("node provider creation not confirmed by readback")
    return {"action": "created", "provider": "cpms-node"}
