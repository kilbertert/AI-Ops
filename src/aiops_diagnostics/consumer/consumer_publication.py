"""WF01发布目标由服务端声明；冻结内容与执行图共用既有Agent版本。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from aiops_diagnostics.common.private_files import validate_private_file
from aiops_diagnostics.consumer.consumer_knowledge import load_content
from aiops_diagnostics.dify.dify_chat_workflow import WORKFLOW_ID_PATTERN
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.identity.order_visibility import tenant_matches

SCENARIO = "consumer_service"

#: 受管行（`operator_apps` 里 `entry='consumer'`）与现役文件**同时**对同一个客户生效时，
#: 必须两条都指向同一个应用。否则两条权威各说一套，而“哪一条算数”没有规则可以回答。
_CONFLICT = "consumer publication target conflicts with managed registration"


def managed_target(settings, tenant_id: str, app_id: str):
    """这个客户在**本环境、这个应用**的受管客户端运行目标；没有则 None。

    受管行是**新客户的第一权威**：页面开通的客户端应用不会再去手改一个 0600 文件。
    按应用取（不是按租户取第一条）—— 一个客户可以同时有现役正式版和新草稿。
    """
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore

    environment = settings.execution_environment
    if not getattr(settings, "database_file", ""):
        return None
    row = SelfServiceStore(settings.database_file).consumer_target(tenant_id, environment, app_id)
    if row is None:
        return None
    targets = json.loads(row.get("targets_json") or "{}")
    if not isinstance(targets, dict):
        raise ValueError("invalid managed consumer targets")
    return {
        "app_id": row["app_id"],
        "tenant_id": row["tenant_id"],
        "environment": environment,
        "knowledge_base_ids": list(targets.get("knowledge_base_ids") or []),
        "live_order_status": bool(targets.get("live_order_status")),
        "live_billing_record": bool(targets.get("live_billing_record")),
        "photo_enabled": bool(targets.get("photo_enabled")),
        # 受管目标没有内容文件：它走动态知识回答（`documents` 模式），文案在代码里。
        "source": "managed",
    }


def declared_rows(settings) -> list[dict]:
    """现役 0600 文件里声明的全部目标（逐条校验形状）。"""
    if not settings.consumer_publication_targets_file:
        return []
    path = Path(settings.consumer_publication_targets_file)
    validate_private_file(path)
    if path.stat().st_size > 100_000:
        raise ValueError("consumer targets too large")
    body = json.loads(path.read_text())
    if (
        not isinstance(body, dict)
        or body.get("schema") != "aiops-consumer-targets/1"
        or not isinstance(body.get("targets"), list)
    ):
        raise ValueError("invalid consumer publication targets")
    found = []
    keys = set()
    for row in body["targets"]:
        if not isinstance(row, dict) or set(row) - {"photo_enabled", "content_file"} != {
            "app_id",
            "tenant_id",
            "environment",
            "knowledge_base_ids",
            "live_order_status",
            "live_billing_record",
        }:
            raise ValueError("invalid consumer publication target")
        for key in ("app_id", "tenant_id", "environment"):
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError("invalid consumer target identity")
        if (
            not WORKFLOW_ID_PATTERN.fullmatch(row["app_id"])
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", row["environment"])
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", row["tenant_id"])
        ):
            raise ValueError("invalid consumer target scope")
        key = row["environment"], row["tenant_id"]
        if key in keys:
            raise ValueError("duplicate consumer publication target")
        keys.add(key)
        if (
            not isinstance(row["knowledge_base_ids"], list)
            or not row["knowledge_base_ids"]
            or len(row["knowledge_base_ids"]) > 20
            or any(
                not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", k)
                for k in row["knowledge_base_ids"]
            )
            or type(row["live_order_status"]) is not bool
            or type(row["live_billing_record"]) is not bool
            or type(row.get("photo_enabled", False)) is not bool
        ):
            raise ValueError("invalid consumer target capabilities")
        found.append(row)
    return found


def file_target(settings, binding):
    """现役文件里**属于这个应用**的那一条目标。"""
    for row in declared_rows(settings):
        if (
            row["app_id"] == binding.app_id
            and tenant_matches(row["tenant_id"], binding.tenant_id)
            and row["environment"] == settings.execution_environment
        ):
            return row
    return None


def target_for(settings, binding):
    """这个应用的可发布目标：受管行优先，现役文件兼容，冲突显式拒绝。

    读顺序固定为“先两边都算出来、再比对”，而不是“先命中先返回”：只有两边都看得到，
    “文件里写着应用 A、受管行写着应用 B”才可能被发现。冲突的粒度是 **(租户, 环境)** ——
    同一客户的客户端只能有一个正式目标，两个来源各说一套时没有规则可以裁决。
    """
    from aiops_diagnostics.identity.order_visibility import normalize_tenant

    tenant = normalize_tenant(binding.tenant_id) or ""
    managed = managed_target(settings, tenant, binding.app_id) if tenant else None
    declared = next(
        (
            row
            for row in declared_rows(settings)
            if row["app_id"] == binding.app_id
            and tenant_matches(row["tenant_id"], binding.tenant_id)
            and row["environment"] == settings.execution_environment
        ),
        None,
    )
    if managed and declared:
        # **同一个应用**被两条权威各说一套才叫冲突。不同应用不冲突：一个客户可以同时有
        # "现役正式版（文件登记）+ 新草稿（受管行）"，把"租户里有别的应用"当成冲突，
        # 会让补开/换副本这件事永远做不了。
        if sorted(managed["knowledge_base_ids"]) != sorted(declared["knowledge_base_ids"]) or any(
            bool(managed[key]) != bool(declared[key]) for key in ("live_order_status", "live_billing_record")
        ):
            raise ValueError(_CONFLICT)
        return declared
    return managed or declared


def frozen_settings(target, *, documents=False):
    from aiops_diagnostics.consumer.consumer_documents import SCHEMA

    if target.get("source") == "managed":
        # 受管目标：动态知识回答，文案与判定在代码里（不读内容文件）。
        documents = True
    if not documents and not target.get("content_file"):
        raise ValueError("旧版FAQ流程缺少内容文件；请先升级为动态知识回答")
    return {
        "photo_enabled": target.get("photo_enabled", False),
        "content": {
            "schema": SCHEMA,
            "state": "published",
            "tenant_id": target["tenant_id"],
            "environment": target["environment"],
        }
        if documents
        else load_content(
            target["content_file"], tenant=target["tenant_id"], environment=target["environment"]
        ),
        **{key: target[key] for key in ("knowledge_base_ids", "live_order_status", "live_billing_record")},
    }


def route_scenario(settings, binding):
    if binding.business_entry == "operator":
        from aiops_diagnostics.execution.node_publication import target_for as node_target

        target = node_target(settings, binding)
        if target is not None:
            return target.scenario
    if settings.chat_route_bindings_file:
        rows = [
            row
            for row in ChatRouteBindingStore(settings.chat_route_bindings_file).all()
            if row.app_id == binding.app_id
            and row.environment == settings.execution_environment
            and tenant_matches(row.tenant_id, binding.tenant_id)
            and row.entry == binding.business_entry
        ]
        if len(rows) > 1:
            raise ValueError("app has ambiguous published routes")
        if rows:
            return rows[0].scenario
    if target_for(settings, binding):
        return SCENARIO
    return "consumer_own_order_status"


# ── 受管客户端应用的来源快照与首次引导 ──────────────────────────────────
#
# 客户端应用是**现役 Chatflow 的副本**，所以开通要回答三个问题：来源是哪一份已发布内容
# （`workflow_of`）、这份内容带着什么只读能力（`flags_of`）、目标客户第一次发布时那个
# 还不存在的 agent 用什么配置建（`bootstrap_config`）。三者都是纯函数，便于在没有 Dify
# 的环境里断言。


def workflow_of(workflow) -> str:
    """一份编排内容的稳定摘要：用来回答"来源在本轮期间变过没有"。"""
    import hashlib

    return hashlib.sha256(
        json.dumps(workflow, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def flags_of(workflow) -> dict:
    """来源编排自己声明的只读能力开关。

    读的是 DSL 的环境变量（`cpms_status_enabled` / `cpms_billing_enabled`），不是我们的
    默认值：来源应用把账单关掉了，复制出来的副本也不该悄悄打开。
    """
    rows = (workflow or {}).get("environment_variables") or []
    found = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        value = row.get("value")
        if name == "cpms_status_enabled":
            found["live_order_status"] = value == "true"
        elif name == "cpms_billing_enabled":
            found["live_billing_record"] = value == "true"
    return {
        "live_order_status": bool(found.get("live_order_status")),
        "live_billing_record": bool(found.get("live_billing_record")),
    }


def answer_prompt(document: dict) -> str:
    """Chatflow 里答复节点的提示词正文 —— 首次建 agent 时它是 `prompt` 的来源。

    找不到就返回空串，让调用方拒绝：编一段提示词塞进去，等于在客户的正式版本里留下一段
    谁都没写过的文字。
    """
    nodes = ((document.get("workflow") or {}).get("graph") or {}).get("nodes") or []
    for node in nodes:
        data = node.get("data") or {}
        if data.get("type") != "llm":
            continue
        template = data.get("prompt_template") or []
        if isinstance(template, list):
            for item in template:
                text = str((item or {}).get("text") or "").strip()
                if text:
                    return text[:8000]
        text = str(data.get("prompt") or "").strip()
        if text:
            return text[:8000]
    return ""


def answer_model(document: dict) -> str:
    nodes = ((document.get("workflow") or {}).get("graph") or {}).get("nodes") or []
    for node in nodes:
        data = node.get("data") or {}
        if data.get("type") != "llm":
            continue
        name = str(((data.get("model") or {}).get("name")) or "")
        if name:
            return name
    return ""


def bootstrap_config(knowledge_base_ids, targets):
    """首次发布时用来建 agent 的配置工厂：`document -> dict`。

    它拿到的是 `sync_workflow_from_dify` 已经导出的那份文档，所以不会再导一次 ——
    "建 agent 用的提示词"和"随后冻结的编排"因此必然是同一份内容。
    发布门仍会校验模型白名单与知识库归属。
    """

    def build(document: dict) -> dict:
        prompt = answer_prompt(document)
        if not prompt:
            raise ValueError("consumer app has no prompt to bootstrap an agent")
        return {
            "agent_type": "customer",
            "prompt": prompt,
            "knowledge_base_ids": tuple(knowledge_base_ids),
            "model": answer_model(document),
            "output_contract": "blocks-v1",
        }

    return build
