"""同一开通生命周期供控制台调用；公司令牌仅存在执行期间的内存中。"""

import json
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

from aiops_diagnostics.execution import execution_manifest, node_publication
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.publication.agent_manifest import admin_context

from . import self_service_config as config
from . import self_service_identity as auth
from . import self_service_prepare as prepare
from .dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
from .dify_dsl_pull import DifyDslSource, _declared_dataset_ids
from .dify_workspaces import DifyWorkspaces
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
        self.enqueued = set()

    def enqueue(self, job_id, session):
        with self.lock:
            if job_id in self.enqueued:
                return
            self.store.pending(job_id)
            self.enqueued.add(job_id)

        def execute():
            try:
                self.run(job_id, session)
            finally:
                with self.lock:
                    self.enqueued.discard(job_id)

        try:
            self.context.runtime._executor.submit(execute)
        except Exception:
            with self.lock:
                self.enqueued.discard(job_id)
            raise

    def managed(self, app_id):
        row = self.store.app(app_id)
        if row:
            return row
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
        }

    def authorize_app(self, caller, app_id):
        row = self.managed(app_id)
        if row is None:
            raise auth.SetupRefused("SETUP_APP_NOT_MANAGED")
        if row["tenant_id"] != caller.tenant_id:
            auth.require_target(self.context, caller, row["tenant_id"])
        return row

    def workspace(self, caller, workspace_id):
        tenant = DifyWorkspaces(self.settings.database_file).tenant_for_workspace(
            workspace_id, self.settings.environment
        )
        if tenant != caller.tenant_id:
            auth.require_target(self.context, caller, tenant)
        return tenant

    def adopt_draft(self, caller, app_id):
        """本租户原生副本沿用已开通的连接；应用内容仍经发布器校验。"""
        workspace_id = DifyWorkspaces(self.settings.database_file).resolve(
            caller.tenant_id, self.settings.environment
        )
        doc = self.console._call(workspace_id, "/console/api/apps/" + app_id)
        mc = doc.get("model_config") or {}
        if not any(t.get("provider_name") == "cpms-node" for t in mc.get("agent_mode", {}).get("tools", [])):
            return False
        configured = {r["profile_id"] for r in self.store.apps() if r["tenant_id"] == caller.tenant_id}
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
        )
        return True

    def route(self, tenant):
        return self.routes.get(
            self.settings.execution_environment, tenant, "operator", "operator_order_diagnosis"
        )

    def status(self, caller, app_id):
        row = self.authorize_app(caller, app_id)
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
            published_prompt = manifest["system_messages"][0].removesuffix(
                "\n" + node_publication.OPERATOR_OUTPUT
            )
            published_tools = [t["name"] for t in manifest["tools"]]
        return {
            "published_prompt": published_prompt,
            "published_tools": published_tools,
            "managed": True,
            "app_id": app_id,
            "platform": caller.platform,
            "model": template.model_key,
            "model_options": json.loads(Path(t.manifest_file).read_text())["model_options"],
            "published": bool(route and route.active and route.app_id == app_id),
            "version_no": route.agent_version_no if route and route.app_id == app_id else None,
            "route_revision": route.revision if route else 0,
            "profile": profile["name"],
        }

    def create(self, caller, payload):
        tenant = auth.require_target(self.context, caller, payload.tenant_id)
        existing = self.store.request(caller.user_id, payload.request_key)
        if existing:
            frozen = json.loads(existing["request_json"])
            if any(
                frozen[k] != getattr(payload, k)
                for k in ("tenant_id", "name", "source_app_id", "source_version")
            ):
                raise SetupConflict("SETUP_REQUEST_CHANGED")
            return existing
        source = self.authorize_app(caller, payload.source_app_id)
        route = self.route(source["tenant_id"])
        if not route or not route.active or route.app_id != payload.source_app_id:
            raise auth.SetupRefused("SETUP_SOURCE_NOT_PUBLISHED")
        if route.agent_version_no != payload.source_version:
            raise SetupConflict("SETUP_SOURCE_CHANGED")
        snapshot = self.context.agent_manager.store.version(
            route.agent_id, route.tenant_id, route.agent_version_no
        ).snapshot
        manifest = json.loads(snapshot["node_manifest"])
        prompt = manifest["system_messages"][0]
        suffix = "\n" + node_publication.OPERATOR_OUTPUT
        if prompt.endswith(suffix):
            prompt = prompt[: -len(suffix)]
        request = {
            "tenant_id": tenant["id"],
            "tenant_name": tenant["name"],
            "name": payload.name,
            "source_app_id": payload.source_app_id,
            "source_version": route.agent_version_no,
            "prompt": prompt,
            "tools": [t["name"] for t in manifest["tools"]],
            "profile_id": source["profile_id"],
        }
        return self.store.create(caller.user_id, payload.request_key, request)

    def run(self, job_id, session):
        owner = str(uuid.uuid4())
        if not self.store.claim(job_id, owner):
            return
        row = self.store.get(job_id)
        request = json.loads(row["request_json"])
        resources = json.loads(row["resources_json"])
        step = row["step"]

        def save(next_step):
            nonlocal step
            step = next_step
            self.store.save(job_id, owner, step=step, resources=resources)

        try:
            caller = auth.identity(self.context, session)
            if caller.user_id != row["actor"]:
                raise auth.SetupRefused("SETUP_ACTOR_CHANGED")
            auth.require_target(self.context, caller, request["tenant_id"])
            profile = config.profiles(self.settings)[request["profile_id"]]
            if not resources.get("workspace_id"):
                resources["workspace_id"] = prepare.workspace(
                    self.settings, request["tenant_id"], request["tenant_name"], self.console
                )
            save("knowledge")
            prepare.knowledge(self.context, request["tenant_id"])
            resources["knowledge_api_id"] = prepare.knowledge_connection(
                self.context, resources["workspace_id"], self.console
            )
            resources["knowledge_ready"] = True
            save("dependencies")
            provider = prepare.dependencies(
                self.console, resources["workspace_id"], profile, self.settings.public_base_url
            )
            save("application")
            if not resources.get("app_id"):
                resources["app_id"] = prepare.create_app(
                    self.console,
                    resources["workspace_id"],
                    job_id,
                    request,
                    profile,
                    provider,
                )
            app = self.console._call(resources["workspace_id"], "/console/api/apps/" + resources["app_id"])
            mc = app["model_config"]
            expected = {
                "pre_prompt": request["prompt"],
                "agent_mode": {
                    "tools": [
                        {"provider_id": provider, "provider_type": "api", "tool_name": name, "enabled": True}
                        for name in request["tools"]
                    ]
                },
            }
            frozen_draft = draft_fingerprint(expected)
            if draft_fingerprint(mc) != frozen_draft or _declared_dataset_ids(mc):
                raise SetupConflict("SETUP_DRAFT_CHANGED")
            candidate_target = node_publication.managed_target(
                self.settings, request["profile_id"], resources["app_id"], request["tenant_id"]
            )
            node_publication.candidate(
                DifyDslSource(
                    self.settings.dify_console_base_url,
                    resources["app_id"],
                    self.settings.dify_console_api_key,
                    resources["workspace_id"],
                ),
                candidate_target,
            )
            DifyWorkspaces(self.settings.database_file).mark_ready(
                request["tenant_id"], self.settings.environment
            )
            save("register")
            self.store.register(
                app_id=resources["app_id"],
                tenant_id=request["tenant_id"],
                workspace_id=resources["workspace_id"],
                agent_name="operator-" + resources["app_id"],
                profile_id=request["profile_id"],
                actor=caller.user_id,
            )
            save("publish")
            caller = auth.identity(self.context, session)
            auth.require_target(self.context, caller, request["tenant_id"])
            current = self.route(request["tenant_id"])
            if current is None:
                save("publish")
                outcome = self.publish(caller, resources["app_id"], 0, frozen_draft)
                resources.update(version_no=outcome["version_no"], published=True)
            elif current.app_id == resources["app_id"]:
                # 上次发布成功但完成回执丢失：只对账，不撤销后续人工撤回或切版。
                resources.update(version_no=current.agent_version_no, published=current.active)
            else:
                resources["published"] = False
            self.store.save(job_id, owner, step="complete", resources=resources, state="completed")
        except Exception as exc:
            # 只保留固定业务码，供应商响应可能含凭据。
            code = str(exc) if isinstance(exc, (auth.SetupRefused, SetupConflict)) else "SETUP_STEP_FAILED"
            self.store.save(job_id, owner, step=step, resources=resources, state="failed", error=code)

    def publish(self, caller, app_id, revision, expected_draft=None):
        with self.lock:
            row = self.authorize_app(caller, app_id)
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
            )
            source = DifyDslSource(
                self.settings.dify_console_base_url,
                app_id,
                self.settings.dify_console_api_key,
                row["workspace_id"],
            )
            t = node_publication.managed_target(self.settings, row["profile_id"], app_id, row["tenant_id"])
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

    def versions(self, caller, app_id):
        row = self.authorize_app(caller, app_id)
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

    def change_version(self, caller, app_id, revision, version=None):
        with self.lock:
            row = self.authorize_app(caller, app_id)
            current = self.route(row["tenant_id"])
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
                execution_manifest.from_text(snapshot["node_manifest"])
                binding = config.app_binding(self.settings, app_id)
                t = node_publication.managed_target(
                    self.settings, row["profile_id"], app_id, row["tenant_id"]
                )
                outcome = SyncOutcome(
                    row["tenant_id"],
                    "operator",
                    app_id,
                    row["agent_name"],
                    "synced",
                    version,
                    agent_id=current.agent_id,
                )
                node_publication.advance(self.context, outcome, binding, t, revision, register_source=True)
                retired = self.route(row["tenant_id"])
                action = "restored"
                version_no = version
            self.context.store.record_publish_audit(
                app_id=app_id,
                action=action,
                tenant_id=row["tenant_id"],
                business_entry="operator",
                agent_name=row["agent_name"],
                company_user_id=caller.user_id,
                version_no=version_no,
            )
            return {
                "app_id": app_id,
                "revision": retired.revision,
                "state": retired.state,
                "version_no": version_no,
            }


def draft_fingerprint(mc):
    import hashlib

    body = {
        "pre_prompt": mc.get("pre_prompt"),
        "tools": [
            {k: t.get(k) for k in ("provider_id", "provider_type", "tool_name")}
            | {"enabled": t.get("enabled", True)}
            for t in mc.get("agent_mode", {}).get("tools", [])
        ],
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
