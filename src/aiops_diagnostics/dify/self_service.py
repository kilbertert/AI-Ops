"""受管智能体生命周期：工作区授权、状态核对、受管发布、版本撤回/恢复与知识授权。

支持单控制面跨环境管理（ADR-0372）：
- 本环境应用（`environment == settings.execution_environment`）直接读写控制面本地 `gateway.db`；
- 远端独立客户机环境应用（如 `jiejia`）仅允许控制面目录中显式授权的 `managers` 操作，
  所有状态读取、发布、回滚与知识库授权均经 `remote_app_management` 读写客户机 `gateway.db`。

旧版 `/apps` 顶栏「复制并开通客户 AI 服务」异步任务流已按 ADR-0372 彻底退役，
新客户环境开通统一收敛至 `/aiops/environments` 5 步向导。
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path

import yaml

from aiops_diagnostics.execution import execution_manifest, node_publication
from aiops_diagnostics.execution.agent_runs import AgentRunStore
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.publication.agent_manifest import admin_context

from . import dual_entry, remote_app_management, service_keys
from . import self_service_config as config
from . import self_service_identity as auth
from . import self_service_prepare as prepare
from .dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
from .dify_dsl_pull import DifyDslSource, _declared_dataset_ids
from .dify_run_store import DifyRunStore
from .dify_workspaces import DIFY_WORKSPACE_UNRESOLVED, DifyWorkspaceError, DifyWorkspaces
from .self_service_store import SelfServiceStore, SetupConflict


class SelfService:
    def __init__(self, context):
        self.context = context
        self.settings = context.settings
        self.store = SelfServiceStore(context.settings.database_file)
        self.console = DifyConsoleAdmin(
            ConsoleApiSettings(self.settings.dify_console_base_url, self.settings.dify_console_api_key)
        )
        self.routes = ChatRouteBindingStore(self.settings.database_file)
        self.lock = threading.RLock()

    # ── 受管应用目录 ──────────────────────────────────────────────────

    def is_remote_app(self, row: dict) -> bool:
        env = str(row.get("environment") or "").strip()
        return bool(env) and env != str(self.settings.execution_environment)

    def managed(self, app_id: str):
        """判断 Dify 应用是否受管，并返回登记行（含端、场景与所属环境）。"""
        row = self.store.app(app_id)
        if row:
            return row
        return self._adopt_operator(app_id) or self._adopt_consumer(app_id)

    def _adopt_operator(self, app_id: str):
        binding = config.app_binding(self.settings, app_id)
        t = node_publication.target_for(self.settings, binding) if binding else None
        if not t or t.entry != "operator":
            return None
        profile_id = next(
            (
                k
                for k, p in config.profiles(self.settings).items()
                if p["target"]
                == {key: value for key, value in asdict(t).items() if key not in {"app_id", "tenant_id"}}
            ),
            None,
        )
        if not profile_id:
            return None
        return {
            "app_id": app_id,
            "tenant_id": binding.tenant_id,
            "agent_name": binding.agent_name,
            "workspace_id": DifyWorkspaces(self.settings.database_file).resolve(
                binding.tenant_id, self.settings.environment
            ),
            "profile_id": profile_id,
            "revision": 1,
            "entry": "operator",
            "scenario": dual_entry.SCENARIO_OPERATOR,
            "environment": self.settings.execution_environment,
            "targets_json": "{}",
        }

    def _adopt_consumer(self, app_id: str):
        """现役客户端应用：注册表里有绑定、且管理表里有一条 active 的客户端路由。"""
        from aiops_diagnostics.dify.dify_app_registry import DifyAppRegistry

        binding = next(
            (
                b
                for b in DifyAppRegistry(self.settings.database_file).all()
                if b.app_id == app_id and b.business_entry == "consumer"
            ),
            None,
        )
        if binding is None:
            return None
        route = self.route_for(binding.tenant_id, "consumer")
        if route is None or route.app_id != app_id:
            return None
        from aiops_diagnostics.consumer.consumer_publication import file_target

        declared = file_target(self.settings, binding)
        targets = (
            {
                key: declared[key]
                for key in ("knowledge_base_ids", "live_order_status", "live_billing_record", "photo_enabled")
            }
            if declared
            else {}
        )
        return {
            "app_id": app_id,
            "tenant_id": binding.tenant_id,
            "agent_name": binding.agent_name,
            "workspace_id": DifyWorkspaces(self.settings.database_file).resolve(
                binding.tenant_id, self.settings.environment
            ),
            "profile_id": "",
            "revision": 1,
            "entry": "consumer",
            "scenario": dual_entry.SCENARIO_CONSUMER,
            "environment": self.settings.execution_environment,
            "targets_json": json.dumps(targets, ensure_ascii=False, sort_keys=True),
        }

    def authorize_app(self, caller, app_id: str):
        row = self.managed(app_id)
        if row is None:
            raise auth.SetupRefused("SETUP_APP_NOT_MANAGED")
        if self.is_remote_app(row):
            auth.require_environment_manager(self.context, caller, row["environment"])
            return row
        if row["tenant_id"] != caller.tenant_id:
            auth.require_target(self.context, caller, row["tenant_id"])
        return row

    def workspace_scope(self, caller, workspace_id: str) -> tuple[str, str]:
        """校验发起人是否有权进入指定 Dify 工作区，返回 `(tenant_id, environment)`。"""
        spaces = DifyWorkspaces(self.settings.database_file)
        try:
            tenant = spaces.tenant_for_workspace(workspace_id, self.settings.environment)
        except DifyWorkspaceError as exc:
            if exc.code == DIFY_WORKSPACE_UNRESOLVED and auth.management_registry_path(self.context):
                entry = spaces.entry_for_workspace(workspace_id)
                auth.require_environment_manager(self.context, caller, entry["environment"])
                return entry["tenant_id"], entry["environment"]
            raise
        if tenant != caller.tenant_id:
            auth.require_target(self.context, caller, tenant)
        return tenant, self.settings.environment

    def workspace(self, caller, workspace_id: str) -> str:
        """兼容入口：返回工作区所属租户编号。"""
        return self.workspace_scope(caller, workspace_id)[0]

    def adopt_draft(self, caller, app_id: str) -> bool:
        """本租户原生副本沿用已开通的连接；应用内容仍经发布器校验。"""
        try:
            workspace_id = DifyWorkspaces(self.settings.database_file).resolve(
                caller.tenant_id, self.settings.environment
            )
        except DifyWorkspaceError:
            return False
        doc = self.console._call(workspace_id, "/console/api/apps/" + app_id)
        mc = doc.get("model_config") or {}
        if not any(t.get("provider_name") == "cpms-node" for t in mc.get("agent_mode", {}).get("tools", [])):
            return False
        configured = {
            r["profile_id"]
            for r in self.store.apps()
            if r["tenant_id"] == caller.tenant_id and not self.is_remote_app(r)
        }
        if len(configured) != 1 or _declared_dataset_ids(mc):
            raise auth.SetupRefused("SETUP_APP_NOT_MANAGED")
        profile_id = configured.pop()
        source = DifyDslSource(
            self.settings.dify_console_base_url, app_id, self.settings.dify_console_api_key, workspace_id
        )
        node_publication.candidate(
            source, node_publication.managed_target(self.settings, profile_id, app_id, caller.tenant_id)
        )
        self.store.register(
            app_id=app_id,
            tenant_id=caller.tenant_id,
            workspace_id=workspace_id,
            agent_name="operator-" + app_id,
            profile_id=profile_id,
            actor=caller.user_id,
            entry="operator",
            environment=self.settings.execution_environment,
        )
        return True

    # ── 正式指针 ──────────────────────────────────────────────────────

    def route(self, tenant: str):
        """管家端的正式路由。"""
        return self.route_for(tenant, "operator")

    def route_for(self, tenant: str, entry: str):
        return self.routes.get(self.settings.execution_environment, tenant, entry, dual_entry.scenario(entry))

    # ── 状态 ──────────────────────────────────────────────────────────

    def status(self, caller, app_id: str):
        row = self.authorize_app(caller, app_id)
        if self.is_remote_app(row):
            return remote_app_management.remote_status(self, caller, row)
        env_name, tenant_name = auth.environment_display(
            self.context, self.settings.environment, row["tenant_id"]
        )
        if row["entry"] != "operator":
            return {
                **self._consumer_status(caller, row),
                "environment": self.settings.environment,
                "environment_name": env_name,
                "tenant_name": tenant_name,
                "remote_managed": False,
            }
        if not row["profile_id"]:
            raise auth.SetupRefused("SETUP_APP_NOT_MANAGED")
        profile = config.profiles(self.settings)[row["profile_id"]]
        t = node_publication.managed_target(self.settings, row["profile_id"], app_id, row["tenant_id"])
        template = execution_manifest.load(t.manifest_file)
        route = self.route(row["tenant_id"])
        published_prompt = ""
        published_tools = []
        if route and route.app_id == app_id:
            snapshot = self.context.agent_manager.store.version(
                route.agent_id, row["tenant_id"], route.agent_version_no
            ).snapshot
            manifest = json.loads(snapshot["node_manifest"])
            published_prompt = node_publication.business_prompt(manifest["system_messages"][0])
            published_tools = [item["name"] for item in manifest["tools"]]
        return {
            "published_prompt": published_prompt,
            "published_tools": published_tools,
            "managed": True,
            "entry": "operator",
            "app_id": app_id,
            "platform": caller.platform,
            "model": template.model_key,
            "model_options": json.loads(Path(t.manifest_file).read_text())["model_options"],
            "published": bool(route and route.active and route.app_id == app_id),
            "version_no": route.agent_version_no if route and route.app_id == app_id else None,
            "route_revision": route.revision if route else 0,
            "profile": profile["name"],
            "channel": self._channel_state(row["tenant_id"], "operator")["state"],
            "environment": self.settings.environment,
            "environment_name": env_name,
            "tenant_name": tenant_name,
            "remote_managed": False,
        }

    def _consumer_status(self, caller, row):
        targets = json.loads(row.get("targets_json") or "{}")
        route = self.route_for(row["tenant_id"], "consumer")
        model = ""
        draft_sha256 = ""
        knowledge_base_ids = []
        try:
            doc = self.console._call(row["workspace_id"], "/console/api/apps/" + row["app_id"])
            if isinstance(doc, dict):
                model = str((doc.get("model_config") or {}).get("model", {}).get("name") or "")
        except Exception:  # noqa: BLE001 - 读模型名失败只降级展示
            model = ""
        try:
            from aiops_diagnostics.consumer import consumer_publication
            from aiops_diagnostics.dify.dify_dsl_pull import DifyDslClient, workflow_content

            source = DifyDslSource(
                self.settings.dify_console_base_url,
                row["app_id"],
                self.settings.dify_console_api_key,
                row["workspace_id"],
            )
            workflow = workflow_content(DifyDslClient(source).export_dsl())
            draft_sha256 = consumer_publication.workflow_of(workflow)
            knowledge_base_ids = list(prepare.consumer_source_ids({"workflow": workflow}))
        except Exception:  # noqa: BLE001 - 读不到草稿摘要只降级展示（发布时会拒绝缺摘要的客户端发布）
            draft_sha256 = ""
        channel = self._channel_state(row["tenant_id"], "consumer")
        published = bool(route and route.active and route.app_id == row["app_id"])
        return {
            "managed": True,
            "entry": "consumer",
            "app_id": row["app_id"],
            "platform": caller.platform,
            "model": model,
            "model_options": {},
            "published": published,
            "version_no": route.agent_version_no if route and route.app_id == row["app_id"] else None,
            "route_revision": route.revision if route else 0,
            "profile": "",
            "knowledge_base_ids": knowledge_base_ids,
            "live_app_id": route.app_id if route and route.active else None,
            "live_version_no": route.agent_version_no if route and route.active else None,
            "live_order_status": bool(targets.get("live_order_status")),
            "live_billing_record": bool(targets.get("live_billing_record")),
            "channel": channel["state"],
            "channel_note": channel["note"],
            "draft_sha256": draft_sha256,
            "readiness": dual_entry.readiness(
                {"app_id": row["app_id"], "published": published, "state": ""}, channel
            ),
        }

    def _channels(self):
        return dual_entry.channels(
            getattr(self.settings, "environment_manifest_file", ""),
            self.settings.execution_environment,
        )

    def _channel_state(self, tenant_id: str, entry: str) -> dict:
        declared = self._channels()
        if (tenant_id, entry) in declared:
            return dual_entry.channel_state(declared, tenant_id, entry)
        return dual_entry.channel_state(
            declared, tenant_id, entry, observed=self._channel_observed(tenant_id, entry)
        )

    def _channel_observed(self, tenant_id: str, entry: str) -> bool:
        try:
            if entry == "operator":
                return AgentRunStore(self.settings.database_file).has_succeeded_for_tenant(tenant_id)
            return DifyRunStore(self.settings.database_file).has_succeeded_for_tenant(tenant_id)
        except Exception:  # noqa: BLE001 - fail-closed：查不出来按未接入显示
            return False

    # ── 知识库授权 ────────────────────────────────────────────────────

    def knowledge(self, caller, app_id: str):
        row = self.authorize_app(caller, app_id)
        if row["entry"] != "consumer":
            raise auth.SetupRefused("SETUP_SOURCE_NOT_CONSUMER")
        if self.is_remote_app(row):
            return remote_app_management.remote_knowledge(self, caller, row)
        from aiops_diagnostics.consumer.consumer_knowledge import owned_libraries

        try:
            libraries = owned_libraries(self.context.runtime, row["tenant_id"], row["app_id"])
        except ValueError as exc:
            raise auth.SetupRefused("SETUP_KNOWLEDGE_FAILED") from exc
        return {
            "libraries": [
                {
                    "id": r["knowledge_base_id"],
                    "name": r.get("name") or r["knowledge_base_id"],
                    "allowed": r["audience"] == "customer",
                }
                for r in libraries
            ]
        }

    def set_knowledge_access(self, caller, app_id: str, knowledge_base_id: str, allowed: bool):
        row = self.authorize_app(caller, app_id)
        if row["entry"] != "consumer":
            raise auth.SetupRefused("SETUP_SOURCE_NOT_CONSUMER")
        if self.is_remote_app(row):
            return remote_app_management.remote_set_knowledge_access(
                self, caller, row, knowledge_base_id, allowed
            )
        from aiops_diagnostics.consumer.knowledge_access import KnowledgeGrants

        libraries = self.knowledge(caller, app_id)
        if knowledge_base_id not in {r["id"] for r in libraries["libraries"]}:
            raise auth.SetupRefused("SETUP_KNOWLEDGE_CROSS_TENANT")
        grants = KnowledgeGrants(self.settings.database_file)
        scope = row["tenant_id"], self.settings.execution_environment, knowledge_base_id
        if allowed:
            grants.grant(*scope, caller.user_id)
        else:
            grants.revoke(*scope)
        return {
            "id": knowledge_base_id,
            "allowed": knowledge_base_id in grants.granted(*scope[:2]),
        }

    # ── 发布 ──────────────────────────────────────────────────────────

    def publish(self, caller, app_id: str, revision: int, expected_draft=None):
        with self.lock:
            row = self.authorize_app(caller, app_id)
            if self.is_remote_app(row):
                return remote_app_management.remote_publish(self, caller, row, revision, expected_draft)
            if row["entry"] == "consumer":
                from aiops_diagnostics.consumer.agent_contract import ConsumerFlowError

                try:
                    return self._publish_consumer(caller, row, revision, expected_draft)
                except ConsumerFlowError as exc:
                    self.context.store.record_publish_audit(
                        app_id=app_id,
                        action="refused",
                        reason_code="CONSUMER_FLOW_INVALID",
                        tenant_id=row["tenant_id"],
                        business_entry="consumer",
                        agent_name=row["agent_name"],
                        company_user_id=caller.user_id,
                    )
                    raise ConsumerFlowError(
                        str(exc) + "；请核对当前应用草稿的选库，本次提示词与选库未发布"
                    ) from exc
            doc = self.console._call(row["workspace_id"], "/console/api/apps/" + app_id)
            mc = doc["model_config"]
            if _declared_dataset_ids(mc):
                raise auth.SetupRefused("SETUP_KNOWLEDGE_NOT_SUPPORTED")
            if expected_draft is not None and expected_draft != draft_fingerprint(mc):
                raise SetupConflict("SETUP_DRAFT_CHANGED")
            current = self.route(row["tenant_id"])
            if (current.revision if current else 0) != revision:
                raise SetupConflict("SETUP_ROUTE_CHANGED")
            self.store.register(
                app_id=app_id,
                tenant_id=row["tenant_id"],
                workspace_id=row["workspace_id"],
                agent_name=row["agent_name"],
                profile_id=row["profile_id"],
                actor=caller.user_id,
                entry="operator",
                environment=self.settings.execution_environment,
            )
            source = DifyDslSource(
                self.settings.dify_console_base_url,
                app_id,
                self.settings.dify_console_api_key,
                row["workspace_id"],
            )
            t = node_publication.managed_target(self.settings, row["profile_id"], app_id, row["tenant_id"])
            from aiops_diagnostics.execution.node_capabilities import require_compatible

            require_compatible(self.settings.database_file, t, json.loads(Path(t.manifest_file).read_text()))
            binding = config.app_binding(self.settings, app_id)
            outcome = node_publication.publish(
                self.context.agent_manager,
                admin_context(row["tenant_id"]),
                binding,
                source,
                t,
                operator_preview={"pre_prompt": mc["pre_prompt"], "tools": mc["agent_mode"]["tools"]},
            )
            node_publication.advance(self.context, outcome, binding, t, revision, register_source=True)
            self.context.store.record_publish_audit(
                app_id=app_id,
                action="published",
                reason_code="",
                tenant_id=t.tenant_id,
                business_entry="operator",
                agent_name=binding.agent_name,
                company_user_id=caller.user_id,
                company_username="",
                dify_account_id="",
                version_no=outcome.version_no,
            )
            return {"version_no": outcome.version_no, "action": outcome.action, "app_id": app_id}

    def _publish_consumer(self, caller, row, revision: int, expected_draft=None):
        from aiops_diagnostics.consumer import consumer_documents, consumer_publication
        from aiops_diagnostics.consumer.consumer_knowledge import owned_libraries
        from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
        from aiops_diagnostics.dify.dify_dsl_pull import (
            DifyDslClient,
            sync_workflow_from_dify,
            workflow_content,
        )
        from aiops_diagnostics.publication.route_advancement import advance_route_binding

        targets = json.loads(row.get("targets_json") or "{}")
        binding = DifyAppBinding(row["tenant_id"], "consumer", row["app_id"], row["agent_name"])
        target = consumer_publication.target_for(self.settings, binding)
        if target is None:
            raise auth.SetupRefused("SETUP_APP_NOT_MANAGED")
        source = DifyDslSource(
            self.settings.dify_console_base_url,
            row["app_id"],
            self.settings.dify_console_api_key,
            row["workspace_id"],
        )
        document = yaml.safe_load(DifyDslClient(source).export_dsl())
        workflow = workflow_content(yaml.safe_dump(document, allow_unicode=True))
        if not consumer_documents.enabled(workflow):
            raise auth.SetupRefused("SETUP_CONSUMER_CONTENT_REQUIRED")
        if expected_draft is not None and expected_draft != consumer_publication.workflow_of(workflow):
            raise SetupConflict("SETUP_DRAFT_CHANGED")
        service_keys.ensure(
            self.console,
            self.settings.chat_dify_key_dir,
            row["workspace_id"],
            row["app_id"],
            database=self.settings.database_file,
        )
        settings = consumer_publication.frozen_settings(target, documents=True)
        bootstrap = consumer_publication.bootstrap_config(settings["knowledge_base_ids"], targets)
        outcome = sync_workflow_from_dify(
            self.context.agent_manager,
            admin_context(row["tenant_id"]),
            binding,
            source,
            consumer_settings=settings,
            consumer_callback_base=self.settings.public_base_url,
            consumer_libraries=lambda: owned_libraries(self.context.runtime, row["tenant_id"], row["app_id"]),
            bootstrap=bootstrap,
            expected_workflow=workflow,
            bootstrap_agent_id=self.store.recoverable_consumer_draft(
                row["app_id"], row["tenant_id"], row["workspace_id"], self.settings.execution_environment
            ),
        )
        if outcome.action == "refused":
            raise auth.SetupRefused("SETUP_AGENT_UNAVAILABLE")
        advance_route_binding(
            self.context,
            outcome,
            binding,
            initiator=(caller.user_id, "", ""),
            expected_revision=revision,
        )
        self.context.store.record_publish_audit(
            app_id=row["app_id"],
            action="published",
            reason_code="",
            tenant_id=row["tenant_id"],
            business_entry="consumer",
            agent_name=row["agent_name"],
            company_user_id=caller.user_id,
            company_username="",
            dify_account_id="",
            version_no=outcome.version_no,
        )
        return {"version_no": outcome.version_no, "action": outcome.action, "app_id": row["app_id"]}

    # ── 版本 ──────────────────────────────────────────────────────────

    def versions(self, caller, app_id: str):
        row = self.authorize_app(caller, app_id)
        if self.is_remote_app(row):
            return remote_app_management.remote_versions(self, caller, row)
        agent = next(
            (
                a
                for a in self.context.agent_manager.store.list(row["tenant_id"])
                if a.name == row["agent_name"]
            ),
            None,
        )
        if not agent:
            return []
        with self.store.connection() as db:
            versions = db.execute(
                "SELECT version_no,snapshot_json FROM agent_versions "
                "WHERE agent_id=? AND tenant_id=? ORDER BY version_no DESC",
                (agent.agent_id, row["tenant_id"]),
            ).fetchall()
        return [v["version_no"] for v in versions if json.loads(v["snapshot_json"]).get("app_id") == app_id]

    def change_version(self, caller, app_id: str, revision: int, version=None):
        with self.lock:
            row = self.authorize_app(caller, app_id)
            if self.is_remote_app(row):
                return remote_app_management.remote_change_version(self, caller, row, revision, version)
            current = self.route_for(row["tenant_id"], row["entry"])
            if not current or current.app_id != app_id or current.revision != revision:
                raise SetupConflict("SETUP_ROUTE_CHANGED")
            if version is None:
                retired = self.routes.retract(
                    environment=current.environment,
                    tenant_id=current.tenant_id,
                    entry=current.entry,
                    scenario=current.scenario,
                    expected_revision=revision,
                    updated_by=caller.user_id,
                )
                if current.entry == "operator":
                    self.context.operator_cpms.retract(current)
                action = "retracted"
                version_no = current.agent_version_no
            else:
                from .dify_dsl_pull import SyncOutcome

                snapshot = self.context.agent_manager.store.version(
                    current.agent_id, current.tenant_id, version
                ).snapshot
                if snapshot.get("app_id") != app_id or snapshot.get("workspace_id") != row["workspace_id"]:
                    raise auth.SetupRefused("SETUP_VERSION_MISMATCH")
                if current.entry == "consumer":
                    frozen = snapshot.get("consumer_service")
                    content = frozen.get("content") if isinstance(frozen, dict) else None
                    if isinstance(content, dict) and content.get("environment") not in (
                        None,
                        current.environment,
                    ):
                        raise auth.SetupRefused("SETUP_VERSION_ENVIRONMENT_MISMATCH")
                outcome = SyncOutcome(
                    row["tenant_id"],
                    current.entry,
                    app_id,
                    row["agent_name"],
                    "synced",
                    version,
                    workflow_id=str(snapshot.get("workflow_id") or ""),
                    agent_id=current.agent_id,
                )
                if current.entry == "operator":
                    execution_manifest.from_text(snapshot["node_manifest"])
                    binding = config.app_binding(self.settings, app_id)
                    t = node_publication.managed_target(
                        self.settings, row["profile_id"], app_id, row["tenant_id"]
                    )
                    from aiops_diagnostics.execution.node_capabilities import require_compatible

                    require_compatible(self.settings.database_file, t, json.loads(snapshot["node_manifest"]))
                    node_publication.advance(
                        self.context, outcome, binding, t, revision, register_source=True
                    )
                else:
                    from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
                    from aiops_diagnostics.publication.route_advancement import advance_route_binding

                    advance_route_binding(
                        self.context,
                        outcome,
                        DifyAppBinding(row["tenant_id"], "consumer", app_id, row["agent_name"]),
                        initiator=(caller.user_id, "", ""),
                        expected_revision=revision,
                    )
                retired = self.route_for(row["tenant_id"], row["entry"])
                action = "restored"
                version_no = version
            self.context.store.record_publish_audit(
                app_id=app_id,
                action=action,
                tenant_id=row["tenant_id"],
                business_entry=row["entry"],
                agent_name=row["agent_name"],
                company_user_id=caller.user_id,
                version_no=version_no,
            )
            return {
                "app_id": app_id,
                "entry": row["entry"],
                "revision": retired.revision,
                "state": retired.state,
                "version_no": version_no,
            }


draft_fingerprint = remote_app_management.draft_fingerprint
