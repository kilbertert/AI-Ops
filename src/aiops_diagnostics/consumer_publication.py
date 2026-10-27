"""WF01发布目标由服务端声明；冻结内容与执行图共用既有Agent版本。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.consumer_knowledge import load_content
from aiops_diagnostics.dify_chat_workflow import WORKFLOW_ID_PATTERN
from aiops_diagnostics.order_visibility import tenant_matches
from aiops_diagnostics.private_files import validate_private_file

SCENARIO = "consumer_service"


def target_for(settings, binding):
    if not settings.consumer_publication_targets_file:
        return None
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
        if (
            row["app_id"] == binding.app_id
            and tenant_matches(row["tenant_id"], binding.tenant_id)
            and row["environment"] == settings.execution_environment
        ):
            found.append(row)
    return found[0] if found else None


def frozen_settings(target, *, documents=False):
    from aiops_diagnostics.consumer_documents import SCHEMA

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
        from aiops_diagnostics.node_publication import target_for as node_target

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
