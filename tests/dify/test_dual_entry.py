"""双端开通：目录分端、逐端准备、逐端发布、失败隔离与凭据对账。

这些用例证明的是**开通流程本身**：一次提交里两端各自准备、各自发布，一端失败不牵连另一端，
重试不重复创建，客户端知识引用被整段换成目标客户自己的库。真实消费者/员工的业务正确性
由 41 上的真实验收证明，fixture 只证明契约。
"""

import json
import re
import uuid
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest
import yaml
from consumer.test_consumer_flow_contract import BASE
from consumer.test_consumer_knowledge_picker import picker_graph
from operations.test_operator_cpms import APP
from operations.test_operator_cpms import operator as operator  # noqa: F401

from aiops_diagnostics.consumer import consumer_publication
from aiops_diagnostics.dify import dual_entry, service_keys
from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify import self_service_prepare as prepare
from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.dify.self_service import SelfService
from aiops_diagnostics.dify.self_service_api import SetupRequest, SetupSource
from aiops_diagnostics.dify.self_service_store import public_job
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
            provider = path.split("/")[6]
            if path.endswith("/credentials"):
                if method == "GET":
                    return {"credentials": self.plugin_credentials.setdefault(provider, [])}
                raise AssertionError("credentials endpoint is GET only here")
            if path.endswith("/add"):
                self.plugin_credentials.setdefault(provider, []).append(
                    {"id": f"cred-{len(self.plugin_credentials[provider]) + 1}", **body}
                )
                return {"result": "success"}
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
    settings.consumer_knowledge_plugin = "trendpower/consumer_knowledge:0.0.1@fixture"
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


def request(dual, *, scope="both", key="request-000000001", name="客户B－客服与管家", sources=None):
    if sources is None:
        sources = {
            "consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version},
            "operator": {"app_id": dual.operator_source, "version_no": dual.operator_version},
        }
    return SetupRequest(
        request_key=key,
        tenant_id=TARGET,
        name=name,
        scope=scope,
        sources={entry: SetupSource(**spec) for entry, spec in sources.items()},
    )


def run(dual, payload):
    job = dual.service.create(dual.caller, payload)
    dual.service.run(job["id"], "session")
    return public_job(dual.service.store.get(job["id"]))


# ── A / B：双端一次开通、单端、补开 ──────────────────────────────────────


def test_both_entries_are_created_and_published_separately(dual):
    result = run(dual, request(dual))
    assert result["state"] == "completed", result
    assert set(result["entries"]) == {"consumer", "operator"}
    consumer, operator = result["entries"]["consumer"], result["entries"]["operator"]
    assert consumer["published"] and operator["published"]
    assert consumer["app_id"] != operator["app_id"]
    assert consumer["version_no"] and operator["version_no"]
    # 两端各有一个正式指针，互不覆盖
    routes = ChatRouteBindingStore(dual.settings.chat_route_bindings_file)
    assert routes.get("env-a", TARGET, "consumer", "consumer_service").app_id == consumer["app_id"]
    assert routes.get("env-a", TARGET, "operator", "operator_order_diagnosis").app_id == operator["app_id"]
    # 来源客户的正式指针不动
    assert routes.get("env-a", TENANT, "consumer", "consumer_service").app_id == CONSUMER_APP


def test_consumer_only_scope_does_not_open_the_operator_entry(dual):
    result = run(
        dual,
        request(
            dual,
            scope="consumer",
            sources={"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}},
        ),
    )
    assert result["state"] == "completed", result
    assert list(result["entries"]) == ["consumer"]
    assert result["entries"]["consumer"]["published"]
    routes = ChatRouteBindingStore(dual.settings.chat_route_bindings_file)
    assert routes.get("env-a", TARGET, "operator", "operator_order_diagnosis") is None


def test_missing_source_for_a_selected_entry_is_refused(dual):
    with pytest.raises(auth.SetupRefused, match="SETUP_SOURCE_REQUIRED"):
        dual.service.create(
            dual.caller,
            request(
                dual,
                sources={"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}},
            ),
        )


def test_a_source_of_the_other_entry_cannot_be_relabelled(dual):
    # 管家应用被填进"客户端来源"槽位：它在这个端没有 active 正式路由 ⇒ 拒绝。
    with pytest.raises(auth.SetupRefused, match="SETUP_SOURCE_NOT_PUBLISHED|SETUP_SOURCE_REQUIRED"):
        dual.service.create(
            dual.caller,
            request(
                dual,
                sources={"consumer": {"app_id": dual.operator_source, "version_no": dual.operator_version}},
            ),
        )


def test_adding_consumer_keeps_the_existing_operator_route(dual):
    routes = ChatRouteBindingStore(dual.settings.chat_route_bindings_file)
    before = routes.get("env-a", TENANT, "operator", "operator_order_diagnosis")
    result = run(
        dual,
        request(
            dual,
            scope="consumer",
            sources={"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}},
            key="request-000000002",
        ),
    )
    assert result["state"] == "completed", result
    assert routes.get("env-a", TENANT, "operator", "operator_order_diagnosis") == before


# ── C：重复与响应丢失 ───────────────────────────────────────────────────


def test_same_request_key_creates_nothing_twice(dual):
    payload = request(dual)
    job = dual.service.create(dual.caller, payload)
    same = dual.service.create(dual.caller, payload)
    assert same["id"] == job["id"]
    dual.service.run(job["id"], "session")
    created = len(dual.console.created)
    keys = dual.console.key_posts
    dual.service.run(job["id"], "session")
    assert len(dual.console.created) == created
    # 只有客户端会申请 app key；管家走节点路线，凭据是运维预置的
    assert dual.console.key_posts == keys == 1


def test_changed_sources_under_the_same_key_are_refused(dual):
    payload = request(dual)
    dual.service.create(dual.caller, payload)
    with pytest.raises(Exception, match="SETUP_REQUEST_CHANGED"):
        dual.service.create(dual, payload) if False else dual.service.create(
            dual.caller,
            request(
                dual,
                sources={"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}},
            ),
        )


def test_consumer_import_uses_the_same_deterministic_app_id(dual):
    job = dual.service.create(dual.caller, request(dual))
    dual.service.run(job["id"], "session")
    app_id = public_job(dual.service.store.get(job["id"]))["entries"]["consumer"]["app_id"]
    # 应用 id 由**补丁同一条派生**算出：uuid5(NS, tenant + ":" + request_id)，
    # 而 request_id 本身是 UUID 形状（补丁的 Pydantic 约束是 ^[a-f0-9-]{36}$）。
    assert app_id == prepare.consumer_app_id("workspace-tenant-b", job["id"])
    request_id = prepare.consumer_request_id(job["id"])
    assert re.fullmatch(r"[a-f0-9-]{36}", request_id)


def test_lost_key_response_is_reconciled_not_duplicated(dual):
    dual.console.lose_key_response = True
    job = dual.service.create(dual.caller, request(dual))
    dual.service.run(job["id"], "session")
    result = public_job(dual.service.store.get(job["id"]))
    # 响应丢失 ⇒ 对账到那一枚新 key 并继续；不会申请第二枚
    assert result["entries"]["consumer"]["published"] is True, result
    assert dual.console.key_posts == 1
    path = service_keys.key_path(dual.settings.chat_dify_key_dir, result["entries"]["consumer"]["app_id"])
    assert path.read_text().strip() == "app-key-1"


def test_ambiguous_key_reconciliation_refuses_to_publish(dual, monkeypatch):
    class Two(SimpleNamespace):
        pass

    console = dual.console
    real = console._call

    def call(workspace_id, path, *, method="GET", body=None, follow_redirects=True):
        if path.endswith("/api-keys") and method == "POST":
            console.key_posts += 1
            app_id = path.split("/")[-2]
            console.keys.setdefault(app_id, {}).update({f"foreign-{n}": f"foreign-id-{n}" for n in (1, 2)})
            raise TimeoutError("response lost")
        return real(workspace_id, path, method=method, body=body, follow_redirects=follow_redirects)

    console._call = call
    job = dual.service.create(dual.caller, request(dual))
    dual.service.run(job["id"], "session")
    result = public_job(dual.service.store.get(job["id"]))
    assert result["state"] == "partial", result
    assert result["entries"]["consumer"]["error_code"] == "SETUP_KEY_AMBIGUOUS"
    assert result["entries"]["consumer"]["published"] is False
    assert result["entries"]["operator"]["published"] is True


# ── D：一端失败不破坏另一端 ─────────────────────────────────────────────


def test_consumer_failure_leaves_operator_ready_and_retry_only_resumes_consumer(dual, monkeypatch):
    calls = {"consumer": 0}

    real = prepare.customer_knowledge_base

    def flaky(context, tenant_id):
        calls["consumer"] += 1
        if calls["consumer"] == 1:
            raise auth.SetupRefused("SETUP_KNOWLEDGE_FAILED")
        return real(context, tenant_id)

    monkeypatch.setattr(prepare, "customer_knowledge_base", flaky)
    job = dual.service.create(dual.caller, request(dual))
    dual.service.run(job["id"], "session")
    first = public_job(dual.service.store.get(job["id"]))
    assert first["state"] == "partial", first
    assert first["entries"]["consumer"]["error_code"] == "SETUP_KNOWLEDGE_FAILED"
    assert first["entries"]["operator"]["state"] == "ready"
    operator_app = first["entries"]["operator"]["app_id"]
    created = len(dual.console.created)
    dual.service.run(job["id"], "session")
    second = public_job(dual.service.store.get(job["id"]))
    assert second["state"] == "completed", second
    assert second["entries"]["operator"]["app_id"] == operator_app
    assert len(dual.console.created) == created + 1  # 只补建了客户端应用


def test_operator_dependency_failure_leaves_consumer_ready(dual, monkeypatch):
    """模型/节点 provider 是**端**的依赖，不是公共准备。

    只开客户端时去装管家的 `cpms-node` provider，会让客户端开通依赖一个它不需要的外部
    动作；反过来，管家那一步失败也不该把已经就绪的客户端一起判成失败。
    """

    def boom(*_args, **_kwargs):
        raise auth.SetupRefused("SETUP_MODEL_CONFLICT")

    monkeypatch.setattr(prepare, "dependencies", boom)
    job = dual.service.create(dual.caller, request(dual))
    dual.service.run(job["id"], "session")
    result = public_job(dual.service.store.get(job["id"]))
    assert result["state"] == "partial", result
    assert result["entries"]["operator"]["error_code"] == "SETUP_MODEL_CONFLICT"
    assert result["entries"]["consumer"]["published"] is True


# ── E：草稿预览、保存不发布、发布/撤回/恢复 ─────────────────────────────


def test_existing_operator_route_makes_the_new_copy_a_draft(dual):
    operator_only = {"operator": {"app_id": dual.operator_source, "version_no": dual.operator_version}}
    first = run(dual, request(dual, scope="operator", sources=operator_only, key="request-000000011"))
    assert first["entries"]["operator"]["published"] is True, first
    routes = ChatRouteBindingStore(dual.settings.chat_route_bindings_file)
    published = routes.get("env-a", TARGET, "operator", "operator_order_diagnosis")
    second = run(dual, request(dual, scope="operator", sources=operator_only, key="request-000000012"))
    assert second["state"] == "completed", second
    entry = second["entries"]["operator"]
    # 第二份只建草稿：原正式版继续服务，不因复制而被替换
    assert entry["published"] is False and entry["draft_only"] is True
    assert entry["app_id"] != published.app_id
    assert routes.get("env-a", TARGET, "operator", "operator_order_diagnosis") == published


def test_consumer_publish_freezes_the_draft_and_retract_does_not_fall_back(dual):
    result = run(
        dual,
        request(
            dual,
            scope="consumer",
            sources={"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}},
        ),
    )
    app_id = result["entries"]["consumer"]["app_id"]
    routes = ChatRouteBindingStore(dual.settings.chat_route_bindings_file)
    route = routes.get("env-a", TARGET, "consumer", "consumer_service")
    assert route.app_id == app_id and route.active
    published = dual.service.change_version(dual.caller, app_id, route.revision)
    assert published["state"] == "retracted"
    assert routes.get("env-a", TARGET, "consumer", "consumer_service").active is False
    restored = dual.service.change_version(dual.caller, app_id, published["revision"], route.agent_version_no)
    assert restored["state"] == "active"
    assert restored["version_no"] == route.agent_version_no


def test_publishing_one_entry_does_not_move_the_other(dual):
    run(dual, request(dual))
    routes = ChatRouteBindingStore(dual.settings.chat_route_bindings_file)
    consumer = routes.get("env-a", TARGET, "consumer", "consumer_service")
    operator = routes.get("env-a", TARGET, "operator", "operator_order_diagnosis")
    dual.service.publish(dual.caller, consumer.app_id, consumer.revision)
    assert routes.get("env-a", TARGET, "operator", "operator_order_diagnosis") == operator


# ── F：来源更新不覆盖客户副本 ───────────────────────────────────────────


def test_source_change_after_freezing_is_refused(dual):
    payload = request(
        dual,
        scope="consumer",
        sources={"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}},
    )
    job = dual.service.create(dual.caller, payload)
    # 来源在本轮期间又发布了一版（快照内容变了）

    class Changed(Dsl):
        def export_dsl(self, app_id=None, *, workflow_id=None):
            doc = source_document()
            doc["workflow"]["graph"]["nodes"][0]["data"]["variables"][0]["value"] = "changed"
            return yaml.safe_dump(doc)

    import aiops_diagnostics.dify.dify_dsl_pull as pull

    original = pull.DifyDslClient
    pull.DifyDslClient = Changed
    try:
        dual.service.run(job["id"], "session")
    finally:
        pull.DifyDslClient = original
    result = public_job(dual.service.store.get(job["id"]))
    assert result["state"] == "failed"
    assert result["entries"]["consumer"]["error_code"] == "SETUP_SOURCE_CHANGED"


# ── G：知识引用重绑 ─────────────────────────────────────────────────────


def test_consumer_copy_rebinds_knowledge_and_never_carries_source_ids(dual):
    result = run(
        dual,
        request(
            dual,
            scope="consumer",
            sources={"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}},
        ),
    )
    app_id = result["entries"]["consumer"]["app_id"]
    document = dual.console.apps[app_id]
    knowledge = next(n for n in document["workflow"]["graph"]["nodes"] if n["id"] == "knowledge")
    selected = knowledge["data"]["tool_configurations"]["knowledge_base_ids"]["value"]
    assert selected and "kb-source" not in selected
    # 名字不再固定、更不按名字复用：每次开通都新建一个库，避免把同名内部库当对客库。
    assert len(dual.kb.created) == 1 and dual.kb.created[0].startswith("对客知识库-")
    # 新建的库被显式登记为"允许对客"
    from aiops_diagnostics.consumer.consumer_knowledge_grants import KnowledgeGrants

    assert KnowledgeGrants(dual.settings.database_file).granted(TARGET, "env-a") == set(selected)


def test_rebind_refuses_source_ids_for_another_tenant():
    document = source_document()
    with pytest.raises(auth.SetupRefused, match="SETUP_KNOWLEDGE_CROSS_TENANT"):
        prepare.rebind_knowledge(document, ["kb-source"], forbidden_ids=["kb-source"])


def test_rebind_requires_at_least_one_library():
    with pytest.raises(auth.SetupRefused, match="SETUP_KNOWLEDGE_BINDING_REQUIRED"):
        prepare.rebind_knowledge(source_document(), [])


# ── J / K：渠道口径与目录分端 ───────────────────────────────────────────


def test_catalogue_lists_sources_per_entry(dual):
    dual.settings.dify_publish_api_key = "management-key"
    headers = {"Authorization": "Bearer management-key", "X-AIOps-Console-Session": "session"}
    body = dual.f["client"].get("/v1/dify/management/catalogue", headers=headers).json()
    entries = {item["entry"]: item for item in body["entries"]}
    assert set(entries) == {"consumer", "operator"}
    assert [s["app_id"] for s in entries["consumer"]["sources"]] == [CONSUMER_APP]
    assert APP in [s["app_id"] for s in entries["operator"]["sources"]]
    # 顶层 sources 仍是管家端，既有前端不改也能跑
    assert APP in [s["app_id"] for s in body["sources"]]


def test_channel_state_comes_from_declaration_not_from_publication(dual, tmp_path):
    result = run(
        dual,
        request(
            dual,
            scope="consumer",
            sources={"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}},
        ),
    )
    # 没有声明 ⇒ 渠道待接入，即使这一端已经发布
    assert result["entries"]["consumer"]["channel"] == "pending"
    manifest = tmp_path / "env.toml"
    manifest.write_text(
        f'[[channels]]\ntenant_id = "{TARGET}"\nentry = "consumer"\n'
        'channel = "ulink-h5"\nstate = "connected"\n'
    )
    dual.settings.environment_manifest_file = str(manifest)
    declared = dual_entry.channels(str(manifest), "env-a")
    assert dual_entry.channel_state(declared, TARGET, "consumer")["state"] == "connected"
    assert dual_entry.channel_state(declared, TARGET, "operator")["state"] == "pending"


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


def test_restoring_a_version_of_another_app_is_refused(dual):
    """正式指针只接受**本应用**的版本快照。

    构造方式：在这个客户自己的 agent 上冻结一份 `app_id` 属于来源应用的历史版本 ——
    这正是"版本号对得上、内容却是别人的"那种状态。恢复它等于把来源客户的编排接到
    目标客户的正式入口上，所以必须拒绝。
    """
    result = run(
        dual,
        request(
            dual,
            scope="consumer",
            sources={"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}},
        ),
    )
    app_id = result["entries"]["consumer"]["app_id"]
    routes = ChatRouteBindingStore(dual.settings.chat_route_bindings_file)
    route = routes.get("env-a", TARGET, "consumer", "consumer_service")
    agent = next(a for a in dual.context.agent_manager.store.list(TARGET) if a.name == "consumer-" + app_id)
    snapshot = dual.context.agent_manager.store.version(
        agent.agent_id, TARGET, agent.published_version
    ).snapshot
    foreign = {**snapshot, "app_id": CONSUMER_APP, "workspace_id": "workspace-tenant-a"}
    version_no = agent.published_version + 1
    with dual.service.store.connection() as db:
        db.execute(
            "INSERT INTO agent_versions"
            "(agent_id,tenant_id,version_no,snapshot_json,published_by,published_at)"
            " VALUES(?,?,?,?,?,?)",
            (
                agent.agent_id,
                TARGET,
                version_no,
                json.dumps(foreign),
                "fixture",
                "2026-10-30T00:00:00+00:00",
            ),
        )
    with pytest.raises(auth.SetupRefused, match="SETUP_VERSION_MISMATCH"):
        dual.service.change_version(dual.caller, app_id, route.revision, version_no)


def test_restoring_with_a_stale_revision_is_refused(dual):
    result = run(
        dual,
        request(
            dual,
            scope="consumer",
            sources={"consumer": {"app_id": dual.consumer_source, "version_no": dual.consumer_version}},
        ),
    )
    app_id = result["entries"]["consumer"]["app_id"]
    routes = ChatRouteBindingStore(dual.settings.chat_route_bindings_file)
    route = routes.get("env-a", TARGET, "consumer", "consumer_service")
    with pytest.raises(Exception, match="SETUP_ROUTE_CHANGED"):
        dual.service.change_version(dual.caller, app_id, route.revision - 1)
