"""双端开通：目录分端、逐端准备、逐端发布、失败隔离与凭据对账。

这些用例证明的是**开通流程本身**：一次提交里两端各自准备、各自发布，一端失败不牵连另一端，
重试不重复创建，客户端知识引用被整段换成目标客户自己的库。真实消费者/员工的业务正确性
由 41 上的真实验收证明，fixture 只证明契约。
"""

import json
import uuid
from types import SimpleNamespace
from unittest import mock
from uuid import UUID, uuid5

import pytest
import yaml
from consumer.test_consumer_flow_contract import BASE
from consumer.test_consumer_knowledge_picker import picker_graph
from operations.test_operator_cpms import APP
from operations.test_operator_cpms import operator as operator  # noqa: F401

from aiops_diagnostics.consumer import consumer_publication
from aiops_diagnostics.dify import dual_entry
from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify import self_service_prepare as prepare
from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
from aiops_diagnostics.dify.dify_run_store import DifyRunStore
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.dify.self_service import SelfService
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.publication.agent_lifecycle import AgentConfig
from aiops_diagnostics.publication.agent_manifest import admin_context

CONSUMER_APP = "cccccccc-1111-2222-3333-444444444444"
SOURCE_WORKFLOW = "wf-source"
NS = UUID("6e5a60f0-9495-4c43-a714-d954c2609aeb")
TENANT = "TENANT-A"
TARGET = "TENANT-B"


def _upgrade():
    """`tools/content/upgrade_consumer_flow.py`：把选库编排升到动态知识回答。

    用生产的那一份，而不是在测试里手写一份"看起来合规"的编排：手写的那份只能证明
    测试自己的假设，证明不了发布门接受的形状。
    """
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "upgrade", Path(__file__).parents[2] / "tools/content/upgrade_consumer_flow.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_document(ids=("kb-source",)):
    # 模板里的 `__MODEL__` 是部署时替换的占位符；这里换成白名单里的模型名，
    # 与真实部署里"模型由 profile 决定"一致。
    flow = picker_graph(list(ids))
    next(n for n in flow["graph"]["nodes"] if n["id"] == "knowledge")["data"]["plugin_unique_identifier"] = (
        "trendpower/consumer_knowledge:0.0.1@" + "a" * 64
    )
    flow = json.loads(json.dumps(flow).replace("__MODEL__", "aiops-api"))
    # 受管客户端目标没有内容文件 ⇒ 只支持动态知识回答（文案与判定在代码里）。
    flow = _upgrade().live_documents({"workflow": flow})["workflow"]
    return {"app": {"mode": "advanced-chat", "name": "现役客服"}, "workflow": flow}


class Console:
    """够用的 Dify 控制台替身：导入幂等、app 文档回读、app key 列表/创建。"""

    def __init__(self):
        self.apps: dict[str, dict] = {}
        self.keys: dict[str, dict[str, str]] = {}
        self.created: list[str] = []
        self.key_posts = 0
        self.lose_key_response = False
        self.plugin = ""
        self.plugin_credentials: dict[str, list[dict]] = {}

    def install_plugins(self, workspace_id, identifiers):
        """真实实现会等安装任务落地；替身只需记录标识并让 credentials 端点可用。"""
        self.plugin = identifiers[0]
        return list(identifiers)

    def _app(self, app_id):
        return self.apps[app_id]

    def _call(self, workspace_id, path, *, method="GET", body=None, follow_redirects=True):
        if path == "/console/api/apps/imports":
            if method == "GET":
                return {"aiops_idempotent_import": 1}
            request_id = body["request_id"]
            app_id = str(uuid5(NS, workspace_id + ":" + request_id))
            self.apps[app_id] = yaml.safe_load(body["yaml_content"])
            self.created.append(app_id)
            return {"status": "completed", "app_id": app_id}
        if path.endswith("/api-keys"):
            app_id = path.split("/")[-2]
            if method == "GET":
                return {
                    "data": [
                        {"id": key_id, "token": token, "type": "app"}
                        for token, key_id in self.keys.setdefault(app_id, {}).items()
                    ]
                }
            self.key_posts += 1
            token, key_id = f"app-key-{self.key_posts}", f"key-{self.key_posts}"
            self.keys.setdefault(app_id, {})[token] = key_id
            if self.lose_key_response:
                raise TimeoutError("response lost after creation")
            return {"id": key_id, "token": token, "type": "app"}
        if path == "/console/api/workspaces/current/plugin/list?page=1&page_size=100":
            return {"plugins": [{"plugin_unique_identifier": self.plugin}]}
        if path == "/console/api/workspaces/current/plugin/install/pkg":
            self.plugin = (body or {}).get("plugin_unique_identifiers", [self.plugin])[0]
            return {"task_id": "", "all_installed": True}
        if path.startswith("/console/api/workspaces/current/tool-provider/builtin/"):
            from dify.test_consumer_dependencies import Console as CredentialConsole

            credentials = CredentialConsole(credentials=self.plugin_credentials)
            result = credentials._call(workspace_id, path, method=method, body=body)
            self.plugin_credentials = credentials.credentials
            return result
        if path.startswith("/console/api/workspaces/current/model-providers/"):
            provider = path.split("/")[6]
            return {"current_credential_id": f"mc-{provider}", "credentials": {}, "load_balancing": {}}
        if path.startswith("/console/api/apps/"):
            app_id = path.rsplit("/", 1)[-1]
            return self._app(app_id)
        raise AssertionError(f"unexpected console call {method} {path}")


class Dsl:
    """按 app_id 分发 DSL：已导入过的应用回读导入内容，否则回读来源应用。

    真实 Dify 的 export 读的就是**那个 app 现在的内容**，所以替身也必须这样做：
    只按"来源是谁"分支会让"复制出来的副本"读到来源的内容，从而掩盖重绑有没有生效。
    """

    console: "Console | None" = None

    def __init__(self, source):
        self.source = source

    def export_dsl(self, app_id=None, *, workflow_id=None):
        target = app_id or self.source.app_id
        if Dsl.console is not None and target in Dsl.console.apps:
            return yaml.safe_dump(Dsl.console.apps[target])
        if self.source.app_id == CONSUMER_APP or target == CONSUMER_APP:
            return yaml.safe_dump(source_document())
        return yaml.safe_dump(
            {
                "app": {"mode": "agent-chat"},
                "model_config": {
                    "pre_prompt": "SOP_ONE",
                    "model": {"name": "aiops-api", "provider": "model/provider", "mode": "chat"},
                    "agent_mode": {
                        "tools": [
                            {
                                "tool_name": name,
                                "provider_id": "provider-id",
                                "provider_type": "api",
                                "enabled": True,
                            }
                            for name in ("cpms_order_detail", "cpms_charging_samples", "cpms_site_search")
                        ]
                    },
                },
            }
        )

    def publish_workflow(self, app_id=None):
        return True

    def published_workflow_id(self, app_id=None):
        return "wf-published"

    def tool_provider_ids(self):
        return {"cpms-node": "provider-id"}


class Kb:
    """kb-service 替身：目标客户空间里先只有一个内部库，对客库由开通时创建。"""

    def __init__(self):
        self.created: list[str] = []
        self.rows: dict[str, list[dict]] = {}

    def for_tenant(self, tenant):
        return self

    def list_knowledge_bases(self):
        return list(self.rows.setdefault(TENANT, [])) or list(self.rows.setdefault("current", []))

    def create_knowledge_base(self, name, description=""):
        knowledge_base_id = "kb-" + uuid.uuid5(NS, name).hex[:12]
        self.created.append(name)
        self.rows.setdefault("current", []).append(
            {"knowledge_base_id": knowledge_base_id, "name": name, "document_count": 0, "chunk_count": 0}
        )
        return {"id": knowledge_base_id}


@pytest.fixture
def dual(operator, monkeypatch, tmp_path):  # noqa: F811
    f = operator
    settings = f["settings"]
    context = f["ctx"]
    settings.environment = "verify"
    settings.execution_environment = "env-a"
    settings.public_base_url = BASE
    settings.dify_chat_base_url = "https://dify.example.test"
    settings.chat_dify_key_dir = str(tmp_path / "keys")
    (tmp_path / "keys").mkdir(mode=0o700)
    settings.environment_manifest_file = ""
    settings.internal_token_secret = "fixture-internal-secret"
    # 已批准的环境连接（裸 URL 形式：本环境所有租户走同一个 CPMS）。
    settings.charging_status_base_url = "https://cpms.example.test"

    secret = tmp_path / "model.json"
    secret.write_text(
        json.dumps(
            {
                "api_key": "fixture",
                "endpoint_url": "https://model.example/v1",
                "max_tokens_to_sample": "4096",
                "function_calling_type": "tool_call",
                "stream_function_calling": "supported",
            }
        )
    )
    secret.chmod(0o600)
    from dataclasses import asdict

    profile = {
        "id": "default",
        "name": "标准管家",
        "target": {k: v for k, v in asdict(f["target"]).items() if k not in {"app_id", "tenant_id"}},
        "model": {
            "plugin": "model@fixture",
            "provider": "model/provider",
            "name": "aiops-api",
            "credential_file": str(secret),
            "completion_params": {"max_tokens": 4096},
        },
    }
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({"schema": "operator-profiles/1", "profiles": [profile]}))
    profiles.chmod(0o600)
    settings.operator_profiles_file = str(profiles)

    workspaces = DifyWorkspaces(settings.database_file)
    for tenant in (TENANT, TARGET):
        workspaces.claim(tenant, settings.environment, owner="fixture", lease_seconds=60)
        workspaces.mark_created(tenant, settings.environment, "workspace-" + tenant.lower())

    service = SelfService(context)
    monkeypatch.setattr(auth, "identity", lambda *_: auth.ManagerIdentity("admin", "platform", True, "x"))
    monkeypatch.setattr(
        auth,
        "tenants",
        lambda *_: [{"id": TENANT, "name": "客户A"}, {"id": TARGET, "name": "客户B"}],
    )
    monkeypatch.setattr(prepare, "knowledge", lambda *_: None)
    monkeypatch.setattr(prepare, "knowledge_connection", lambda *_: "workspace-api")
    monkeypatch.setattr(prepare, "dependencies", lambda *_: "provider-id")

    def workspace(_settings, tenant, *_):
        return "workspace-" + tenant.lower()

    monkeypatch.setattr(prepare, "workspace", workspace)
    monkeypatch.setattr("aiops_diagnostics.dify.dify_dsl_pull.DifyDslClient", Dsl)
    # 管家发布器自己也要导一次 DSL 取模型名：同一个分发器给它同一份文档。
    monkeypatch.setattr("aiops_diagnostics.execution.node_publication.DifyDslClient", Dsl)
    service.console = Console()
    Dsl.console = service.console

    kb = Kb()
    context.runtime.kb_search_client = kb

    # ── 客户端来源：一个已发布的现役客服应用（本租户 TENANT-A）──
    manager = context.agent_manager
    admin = admin_context(TENANT)
    agent = manager.create(
        admin,
        name="consumer-source",
        description="现役客服",
        config=AgentConfig(
            agent_type="customer",
            prompt="现役客服提示词",
            knowledge_base_ids=("kb-source",),
            model="aiops-api",
            output_contract="blocks-v1",
        ),
    )
    flow = consumer_publication.workflow_of(source_document()["workflow"])
    snapshot = {
        "name": agent.name,
        "description": "",
        **agent.config.to_dict(),
        "kind": "dify-workflow",
        "app_id": CONSUMER_APP,
        "workspace_id": "workspace-tenant-a",
        "workflow_id": SOURCE_WORKFLOW,
        "workflow": source_document()["workflow"],
        "consumer_service": {
            "live_order_status": True,
            "live_billing_record": True,
            "knowledge_base_ids": ["kb-source"],
        },
    }
    version = manager.store.publish(agent.agent_id, TENANT, agent.revision, snapshot, published_by="fixture")
    ChatRouteBindingStore(settings.chat_route_bindings_file).publish(
        environment="env-a",
        tenant_id=TENANT,
        entry="consumer",
        scenario="consumer_service",
        engine="dify",
        agent_id=agent.agent_id,
        agent_version_no=version.version_no,
        app_id=CONSUMER_APP,
        workflow_id=SOURCE_WORKFLOW,
    )
    service.store.register(
        app_id=CONSUMER_APP,
        tenant_id=TENANT,
        workspace_id="workspace-tenant-a",
        agent_name="consumer-source",
        profile_id="",
        actor="fixture",
        entry="consumer",
        environment="env-a",
        targets={
            "knowledge_base_ids": ["kb-source"],
            "live_order_status": True,
            "live_billing_record": True,
        },
    )
    from execution.test_node_capabilities import record_ready

    record_ready(settings.database_file, f["target"].config_file)

    return SimpleNamespace(
        f=f,
        service=service,
        settings=settings,
        context=context,
        console=service.console,
        kb=kb,
        caller=auth.ManagerIdentity("admin", "platform", True, "x"),
        operator_source=APP,
        operator_version=f["route"].agent_version_no,
        consumer_source=CONSUMER_APP,
        consumer_version=version.version_no,
        source_flow_sha=flow,
    )


# ── A / B：双端一次开通、单端、补开 ──────────────────────────────────────


# ── C：重复与响应丢失 ───────────────────────────────────────────────────


# ── D：一端失败不破坏另一端 ─────────────────────────────────────────────


# ── E：草稿预览、保存不发布、发布/撤回/恢复 ─────────────────────────────


# ── F：来源更新不覆盖客户副本 ───────────────────────────────────────────


# ── G：知识引用重绑 ─────────────────────────────────────────────────────


def test_rebind_refuses_source_ids_for_another_tenant():
    document = source_document()
    with pytest.raises(auth.SetupRefused, match="SETUP_KNOWLEDGE_CROSS_TENANT"):
        prepare.rebind_knowledge(document, ["kb-source"], forbidden_ids=["kb-source"])


def test_rebind_requires_at_least_one_library():
    with pytest.raises(auth.SetupRefused, match="SETUP_KNOWLEDGE_BINDING_REQUIRED"):
        prepare.rebind_knowledge(source_document(), [])


# ── J / K：渠道口径与目录分端 ───────────────────────────────────────────


def test_channel_state_comes_from_declaration_not_from_publication(dual, tmp_path):
    # 没有声明 ⇒ 渠道待接入
    assert dual_entry.channel_state({}, TARGET, "consumer")["state"] == "pending"
    manifest = tmp_path / "env.toml"
    manifest.write_text(
        f'[[channels]]\ntenant_id = "{TARGET}"\nentry = "consumer"\n'
        'channel = "ulink-h5"\nstate = "connected"\n'
    )
    dual.settings.environment_manifest_file = str(manifest)
    declared = dual_entry.channels(str(manifest), "env-a")
    assert dual_entry.channel_state(declared, TARGET, "consumer")["state"] == "connected"
    assert dual_entry.channel_state(declared, TARGET, "operator")["state"] == "pending"


def test_channel_state_declared_wins_over_observed():
    declared = {("t", "consumer"): {"channel": "h5", "state": "pending", "note": "人工"}}
    # 声明行是显式裁决：人工压回 pending 不被流量证据推翻。
    assert dual_entry.channel_state(declared, "t", "consumer", observed=True)["state"] == "pending"
    # 无声明：观测到流量 ⇒ connected；没流量 ⇒ pending（fail closed）。
    assert dual_entry.channel_state({}, "t", "consumer", observed=True)["state"] == "connected"
    assert dual_entry.channel_state({}, "t", "consumer")["state"] == "pending"


def test_channel_state_derives_connected_from_observed_traffic(dual, tmp_path):
    """无声明时从真实成功流量推导客户端渠道；声明行仍赢。"""
    store = DifyRunStore(dual.settings.database_file)
    assert dual.service._channel_state(TARGET, "consumer")["state"] == "pending"
    store.issue(
        run_id="run-obs-1",
        qa_id="qa-1",
        scope_fingerprint="fp",
        scope_json="{}",
        binding_key=f"consumer_service:env-a:{TARGET}",
        order_no="",
        environment="env-a",
        tenant_id=TARGET,
        app_id="app-x",
        workflow_id="wf-1",
        grant_id="g-1",
    )
    # running 不是证据——渠道已接入要的是一条真正跑完的。
    assert dual.service._channel_state(TARGET, "consumer")["state"] == "pending"
    store.bind_workflow("run-obs-1", workflow_run_id="wr-1", task_id="t-1")
    store.succeed("run-obs-1")
    assert dual.service._channel_state(TARGET, "consumer")["state"] == "connected"
    # 声明行仍是显式裁决：人工压回 pending 不被这条流量推翻。
    manifest = tmp_path / "env.toml"
    manifest.write_text(
        f'[[channels]]\ntenant_id = "{TARGET}"\nentry = "consumer"\n'
        'channel = "ulink-h5"\nstate = "pending"\nnote = "人工压回"\n'
    )
    dual.settings.environment_manifest_file = str(manifest)
    state = dual.service._channel_state(TARGET, "consumer")
    assert state["state"] == "pending" and state["note"] == "人工压回"


def test_conflicting_channel_declarations_are_refused(tmp_path):
    manifest = tmp_path / "env.toml"
    manifest.write_text(
        f'[[channels]]\ntenant_id = "{TARGET}"\nentry = "consumer"\nchannel = "a"\nstate = "connected"\n'
        f'[[channels]]\ntenant_id = "{TARGET}"\nentry = "consumer"\nchannel = "b"\nstate = "pending"\n'
    )
    with pytest.raises(dual_entry.DualEntryError, match="CHANNEL_DECLARATION_INVALID"):
        dual_entry.channels(str(manifest), "env-a")


# ── 受管客户端运行目标 ──────────────────────────────────────────────────


def test_managed_registration_is_the_first_authority_for_consumer_targets(dual):
    binding = DifyAppBinding(TARGET, "consumer", "app-x", "consumer-app-x")
    dual.service.store.register(
        app_id="app-x",
        tenant_id=TARGET,
        workspace_id="workspace-tenant-b",
        agent_name="consumer-app-x",
        profile_id="",
        actor="fixture",
        entry="consumer",
        environment="env-a",
        targets={"knowledge_base_ids": ["kb-b"], "live_order_status": True},
    )
    target = consumer_publication.target_for(dual.settings, binding)
    assert target["source"] == "managed"
    assert target["app_id"] == "app-x" and target["knowledge_base_ids"] == ["kb-b"]


def test_same_app_declared_by_file_and_managed_row_must_agree(dual, tmp_path):
    target_file = tmp_path / "consumer-targets.json"
    target_file.write_text(
        json.dumps(
            {
                "schema": "aiops-consumer-targets/1",
                "targets": [
                    {
                        "tenant_id": TARGET,
                        "environment": "env-a",
                        "app_id": "ffffffff-1111-2222-3333-444444444444",
                        "knowledge_base_ids": ["kb-b"],
                        "live_order_status": True,
                        "live_billing_record": True,
                    }
                ],
            }
        )
    )
    target_file.chmod(0o600)
    dual.settings.consumer_publication_targets_file = str(target_file)
    app = "ffffffff-1111-2222-3333-444444444444"
    dual.service.store.register(
        app_id=app,
        tenant_id=TARGET,
        workspace_id="workspace-tenant-b",
        agent_name="consumer-" + app,
        profile_id="",
        actor="fixture",
        entry="consumer",
        environment="env-a",
        targets={"knowledge_base_ids": ["kb-b"]},
    )
    binding = DifyAppBinding(TARGET, "consumer", app, "consumer-" + app)
    # 同一个应用两条权威各说一套（文件说 kb-b、受管行说 kb-c）⇒ 拒绝
    dual.service.store.set_targets(app, {"knowledge_base_ids": ["kb-c"], "live_order_status": True})
    with pytest.raises(ValueError, match="conflicts with managed registration"):
        consumer_publication.target_for(dual.settings, binding)


def test_different_apps_of_one_tenant_do_not_conflict(dual, tmp_path):
    """一个客户可以同时有"现役正式版（文件登记）+ 新草稿（受管行）"。

    把"租户里有别的应用"当成冲突，会让补开/换副本这件事永远做不了 —— 独立验收 R5 的
    反例就是这个：第二份草稿一发布就 `SETUP_APP_NOT_MANAGED`。
    """
    target_file = tmp_path / "consumer-targets.json"
    target_file.write_text(
        json.dumps(
            {
                "schema": "aiops-consumer-targets/1",
                "targets": [
                    {
                        "tenant_id": TARGET,
                        "environment": "env-a",
                        "app_id": "ffffffff-1111-2222-3333-444444444444",
                        "knowledge_base_ids": ["kb-b"],
                        "live_order_status": True,
                        "live_billing_record": True,
                    }
                ],
            }
        )
    )
    target_file.chmod(0o600)
    dual.settings.consumer_publication_targets_file = str(target_file)
    draft_app = "eeeeeeee-1111-2222-3333-444444444444"
    dual.service.store.register(
        app_id=draft_app,
        tenant_id=TARGET,
        workspace_id="workspace-tenant-b",
        agent_name="consumer-" + draft_app,
        profile_id="",
        actor="fixture",
        entry="consumer",
        environment="env-a",
        targets={"knowledge_base_ids": ["kb-b"], "live_order_status": True},
    )
    binding = DifyAppBinding(TARGET, "consumer", draft_app, "consumer-" + draft_app)
    target = consumer_publication.target_for(dual.settings, binding)
    assert target["app_id"] == draft_app and target["source"] == "managed"


# ── L：版本不匹配阻止启用 ───────────────────────────────────────────────


# ── 2026-10-31 真实页面开通暴露的两个缺陷 ───────────────────────────────


def test_consumer_status_does_not_need_an_operator_profile(dual):
    """客户端登记行没有 profile；读它的状态不能因此崩。

    真实事故：启动纳管把客户端应用写成了 operator 端 + `profile_id=''`，
    页面读客户端状态直接 `KeyError: ''`（500），客户看不到自己客服端的任何状态。
    """
    service = dual.service
    service.store.register(
        app_id="eeeeeeee-1111-2222-3333-444444444444",
        tenant_id=TENANT,
        workspace_id="workspace-tenant-a",
        agent_name="orphan-consumer",
        profile_id="",
        actor="fixture",
        entry="consumer",
        environment="env-a",
    )
    row = service.status(dual.caller, "eeeeeeee-1111-2222-3333-444444444444")
    assert row["entry"] == "consumer" and row["managed"] is True


def test_client_row_mislabelled_as_operator_is_repaired_by_adoption(dual):
    """纳管必须把"端写错"的老行改回来，而不是原样写回去。

    真实事故：`operator_apps` 里客户端应用的端是 operator、场景是管家的、profile 为空；
    `store.register` 遇到同一条登记只更新元数据，于是错值在每次启动时都被"确认"一次，
    双端开通因此报 `SETUP_SOURCE_NOT_PUBLISHED`，页面完全无法开通客户端。

    端的裁定权在注册表（`dify_app_registry.business_entry`），所以修复是一次显式的
    `repair_entry` —— 不是让任意 `register` 调用改端（那等于允许改标签当另一端）。
    """
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore

    store = SelfServiceStore(dual.settings.database_file)
    store.repair_entry(CONSUMER_APP, tenant_id=TENANT, entry="operator")  # 造出"端写错"的旧行
    store.register(
        app_id=CONSUMER_APP,
        tenant_id=TENANT,
        workspace_id="workspace-tenant-a",
        agent_name="consumer-source",
        profile_id="operator-41",
        actor="managed-adoption",
        entry="operator",
        scenario="operator_order_diagnosis",
        environment="env-a",
    )
    assert store.repair_entry(CONSUMER_APP, tenant_id=TENANT, entry="consumer") is True
    row = store.app(CONSUMER_APP)
    assert (row["entry"], row["scenario"], row["profile_id"]) == ("consumer", "consumer_service", "")
    # 已经是正确端时是 no-op，不写库。
    assert store.repair_entry(CONSUMER_APP, tenant_id=TENANT, entry="consumer") is False
    # 别的客户不能借这个入口改别人的登记。
    assert store.repair_entry(CONSUMER_APP, tenant_id=TARGET, entry="consumer") is False


def test_register_refuses_to_change_tenant_or_entry_of_an_existing_app(dual):
    """修复端不等于允许把一行改挂到别的客户或另一个端上。"""
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore, SetupConflict

    store = SelfServiceStore(dual.settings.database_file)
    with pytest.raises(SetupConflict, match="SETUP_APP_CONFLICT"):
        store.register(
            app_id=CONSUMER_APP,
            tenant_id=TARGET,  # 同一个应用不能改挂到别的客户
            workspace_id="workspace-tenant-b",
            agent_name="consumer-source",
            profile_id="",
            actor="fixture",
            entry="consumer",
            environment="env-a",
        )


def test_outdated_provider_schema_is_reported_as_its_own_reason():
    """provider schema 比声明旧 ⇒ 页面看到 `SETUP_PROVIDER_OUTDATED`，不是 STEP_FAILED。

    真实事故：`operator_tools.json` 加了 `cpms_site_search`，两个客户工作区的
    `cpms-node` 还是两个工具的旧 schema；`ensure_node_provider(migrate=False)` 抛
    ValueError，被归成 `SETUP_STEP_FAILED`，页面只说"开通失败"。

    这里不借 `dual` 夹具：它把 `dependencies` 整个换成了替身（别的用例只要 provider id），
    在替身上验证错误映射等于什么都没验证。
    """
    import sys
    import tempfile
    from pathlib import Path

    prepare_module = sys.modules["aiops_diagnostics.dify.self_service_prepare"]
    manifest = Path(tempfile.mkdtemp()) / "exec-manifest.json"
    manifest.write_text(json.dumps({"tools": []}))
    manifest.chmod(0o600)

    with (
        mock.patch.object(prepare_module, "model_credentials", lambda *a: None),
        mock.patch.object(prepare_module, "ensure_node_provider", side_effect=ValueError("schema differs")),
        pytest.raises(auth.SetupRefused, match="SETUP_PROVIDER_OUTDATED"),
    ):
        prepare_module.dependencies(
            mock.Mock(), "workspace", {"model": {}, "target": {"manifest_file": str(manifest)}}, "https://gw"
        )
