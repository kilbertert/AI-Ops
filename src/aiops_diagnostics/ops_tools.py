"""Dify 负责循环；这里仅执行登记租户的有界读取与证据交付。"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aiops_diagnostics.agent_contracts import OPS_TOOL_NAMES
from aiops_diagnostics.agent_lifecycle import Agent, AgentError, AgentManager
from aiops_diagnostics.charging_status import ChargingStatusError, read_charging_status
from aiops_diagnostics.config import Settings
from aiops_diagnostics.dify_app_registry import DifyAppRegistry
from aiops_diagnostics.dify_workspaces import DifyWorkspaceError, DifyWorkspaces
from aiops_diagnostics.gateway_config import GatewayServerSettings, charging_endpoint
from aiops_diagnostics.ops_bindings import OpsBinding, load_ops_bindings, token_digest
from aiops_diagnostics.order_visibility import tenant_matches
from aiops_diagnostics.query_scope import QueryScope
from aiops_diagnostics.sources import SourceError, scoped_mysql_source


class ChargingInfoInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    order_no: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class OrderListInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page: int = Field(default=1, ge=1, le=1000)
    page_size: int = Field(default=20, ge=1, le=50)


class OpsToolError(RuntimeError):
    def __init__(self, status: int, code: str, *, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.evidence = evidence or {}


class OpsToolService:
    def __init__(self, settings: GatewayServerSettings, manager: AgentManager) -> None:
        self.settings = settings
        self.manager = manager
        self.registry = DifyAppRegistry(settings.database_file)
        self.workspaces = DifyWorkspaces(settings.database_file)
        with closing(sqlite3.connect(settings.database_file)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS ops_tool_audit ("
                "call_id TEXT PRIMARY KEY, evidence_json TEXT NOT NULL)"
            )

    def resolve_binding(self, token: str, tenant: str) -> tuple[OpsBinding, Agent]:
        if not token:
            raise OpsToolError(401, "OPS_CREDENTIAL_REQUIRED")
        if not 32 <= len(token) <= 4096:
            raise OpsToolError(403, "OPS_CREDENTIAL_REJECTED")
        bindings = load_ops_bindings(self.settings.ops_bindings_file)
        digest = token_digest(token)
        if any(
            token_digest(value) == digest
            for value in (
                self.settings.dify_tools_api_key,
                self.settings.dify_publish_api_key,
                self.settings.dify_knowledge_api_key,
                self.settings.dify_trial_api_key,
                self.settings.dify_console_api_key,
                self.settings.dify_debug_api_key,
            )
            if value
        ):
            raise OpsToolError(403, "OPS_CREDENTIAL_REJECTED")
        binding = next((b for b in bindings if secrets.compare_digest(b.key_sha256, digest)), None)
        if (
            binding is None
            or not binding.active
            or binding.environment != self.settings.environment
            or not tenant_matches(binding.tenant_id, tenant)
        ):
            raise OpsToolError(403, "OPS_BINDING_REJECTED")
        try:
            workspace = self.workspaces.resolve(binding.tenant_id, binding.environment)
            registration = self.registry.lookup(binding.tenant_id, "operator")
            agent = self.manager.store.get(binding.agent_id, binding.tenant_id)
            if (
                workspace != binding.workspace_id
                or registration is None
                or registration.app_id != binding.app_id
                or registration.agent_name != agent.name
                or agent.config.agent_type != "operations"
                or agent.status not in {"draft", "published"}
                or agent.published_version is None
            ):
                raise OpsToolError(403, "OPS_BINDING_REJECTED")
        except (AgentError, DifyWorkspaceError) as exc:
            raise OpsToolError(403, "OPS_BINDING_REJECTED") from exc
        return binding, agent

    def authorize(self, token: str, tenant: str, tool: str) -> tuple[OpsBinding, int]:
        binding, agent = self.resolve_binding(token, tenant)
        try:
            version = self.manager.store.version(agent.agent_id, binding.tenant_id, agent.published_version)
        except AgentError as exc:
            raise OpsToolError(403, "OPS_BINDING_REJECTED") from exc
        allowed = version.snapshot.get("allowed_tools", ())
        if not isinstance(allowed, (list, tuple)) or tool not in OPS_TOOL_NAMES or tool not in allowed:
            raise OpsToolError(
                403, "OPS_TOOL_NOT_PUBLISHED", evidence=self._identity(binding, version.version_no)
            )
        return binding, version.version_no

    @staticmethod
    def _identity(binding: OpsBinding, version: int) -> dict[str, Any]:
        return {
            "subject_type": "tenant_service",
            "tenant_id": binding.tenant_id,
            "environment": binding.environment,
            "registered_workspace_id": binding.workspace_id,
            "registered_app_id": binding.app_id,
            "agent_id": binding.agent_id,
            "permission_version": version,
        }

    def _record(self, evidence: dict[str, Any]) -> None:
        with closing(sqlite3.connect(self.settings.database_file)) as db, db:
            db.execute(
                "INSERT INTO ops_tool_audit VALUES (?, ?)",
                (evidence["call_id"], json.dumps(evidence, ensure_ascii=False, sort_keys=True)),
            )

    def execute(
        self, token: str, tenant: str, tool: str, inputs: ChargingInfoInput | OrderListInput
    ) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "call_id": uuid.uuid4().hex,
            "tool": tool,
            "observed_at": datetime.now(UTC).isoformat(),
            "outcome": "refused",
            "input_sha256": token_digest(inputs.model_dump_json()),
        }
        try:
            binding, version = self.authorize(token, tenant, tool)
            evidence.update(self._identity(binding, version))
            try:
                base = charging_endpoint(
                    self.settings.charging_status_base_url, binding.tenant_id, require_registration=True
                )
            except ValueError as exc:
                raise OpsToolError(404, "TENANT_ENDPOINT_UNRESOLVED") from exc
            result = self._read(binding, tool, inputs, base)
            current_binding, current_version = self.authorize(token, tenant, tool)
            if binding != current_binding or current_version != version:
                raise OpsToolError(409, "OPS_AUTHORIZATION_CHANGED")
            evidence.update(
                {
                    "outcome": "succeeded",
                    "source": result["source"],
                    "result_sha256": hashlib.sha256(
                        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                }
            )
        except OpsToolError as exc:
            evidence.update(exc.evidence)
            evidence["reason_code"] = exc.code
            self._record_or_refuse(evidence)
            raise
        except (SourceError, ChargingStatusError) as exc:
            evidence["reason_code"] = "OPS_SOURCE_UNAVAILABLE"
            self._record_or_refuse(evidence)
            raise OpsToolError(502, "OPS_SOURCE_UNAVAILABLE") from exc
        except (ValueError, OSError, sqlite3.Error) as exc:
            evidence["reason_code"] = "OPS_CONFIGURATION_UNAVAILABLE"
            self._record_or_refuse(evidence)
            raise OpsToolError(503, "OPS_CONFIGURATION_UNAVAILABLE") from exc
        self._record_or_refuse(evidence)
        return {"result": result, "evidence": evidence}

    def _record_or_refuse(self, evidence: dict[str, Any]) -> None:
        try:
            self._record(evidence)
        except (OSError, sqlite3.Error) as exc:
            raise OpsToolError(503, "OPS_EVIDENCE_UNAVAILABLE") from exc

    def _read(
        self, binding: OpsBinding, tool: str, inputs: ChargingInfoInput | OrderListInput, base: str
    ) -> dict[str, Any]:
        scope = QueryScope(tenant_id=binding.tenant_id, site_ids=None, user_id=None)
        with scoped_mysql_source(Settings.from_config(self.settings.server_config_file), scope) as source:
            if tool == "charging_info" and isinstance(inputs, ChargingInfoInput):
                if len(source.get_orders(inputs.order_no)) != 1:
                    raise OpsToolError(404, "ORDER_NOT_FOUND")
                result = read_charging_status(base, inputs.order_no, tenant_id=binding.tenant_id, user_id="")
                if len(source.get_orders(inputs.order_no)) != 1:
                    raise OpsToolError(404, "ORDER_NOT_FOUND")
                return result
            if tool == "order_list" and isinstance(inputs, OrderListInput):
                rows = source.list_orders(page=inputs.page, page_size=inputs.page_size)
                orders = [
                    {
                        "order_no": str(row["order_no"]),
                        "status_code": row["status"],
                        "site_id": str(row["site_id"]) if row.get("site_id") is not None else None,
                        "created_time": str(row["created_time"])
                        if row.get("created_time") is not None
                        else None,
                    }
                    for row in rows[: inputs.page_size]
                ]
                return {
                    "orders": orders,
                    "page": inputs.page,
                    "page_size": inputs.page_size,
                    "has_more": len(rows) > inputs.page_size,
                    "source": "mysql.ch_order_info",
                    "observed_at": datetime.now(UTC).isoformat(),
                }
        raise OpsToolError(400, "OPS_INVALID_TOOL_INPUT")
