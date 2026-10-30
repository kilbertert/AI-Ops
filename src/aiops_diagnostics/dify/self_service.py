"""双端开通生命周期：公共准备一次，两端各自准备、发布与对账。

## 形状

一次开通 = 一段**公共准备** + 每个端各自的准备：

```
workspace → knowledge → dependencies        （公共，只做一次）
   └─ consumer:  application → key → register → publish
   └─ operator:  application →      register → publish
```

公共准备只做一次，是因为它准备的是**客户空间**（工作区、RAGFlow 空间、模型凭据、节点
provider），两端共用同一个空间；每端再各自准备自己的应用、凭据和正式指针。这样"只开一端"
不是"少跑一半流程"，而是"这一段本来就属于端"。

## 两端不是同一个事务

一端失败时另一端继续，整体状态是 `partial`，重试只续跑失败的那一端。把它们绑成一个事务，
等于让"客户端凭据被上游限流"顺手把已经成功的管家回滚掉 —— 而回滚外部资源是不可靠的
（Dify 应用删掉可以，工作区/知识空间不行），所以宁可如实报告部分完成。

## 已存在正式版时只建草稿

目标客户已经有该端正式应用时，开通**只建草稿**（`draft_only`），原正式版继续服务；
只有客户主动发布才推进指针。这条不是保守，是规划里明确的业务要求：复制不能自动替换
正在服务的那一版。
"""

import json
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import yaml

from aiops_diagnostics.execution import execution_manifest, node_publication
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.publication.agent_manifest import admin_context

from . import dual_entry, service_keys
from . import self_service_config as config
from . import self_service_identity as auth
from . import self_service_prepare as prepare
from .dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
from .dify_dsl_pull import DifyDslSource, _declared_dataset_ids
from .dify_workspaces import DifyWorkspaces
from .self_service_store import SelfServiceStore, SetupConflict

#: 来源应用在 Dify 里的名字几乎不变，而「工作室」每打开一次就会拉一次目录。缓存的是
#: **已经决定要显示的**标签（含兜底）：Dify 慢或不可用时，失败本身也被缓存，避免每次
#: 打开工作室都撞一轮超时。
_APP_NAME_TTL_SECONDS = 120.0


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
        self._app_names: dict[str, tuple[float, str]] = {}

    # ── 队列 ──────────────────────────────────────────────────────────

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

    # ── 受管应用目录 ──────────────────────────────────────────────────

    def managed(self, app_id):
        """这个 Dify 应用是不是受管的；是就返回目录行（含端与场景）。

        三处来源按顺序：受管目录本身 → 现役管家 profile 推导 → 现役客户端注册表推导。
        后两处是**纳管**：现役客户的应用在页面开通之前就在服务，它们不该因为"没走过
        新流程"而变成不可管理；推导出来的行与真实登记同形，后续发布照常写库。
        """
        row = self.store.app(app_id)
        if row:
            return row
        return self._adopt_operator(app_id) or self._adopt_consumer(app_id)

    def _adopt_operator(self, app_id):
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

    def _adopt_consumer(self, app_id):
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
            entry="operator",
            environment=self.settings.execution_environment,
        )
        return True

    # ── 正式指针 ──────────────────────────────────────────────────────

    def route(self, tenant):
        """管家端的正式路由。保留这个名字：既有调用方问的就是"管家走哪一版"。"""
        return self.route_for(tenant, "operator")

    def route_for(self, tenant, entry):
        return self.routes.get(self.settings.execution_environment, tenant, entry, dual_entry.scenario(entry))

    # ── 来源标签 ──────────────────────────────────────────────────────

    def source_app_name(self, tenant_id, app_id, fallback):
        """来源应用在 **Dify** 里叫什么 —— 运营认得的是这个名字。

        目录里的 ``name`` 曾经取 Gateway 的智能体名（``ops-methodology-700``、
        ``operator-<app_id>``）。那是执行侧标识：它既不是 Dify 应用名，也不出现在任何
        工作室列表里，于是「来源版本」下拉里出现了两个运营「从来没见过、也不知道属于谁」
        的条目。这里改读 Dify 控制台自己的应用名，读不到就退回智能体名 —— 读名失败只降级
        标签，不挡住开通入口。
        """
        now = time.monotonic()
        cached = self._app_names.get(app_id)
        if cached and cached[0] > now:
            return cached[1]
        name = ""
        try:
            workspace_id = DifyWorkspaces(self.settings.database_file).resolve(
                tenant_id, self.settings.environment
            )
            if workspace_id:
                payload = self.console._call(workspace_id, "/console/api/apps/" + app_id)
                if isinstance(payload, dict):
                    name = str(payload.get("name") or "").strip()
        except Exception:  # noqa: BLE001 - 读名失败只降级标签
            name = ""
        label = name[:128] or fallback
        self._app_names[app_id] = (now + _APP_NAME_TTL_SECONDS, label)
        return label

    # ── 状态 ──────────────────────────────────────────────────────────

    def status(self, caller, app_id):
        row = self.authorize_app(caller, app_id)
        if row["entry"] == "consumer":
            return self._consumer_status(caller, row)
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
            "entry": "operator",
            "app_id": app_id,
            "platform": caller.platform,
            "model": template.model_key,
            "model_options": json.loads(Path(t.manifest_file).read_text())["model_options"],
            "published": bool(route and route.active and route.app_id == app_id),
            "version_no": route.agent_version_no if route and route.app_id == app_id else None,
            "route_revision": route.revision if route else 0,
            "profile": profile["name"],
            "channel": dual_entry.channel_state(self._channels(), row["tenant_id"], "operator")["state"],
        }

    def _consumer_status(self, caller, row):
        targets = json.loads(row.get("targets_json") or "{}")
        route = self.route_for(row["tenant_id"], "consumer")
        model = ""
        draft_sha256 = ""
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
            draft_sha256 = consumer_publication.workflow_of(
                workflow_content(DifyDslClient(source).export_dsl())
            )
        except Exception:  # noqa: BLE001 - 读不到草稿摘要只降级展示（发布时会拒绝缺摘要的客户端发布）
            draft_sha256 = ""
        channel = dual_entry.channel_state(self._channels(), row["tenant_id"], "consumer")
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
            "knowledge_base_ids": list(targets.get("knowledge_base_ids") or []),
            "live_order_status": bool(targets.get("live_order_status")),
            "live_billing_record": bool(targets.get("live_billing_record")),
            "channel": channel["state"],
            "channel_note": channel["note"],
            # 页面把它原样发回发布请求：发布冻结的必须是操作者看到的那一份。
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

    # ── 创建 ──────────────────────────────────────────────────────────

    def create(self, caller, payload):
        tenant = auth.require_target(self.context, caller, payload.tenant_id)
        existing = self.store.request(caller.user_id, payload.request_key)
        if existing:
            frozen = json.loads(existing["request_json"])
            if _identity(frozen) != _identity_of(payload):
                raise SetupConflict("SETUP_REQUEST_CHANGED")
            return existing

        entries, chosen = _requested_entries(payload)
        request = {
            "tenant_id": tenant["id"],
            "tenant_name": tenant["name"],
            "name": payload.name,
            "scope": dual_entry.scope_for_entries(entries),
            "entries": list(entries),
            "sources": {},
        }
        for entry in entries:
            spec = chosen[entry]
            source = self.authorize_app(caller, spec.app_id)
            if source["entry"] != entry:
                # 一端的应用不能改个标签当另一端：来源必须**本来就是**这个端。
                raise auth.SetupRefused("SETUP_SOURCE_NOT_PUBLISHED")
            route = self.route_for(source["tenant_id"], entry)
            if not route or not route.active or route.app_id != spec.app_id:
                raise auth.SetupRefused("SETUP_SOURCE_NOT_PUBLISHED")
            if route.agent_version_no != spec.version_no:
                raise SetupConflict("SETUP_SOURCE_CHANGED")
            request["sources"][entry] = self._freeze_source(entry, source, route)
        return self.store.create(caller.user_id, payload.request_key, request)

    def _freeze_source(self, entry, source, route):
        """把来源**已发布快照**冻进请求：之后来源再改也不影响这次开通。"""
        snapshot = self.context.agent_manager.store.version(
            route.agent_id, route.tenant_id, route.agent_version_no
        ).snapshot
        if entry == "operator":
            manifest = json.loads(snapshot["node_manifest"])
            prompt = manifest["system_messages"][0]
            suffix = "\n" + node_publication.OPERATOR_OUTPUT
            if prompt.endswith(suffix):
                prompt = prompt[: -len(suffix)]
            return {
                "app_id": source["app_id"],
                "version_no": route.agent_version_no,
                "profile_id": source["profile_id"],
                "prompt": prompt,
                "tools": [t["name"] for t in manifest["tools"]],
            }
        from aiops_diagnostics.consumer.consumer_publication import flags_of, workflow_of

        workflow = snapshot.get("workflow")
        if not isinstance(workflow, dict) or not snapshot.get("workflow_id"):
            raise auth.SetupRefused("SETUP_SOURCE_NOT_CONSUMER")
        return {
            "app_id": source["app_id"],
            "version_no": route.agent_version_no,
            "workspace_id": source["workspace_id"],
            "workflow_id": snapshot["workflow_id"],
            "graph_sha256": workflow_of(workflow),
            "flags": flags_of(workflow),
        }

    # ── 执行 ──────────────────────────────────────────────────────────

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
            self._prepare_common(request, resources, save)
            entries = request["entries"]
            for entry in entries:
                state = resources.setdefault("entries", {}).setdefault(
                    entry, {"state": "pending", "error_code": ""}
                )
                if state.get("state") == "ready":
                    continue  # 重试续跑：已完成端不重复创建、不重复发布
                try:
                    if entry == "operator":
                        self._run_operator(job_id, request, resources, state, save, caller, session)
                    else:
                        self._run_consumer(job_id, request, resources, state, save, caller, session)
                except Exception as exc:  # noqa: BLE001 - 逐端记录，不牵连另一端
                    import traceback as _tb

                    _tb.print_exc()
                    state.update(state="failed", error_code=_error_code(exc))
                    save(entry + ":failed")
            failed = [e for e in entries if resources["entries"][e].get("state") == "failed"]
            done = [e for e in entries if resources["entries"][e].get("state") in {"ready", "draft"}]
            _mirror(resources, request["entries"])
            if not failed:
                self.store.save(job_id, owner, step="complete", resources=resources, state="completed")
            elif done:
                self.store.save(
                    job_id,
                    owner,
                    step="complete",
                    resources=resources,
                    state="partial",
                    error="SETUP_PARTIAL",
                )
            else:
                self.store.save(
                    job_id,
                    owner,
                    step=step,
                    resources=resources,
                    state="failed",
                    error=resources["entries"][failed[0]]["error_code"] or "SETUP_STEP_FAILED",
                )
        except Exception as exc:
            # 只保留固定业务码，供应商响应可能含凭据。
            code = str(exc) if isinstance(exc, (auth.SetupRefused, SetupConflict)) else "SETUP_STEP_FAILED"
            self.store.save(job_id, owner, step=step, resources=resources, state="failed", error=code)

    def _prepare_common(self, request, resources, save):
        """客户空间的**公共**准备：工作区、知识空间、知识连接。

        模型/插件/节点 provider **不在这里**：它们属于端。只开客户端时去装管家用的
        `cpms-node` provider，会让"客户端的开通"依赖一个它根本不需要的外部动作 ——
        而那个动作失败时，页面会显示"客户端开通失败"，指向一个与它无关的原因。
        """
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
        return None

    def _profile(self, request):
        profiles = config.profiles(self.settings)
        wanted = (request.get("sources", {}).get("operator") or {}).get("profile_id")
        if wanted and wanted in profiles:
            return profiles[wanted]
        # 只开客户端时也要装模型/插件：客服应用和管家共用一个工作区里的模型凭据。
        return next(iter(profiles.values()))

    def _run_operator(self, job_id, request, resources, state, save, caller, session):
        spec = request["sources"]["operator"]
        workspace_id = resources["workspace_id"]
        save("operator:dependencies")
        provider = resources.get("provider_id")
        if not provider:
            provider = prepare.dependencies(
                self.console, workspace_id, self._profile(request), self.settings.public_base_url
            )
            resources["provider_id"] = provider
        save("operator:application")
        if not state.get("app_id"):
            state["app_id"] = prepare.create_app(
                self.console,
                workspace_id,
                job_id,
                {
                    "name": request["name"],
                    "prompt": spec["prompt"],
                    "tools": spec["tools"],
                },
                self._profile(request),
                provider,
            )
            save("operator:application")
        app = self.console._call(workspace_id, "/console/api/apps/" + state["app_id"])
        mc = app["model_config"]
        expected = {
            "pre_prompt": spec["prompt"],
            "agent_mode": {
                "tools": [
                    {"provider_id": provider, "provider_type": "api", "tool_name": name, "enabled": True}
                    for name in spec["tools"]
                ]
            },
        }
        frozen_draft = draft_fingerprint(expected)
        if draft_fingerprint(mc) != frozen_draft or _declared_dataset_ids(mc):
            raise SetupConflict("SETUP_DRAFT_CHANGED")
        candidate_target = node_publication.managed_target(
            self.settings, spec["profile_id"], state["app_id"], request["tenant_id"]
        )
        node_publication.candidate(
            DifyDslSource(
                self.settings.dify_console_base_url,
                state["app_id"],
                self.settings.dify_console_api_key,
                workspace_id,
            ),
            candidate_target,
        )
        DifyWorkspaces(self.settings.database_file).mark_ready(
            request["tenant_id"], self.settings.environment
        )
        save("operator:register")
        self.store.register(
            app_id=state["app_id"],
            tenant_id=request["tenant_id"],
            workspace_id=workspace_id,
            agent_name="operator-" + state["app_id"],
            profile_id=spec["profile_id"],
            actor=caller.user_id,
            entry="operator",
            environment=self.settings.execution_environment,
        )
        save("operator:publish")
        caller = auth.identity(self.context, session)
        auth.require_target(self.context, caller, request["tenant_id"])
        current = self.route(request["tenant_id"])
        if current is None:
            outcome = self.publish(caller, state["app_id"], 0, frozen_draft)
            state.update(version_no=outcome["version_no"], published=True)
        elif current.app_id == state["app_id"]:
            # 上次发布成功但完成回执丢失：只对账，不撤销后续人工撤回或切版。
            state.update(version_no=current.agent_version_no, published=current.active)
        else:
            state.update(published=False, draft_only=True)
        state.update(state="ready" if state.get("published") else "draft", error_code="")

    def _run_consumer(self, job_id, request, resources, state, save, caller, session):
        from aiops_diagnostics.consumer.consumer_publication import workflow_of
        from aiops_diagnostics.dify.dify_dsl_pull import DifyDslClient, workflow_content

        spec = request["sources"]["consumer"]
        workspace_id = resources["workspace_id"]
        save("consumer:application")
        if not state.get("knowledge_base_ids"):
            state["knowledge_base_ids"] = [
                prepare.customer_knowledge_base(self.context, request["tenant_id"])
            ]
            # 显式登记这一次新建的库"允许对客"。没有这一行，发布契约与检索都会把它当内部库。
            from aiops_diagnostics.consumer.consumer_knowledge_grants import KnowledgeGrants

            grants = KnowledgeGrants(self.settings.database_file)
            for knowledge_base_id in state["knowledge_base_ids"]:
                grants.grant(
                    request["tenant_id"],
                    self.settings.execution_environment,
                    knowledge_base_id,
                    caller.user_id,
                )
        source = DifyDslSource(
            self.settings.dify_console_base_url,
            spec["app_id"],
            self.settings.dify_console_api_key,
            spec["workspace_id"],
        )
        document = yaml.safe_load(DifyDslClient(source).export_dsl(workflow_id=spec["workflow_id"]))
        if not isinstance(document, dict) or "workflow" not in document:
            raise auth.SetupRefused("SETUP_SOURCE_NOT_CONSUMER")
        # 客户端的**实际依赖**在这里就位并回读：业务连接、知识插件与目录凭据、源编排用到的
        # 每个模型。只复制 DSL 而说"资源就绪"，等于把问题推到客户第一次提问的时候。
        save("consumer:dependencies")
        state["business_endpoint"] = prepare.consumer_endpoint(self.context, request["tenant_id"])
        from aiops_diagnostics.consumer.consumer_knowledge_selection import catalogue_token

        token = catalogue_token(
            self.settings.internal_token_secret,
            self.settings.environment,
            workspace_id,
        )
        state["plugin"] = prepare.consumer_plugin(self.console, workspace_id, self.settings, token)
        state["models"] = prepare.consumer_models(
            self.console, workspace_id, document, self._profile(request)
        )
        # 来源在本轮期间改过 ⇒ 冻结的那份与现在能导出的不是同一份，拒绝而不是"按现在的来"。
        current = workflow_of(workflow_content(yaml.safe_dump(document, allow_unicode=True)))
        if current != spec["graph_sha256"]:
            raise SetupConflict("SETUP_SOURCE_CHANGED")
        forbidden = ()
        if not _same_tenant(spec.get("workspace_id"), workspace_id):
            forbidden = prepare.consumer_source_ids(document)
        document = prepare.rebind_knowledge(document, state["knowledge_base_ids"], forbidden_ids=forbidden)
        document.setdefault("app", {})["name"] = request["name"]
        if not state.get("app_id"):
            state["app_id"] = prepare.copy_consumer_app(
                self.console, source, workspace_id, job_id, {"name": request["name"]}, document
            )
            save("consumer:application")
        save("consumer:key")
        state["key_ref"] = service_keys.ensure(
            self.console,
            self.settings.chat_dify_key_dir,
            workspace_id,
            state["app_id"],
            database=self.settings.database_file,
        )
        targets = {
            "knowledge_base_ids": list(state["knowledge_base_ids"]),
            **spec["flags"],
            "photo_enabled": False,
        }
        save("consumer:register")
        self.store.register(
            app_id=state["app_id"],
            tenant_id=request["tenant_id"],
            workspace_id=workspace_id,
            agent_name="consumer-" + state["app_id"],
            profile_id="",
            actor=caller.user_id,
            entry="consumer",
            environment=self.settings.execution_environment,
            targets=targets,
        )
        save("consumer:publish")
        caller = auth.identity(self.context, session)
        auth.require_target(self.context, caller, request["tenant_id"])
        current = self.route_for(request["tenant_id"], "consumer")
        if current is None:
            outcome = self.publish(caller, state["app_id"], 0)
            state.update(version_no=outcome["version_no"], published=True)
        elif current.app_id == state["app_id"]:
            # 指针已经是这个应用：**只对账，不推进**。`active=False` 是"客户主动撤回了"，
            # 不是"上次没发成功" —— 重试把它改回 active 等于替客户撤销了一个决定。
            state.update(
                version_no=current.agent_version_no,
                published=current.active,
                retracted=not current.active,
            )
        else:
            state.update(published=False, draft_only=True)
        channel = dual_entry.channel_state(self._channels(), request["tenant_id"], "consumer")
        state.update(
            state="ready" if state.get("published") else "draft",
            error_code="",
            channel=channel["state"],
            channel_note=channel["note"],
        )

    # ── 发布 ──────────────────────────────────────────────────────────

    def publish(self, caller, app_id, revision, expected_draft=None):
        with self.lock:
            row = self.authorize_app(caller, app_id)
            if row["entry"] == "consumer":
                return self._publish_consumer(caller, row, revision, expected_draft)
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

    def _publish_consumer(self, caller, row, revision, expected_draft=None):
        """客户端发布：冻结编排 → 推进正式指针 → 回读凭据与知识库。

        推进指针这一步**复用控制台发布那条路径**（`gateway_api._advance_route_binding`），
        而不是在这里再写一份：那条路径里的三条前置拒绝（凭据目录、聊天地址、文件登记冲突）
        每一条都对应一次"发布了但跑不起来"，复制一份就等于让两边慢慢长歪。
        """
        from aiops_diagnostics.consumer import consumer_publication
        from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
        from aiops_diagnostics.dify.dify_dsl_pull import sync_workflow_from_dify

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
        # 受管目标没有内容文件，所以它只支持**动态知识回答**（文案与判定在代码里）。
        # 这不是猜测：读应用自己的编排，缺模式就直接拒绝，而不是在发布时才报一句
        # 谁都看不懂的"内容文件缺失"。
        from aiops_diagnostics.consumer import consumer_documents
        from aiops_diagnostics.dify.dify_dsl_pull import DifyDslClient, workflow_content

        document = yaml.safe_load(DifyDslClient(source).export_dsl())
        workflow = workflow_content(yaml.safe_dump(document, allow_unicode=True))
        if not consumer_documents.enabled(workflow):
            raise auth.SetupRefused("SETUP_CONSUMER_CONTENT_REQUIRED")
        # 发布冻结的是**操作者确认过的那一份**。摘要由 `status` 给出（页面读到什么就发回
        # 什么），发布时再算一次：中间被另一个标签页改过就拒绝，而不是把别人的内容发出去。
        if expected_draft is not None and expected_draft != consumer_publication.workflow_of(workflow):
            raise SetupConflict("SETUP_DRAFT_CHANGED")
        # 凭据先就位：发布只推路径，而一条指向不存在文件的路径等于"发布了也跑不起来"。
        key_ref = service_keys.ensure(
            self.console,
            self.settings.chat_dify_key_dir,
            row["workspace_id"],
            row["app_id"],
            database=self.settings.database_file,
        )
        settings = consumer_publication.frozen_settings(target, documents=True)
        from aiops_diagnostics.consumer.consumer_knowledge_selection import owned_libraries

        bootstrap = consumer_publication.bootstrap_config(settings["knowledge_base_ids"], targets)
        outcome = sync_workflow_from_dify(
            self.context.agent_manager,
            admin_context(row["tenant_id"]),
            binding,
            source,
            consumer_settings=settings,
            consumer_callback_base=self.settings.public_base_url,
            consumer_libraries=lambda: owned_libraries(self.context.runtime, row["tenant_id"]),
            bootstrap=bootstrap,
        )
        if outcome.action == "refused":
            raise auth.SetupRefused("SETUP_AGENT_UNAVAILABLE")
        from aiops_diagnostics.gateway import gateway_api

        gateway_api._advance_route_binding(  # noqa: SLF001 - 复用同一条推进路径
            self.context,
            outcome,
            binding,
            initiator=(caller.user_id, "", ""),
            expected_revision=revision,
        )
        if outcome.workflow_id and key_ref:
            pass  # 路径已由 `_advance_route_binding` 写入正式指针
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
                    node_publication.advance(
                        self.context, outcome, binding, t, revision, register_source=True
                    )
                else:
                    from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
                    from aiops_diagnostics.gateway import gateway_api

                    gateway_api._advance_route_binding(  # noqa: SLF001
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


def _requested_entries(payload):
    """这次开通要做哪几端，以及各端的来源。

    `sources` 是新的形状（每端一个来源）。只有旧的单来源字段时按**管家**处理 ——
    那个字段本来也只能表达管家：它是「复制并开通客户管家」的入参。
    """
    sources = getattr(payload, "sources", None) or {}
    if sources:
        entries = dual_entry.entries_for_scope(payload.scope)
        missing = [entry for entry in entries if entry not in sources]
        if missing:
            raise auth.SetupRefused("SETUP_SOURCE_REQUIRED")
        return entries, sources
    if not payload.source_app_id or payload.source_version is None:
        raise auth.SetupRefused("SETUP_SOURCE_REQUIRED")
    entries = ("operator",)
    return entries, {"operator": _LegacySource(payload.source_app_id, payload.source_version)}


class _LegacySource:
    __slots__ = ("app_id", "version_no")

    def __init__(self, app_id, version_no):
        self.app_id = app_id
        self.version_no = version_no


def _identity(frozen):
    return {
        "tenant_id": frozen["tenant_id"],
        "name": frozen["name"],
        "sources": {
            entry: [spec["app_id"], spec["version_no"]]
            for entry, spec in (frozen.get("sources") or {}).items()
        },
    }


def _identity_of(payload):
    sources = getattr(payload, "sources", None) or {}
    if sources:
        return {
            "tenant_id": payload.tenant_id,
            "name": payload.name,
            "sources": {entry: [spec.app_id, spec.version_no] for entry, spec in sources.items()},
        }
    return {
        "tenant_id": payload.tenant_id,
        "name": payload.name,
        "sources": {"operator": [payload.source_app_id, payload.source_version]},
    }


def _same_tenant(source_workspace, target_workspace):
    return bool(source_workspace) and source_workspace == target_workspace


def _error_code(exc):
    if isinstance(exc, (auth.SetupRefused, SetupConflict, dual_entry.DualEntryError)):
        return str(exc)
    return "SETUP_STEP_FAILED"


def _mirror(resources, entries):
    """顶层三个字段镜像管家端（没有管家端时镜像第一个已选端），供既有页面读取。"""
    states = resources.get("entries") or {}
    primary = states.get("operator") or (states.get(entries[0]) if entries else None) or {}
    resources["app_id"] = primary.get("app_id")
    resources["version_no"] = primary.get("version_no")
    resources["published"] = bool(primary.get("published"))


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
