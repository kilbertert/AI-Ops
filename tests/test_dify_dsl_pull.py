from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentNotFound, AgentStore
from aiops_diagnostics.dify_dsl_pull import (
    DifyDslAuthRejected,
    DifyDslClient,
    DifyDslMalformed,
    DifyDslSource,
    DifyDslUnreachable,
    DifyDslVersionUnsupported,
    converge_agent_draft,
    map_dsl_to_config,
    pull_agent_draft,
)
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

FIXTURES = Path(__file__).parent / "fixtures"
#: A real export pulled from the Dify instance on host 36 (#579) through the
#: console export route — not a hand-written sample. It carries a non-empty
#: pre_prompt, a model name, and (deliberately) an opening statement and
#: suggested questions, so "these two are not consumed" is provable.
REAL_DSL = (FIXTURES / "dify-app-chat.dsl.yml").read_text(encoding="utf-8")
#: The same app from before its prompt was filled in: the fixture for "the DSL
#: does not carry what we need".
NULL_PROMPT_DSL = (FIXTURES / "dify-app-chat-null-prompt.dsl.yml").read_text(encoding="utf-8")


class _Knowledge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        self.calls.append((tenant_id, knowledge_base_ids))


def _context(tenant: str = "tenant-a") -> ScopeContext:
    subject = SubjectRecord(b_user_id="B-1", tenant_id=tenant)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=tenant,
        data_scope=DataScope(type="self"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    )


def _manager(tmp_path: Path) -> AgentManager:
    return AgentManager(
        AgentStore(tmp_path / "gateway.db"),
        knowledge_resolver=_Knowledge(),
        allowed_models=("aiops-api", "deepseek-v4-flash"),
    )


def _source(**overrides) -> DifyDslSource:
    values = {
        "base_url": "http://127.0.0.1:10008",
        "app_id": "a975c8e5-ad9e-425c-84c9-77581a0a2bed",
        "api_key": "dsl-pull-controlled-key",
        "workspace_id": "ws-1",
    }
    values.update(overrides)
    return DifyDslSource(**values)


def _fake_response(payload: object, *, body: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    raw = body if body is not None else json.dumps(payload)
    resp.read.return_value = raw.encode("utf-8")
    return resp


def _serve(monkeypatch, *, payload=None, body: str | None = None, error: Exception | None = None):
    """Stand in for Dify's export endpoint and record the request we sent."""
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = {key.lower(): value for key, value in request.header_items()}
        seen["timeout"] = timeout
        if error is not None:
            raise error
        return _fake_response(payload, body=body)

    monkeypatch.setattr("aiops_diagnostics.bounded_http.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("aiops_diagnostics.bounded_http.time.sleep", lambda seconds: None)
    monkeypatch.setattr("aiops_diagnostics.bounded_http.random.uniform", lambda a, b: 0.0)
    return seen


def _export_body(dsl_text: str = REAL_DSL) -> str:
    return json.dumps({"data": dsl_text})


# --- mapping: the real export -------------------------------------------------


def test_real_export_maps_prompt_and_model_and_leaves_lifecycle_copy_alone() -> None:
    """The reason to pull is the prompt and the model; the DSL's copy is not ours to take."""
    config = map_dsl_to_config(REAL_DSL, agent_type="customer")

    assert config.model == "deepseek-v4-flash"
    assert "小趋" in config.prompt and "\n" in config.prompt
    assert len(config.prompt) > 60
    assert config.agent_type == "customer"
    assert config.output_contract == "blocks-v1"
    # dataset_configs carries only retrieval_model in this export: no dataset is bound.
    assert config.knowledge_base_ids == ()
    # The DSL does carry these two, non-empty — and they must not land here. Their
    # authoritative, per-language source is our own content artifact (#585).
    assert "欢迎使用充电服务助手" in REAL_DSL
    assert "充电桩无法启动怎么办？" in REAL_DSL
    assert config.opening_questions == ()
    assert config.quick_commands == ()


def test_a_superset_dsl_is_mapped_by_the_fields_we_read_and_the_rest_ignored() -> None:
    """Every real export is a superset: it carries editor fields we have no counterpart for.

    Being a superset is not an error — the DSL's shape is Dify's, not ours. What
    must hold is that the extra fields change nothing: no field lands on the
    config unless it is one we read.
    """
    smaller = map_dsl_to_config(REAL_DSL, agent_type="customer")
    superset = map_dsl_to_config(
        REAL_DSL.replace(
            "  user_input_form: []\n",
            "  user_input_form: []\n  some_future_dify_field:\n    enabled: true\n    value: 忽略我\n",
        ),
        agent_type="customer",
    )
    assert superset == smaller


def test_operations_agent_gets_the_operations_output_contract() -> None:
    assert map_dsl_to_config(REAL_DSL, agent_type="operations").output_contract == "diagnosis-v1"


def test_explicit_output_contract_wins_over_the_default() -> None:
    config = map_dsl_to_config(REAL_DSL, agent_type="operations", output_contract="blocks-v1")
    assert config.output_contract == "blocks-v1"


# --- mapping: absent and invalid values, each one loud ------------------------


def test_missing_prompt_is_an_error_not_an_empty_draft() -> None:
    with pytest.raises(DifyDslMalformed, match="pre_prompt"):
        map_dsl_to_config(NULL_PROMPT_DSL, agent_type="customer")


def test_unsupported_dsl_version_fails_loudly_instead_of_guessing() -> None:
    with pytest.raises(DifyDslVersionUnsupported):
        map_dsl_to_config(REAL_DSL.replace("version: 0.7.0", "version: 0.9.0"), agent_type="customer")
    with pytest.raises(DifyDslMalformed):
        map_dsl_to_config(REAL_DSL.replace("version: 0.7.0", "version: seven"), agent_type="customer")
    with pytest.raises(DifyDslMalformed):
        map_dsl_to_config("kind: app\nmodel_config: {}\n", agent_type="customer")


def test_unparseable_or_unknown_shaped_dsl_is_rejected() -> None:
    with pytest.raises(DifyDslMalformed):
        map_dsl_to_config("model_config: [unclosed", agent_type="customer")
    with pytest.raises(DifyDslMalformed):
        map_dsl_to_config("- just\n- a\n- list\n", agent_type="customer")
    with pytest.raises(DifyDslMalformed):
        map_dsl_to_config(REAL_DSL.replace("kind: app", "kind: workflow"), agent_type="customer")
    with pytest.raises(DifyDslMalformed, match="model_config"):
        map_dsl_to_config("version: 0.7.0\nkind: app\n", agent_type="customer")


def test_unknown_agent_type_or_missing_model_name_is_rejected() -> None:
    with pytest.raises(DifyDslMalformed, match="output contract"):
        map_dsl_to_config(REAL_DSL, agent_type="marketing")
    broken = REAL_DSL.replace("name: deepseek-v4-flash", "name: null")
    with pytest.raises(DifyDslMalformed, match="model"):
        map_dsl_to_config(broken, agent_type="customer")


def _with_dataset(entries: str) -> str:
    return REAL_DSL.replace(
        "  dataset_configs:\n    retrieval_model: multiple\n",
        "  dataset_configs:\n    retrieval_model: multiple\n    datasets:\n      datasets:\n" + entries,
    )


def test_the_dsls_dataset_ids_are_never_taken_as_our_knowledge_bindings() -> None:
    """Dify's dataset UUID and our knowledge-base id are different id spaces.

    On 41 the app bound to the live tenant declares Dify's ``df0f1d4a-…`` external
    dataset. Copying that into ``knowledge_base_ids`` made every ``admin pull-dify``
    for a bound app fail at the publish gate with ``知识库 df0f1d4a-… 不存在`` — and
    a caller that handed the value straight to a store would instead have published
    a knowledge base that cannot be resolved.
    """
    dsl = _with_dataset(
        "      - dataset:\n          enabled: true\n          id: df0f1d4a-3d6e-404e-9656-c88da6df3d1e\n"
    )
    # Not stated by the caller ⇒ refuse, rather than carry the Dify id forward.
    with pytest.raises(DifyDslMalformed, match="id 空间"):
        map_dsl_to_config(dsl, agent_type="customer")


def test_an_explicit_knowledge_binding_wins_and_the_dsls_dataset_does_not_leak_in() -> None:
    dsl = _with_dataset(
        "      - dataset:\n"
        "          enabled: true\n"
        "          id: df0f1d4a-3d6e-404e-9656-c88da6df3d1e\n"
        "      - dataset:\n"
        "          enabled: false\n"
        "          id: 4f4bc674ad8911f1ae704bfc8c544ea6\n"
    )
    config = map_dsl_to_config(
        dsl,
        agent_type="customer",
        knowledge_base_ids=("8701c742b28111f18637d95f7710e3a3",),
    )
    assert config.knowledge_base_ids == ("8701c742b28111f18637d95f7710e3a3",)
    # An explicit empty tuple is a real intent ("this agent has no knowledge base"),
    # and must be honoured as stated rather than treated as "not given".
    assert map_dsl_to_config(dsl, agent_type="customer", knowledge_base_ids=()).knowledge_base_ids == ()


def test_a_dsl_with_no_dataset_maps_to_no_knowledge_binding() -> None:
    """Both real 36 fixtures carry `dataset_configs` with only `retrieval_model`."""
    assert map_dsl_to_config(REAL_DSL, agent_type="customer").knowledge_base_ids == ()
    disabled = _with_dataset(
        "      - dataset:\n          enabled: false\n          id: df0f1d4a-3d6e-404e-9656-c88da6df3d1e\n"
    )
    assert map_dsl_to_config(disabled, agent_type="customer").knowledge_base_ids == ()


def test_a_present_but_unreadable_dataset_binding_is_an_error_not_an_empty_tuple() -> None:
    """Dropping a binding the operator configured is worse than refusing to map."""
    with pytest.raises(DifyDslMalformed, match="no id"):
        map_dsl_to_config(_with_dataset("      - dataset:\n          enabled: true\n"), agent_type="customer")
    with pytest.raises(DifyDslMalformed):
        map_dsl_to_config(_with_dataset("      - enabled: true\n"), agent_type="customer")
    with pytest.raises(DifyDslMalformed):
        map_dsl_to_config(
            REAL_DSL.replace(
                "    retrieval_model: multiple\n", "    retrieval_model: multiple\n    datasets: nope\n"
            ),
            agent_type="customer",
        )


# --- the pull client ----------------------------------------------------------


def test_client_requests_the_console_export_route_with_bearer_and_workspace(monkeypatch) -> None:
    seen = _serve(monkeypatch, body=_export_body())
    assert DifyDslClient(_source()).export_dsl() == REAL_DSL
    assert seen["url"] == (
        "http://127.0.0.1:10008/console/api/apps/a975c8e5-ad9e-425c-84c9-77581a0a2bed/export"
    )
    assert seen["headers"]["authorization"] == "Bearer dsl-pull-controlled-key"
    assert seen["headers"]["x-workspace-id"] == "ws-1"


def test_client_rejects_a_bad_source_before_any_request(monkeypatch) -> None:
    seen = _serve(monkeypatch, body=_export_body())
    for override in (
        {"base_url": "ftp://127.0.0.1:10008"},
        {"base_url": "http://user:pass@127.0.0.1:10008"},
        {"base_url": "http://127.0.0.1:10008/#frag"},
        {"app_id": "../escape"},
        {"api_key": ""},
        {"timeout": 0.1},
    ):
        with pytest.raises(ValueError):
            DifyDslClient(_source(**override))
    assert "url" not in seen


def test_source_repr_never_carries_the_credential() -> None:
    assert "dsl-pull-controlled-key" not in repr(_source())
    assert DifyDslSource.__dataclass_fields__["api_key"].repr is False


def test_unreachable_dify_is_its_own_signal(monkeypatch) -> None:
    import urllib.error

    _serve(monkeypatch, error=urllib.error.URLError("connection refused"))
    with pytest.raises(DifyDslUnreachable):
        DifyDslClient(_source()).export_dsl()


def test_rejected_credential_is_its_own_signal_not_an_outage(monkeypatch) -> None:
    import urllib.error

    error = urllib.error.HTTPError(
        url="http://127.0.0.1:10008",
        code=401,
        msg="unauthorized",
        hdrs=None,
        fp=None,
    )
    error.read = lambda: b'{"code":"unauthorized","message":"Access token is invalid"}'
    _serve(monkeypatch, error=error)
    with pytest.raises(DifyDslAuthRejected, match="401"):
        DifyDslClient(_source()).export_dsl()


def test_an_envelope_without_the_dsl_is_a_clear_failure(monkeypatch) -> None:
    _serve(monkeypatch, payload={"data": ""})
    with pytest.raises(DifyDslMalformed):
        DifyDslClient(_source()).export_dsl()
    _serve(monkeypatch, body="not json at all")
    with pytest.raises(DifyDslMalformed):
        DifyDslClient(_source()).export_dsl()


# --- landing the draft --------------------------------------------------------


def test_pull_lands_a_draft_that_is_not_published(monkeypatch, tmp_path: Path) -> None:
    _serve(monkeypatch, body=_export_body())
    manager = _manager(tmp_path)
    admin = _context()

    agent = pull_agent_draft(
        manager, admin, _source(), name="小趋-客服", description="从 Dify 拉取", agent_type="customer"
    )

    assert agent.status == "draft"
    assert agent.revision == 1
    assert agent.published_version is None
    assert agent.config.model == "deepseek-v4-flash"
    assert "小趋" in agent.config.prompt
    with pytest.raises(AgentNotFound):  # nothing was published
        manager.version(admin, agent.agent_id, 1)


def test_no_half_built_draft_when_the_pull_fails(monkeypatch, tmp_path: Path) -> None:
    """The pull, the parse and the map all finish before the first write."""
    manager = _manager(tmp_path)
    admin = _context()

    _serve(monkeypatch, body=_export_body(NULL_PROMPT_DSL))
    with pytest.raises(DifyDslMalformed):
        pull_agent_draft(manager, admin, _source(), name="小趋-客服", description="", agent_type="customer")
    assert manager.list(admin) == []

    import urllib.error

    monkeypatch.undo()
    _serve(monkeypatch, error=urllib.error.URLError("down"))
    with pytest.raises(DifyDslUnreachable):
        pull_agent_draft(manager, admin, _source(), name="小趋-客服", description="", agent_type="customer")
    assert manager.list(admin) == []


def test_repulling_converges_in_place_and_forks_a_published_agent(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    admin = _context()
    config = map_dsl_to_config(REAL_DSL, agent_type="customer")

    created = converge_agent_draft(manager, admin, config, name="小趋-客服", description="d")
    again = converge_agent_draft(manager, admin, config, name="小趋-客服", description="d")
    assert again.revision == created.revision  # nothing changed, so nothing written
    assert len(manager.list(admin)) == 1

    published = manager.publish(admin, created.agent_id, expected_revision=created.revision)
    assert published.version_no == 1

    forked = converge_agent_draft(manager, admin, config, name="小趋-客服", description="d")
    assert forked.status == "draft"
    assert forked.agent_id == created.agent_id
    assert manager.version(admin, created.agent_id, 1).snapshot["prompt"] == config.prompt
    assert len(manager.list(admin)) == 1


def test_repull_after_a_prompt_edit_updates_the_draft_and_keeps_versions_immutable(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    admin = _context()
    config = map_dsl_to_config(REAL_DSL, agent_type="customer")
    agent = converge_agent_draft(manager, admin, config, name="小趋-客服", description="d")
    manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
    current = manager.get(admin, agent.agent_id)

    edited = map_dsl_to_config(
        REAL_DSL.replace("知识库内容是唯一事实来源", "知识库内容是唯一权威事实来源"),
        agent_type="customer",
    )
    updated = converge_agent_draft(manager, admin, edited, name="小趋-客服", description="d")

    assert updated.revision > current.revision
    assert "唯一权威事实来源" in updated.config.prompt
    assert "唯一权威事实来源" not in manager.version(admin, agent.agent_id, 1).snapshot["prompt"]


def test_a_pull_that_states_no_knowledge_base_keeps_the_existing_one(monkeypatch, tmp_path: Path) -> None:
    """「这次拉取没提知识库」≠「这个 agent 没有知识库」。

    dataset-free 的 app（生产那个现在就是）拉回来时 `knowledge_base_ids` 是空的；
    若直接落库，线上客服会被悄悄摘掉 RAG 路径 —— 而命令照样退 0。这与 #739 在
    prompt 上挡的**是同一种静默覆盖**。
    """
    _serve(monkeypatch, body=_export_body())  # REAL_DSL: 无 dataset
    manager = _manager(tmp_path)
    admin = _context()

    seeded = manager.create(
        admin,
        name="小趋-客服",
        description="d",
        config=map_dsl_to_config(REAL_DSL, agent_type="customer", knowledge_base_ids=("kb-live",)),
    )
    manager.publish(admin, seeded.agent_id, expected_revision=seeded.revision)

    pulled = pull_agent_draft(
        manager, admin, _source(), name="小趋-客服", description="d", agent_type="customer"
    )
    assert pulled.config.knowledge_base_ids == ("kb-live",)

    # 显式给了空 tuple ⇒ 那才是「就是要它没有知识库」，按字面采纳。
    explicit = pull_agent_draft(
        manager,
        admin,
        _source(),
        name="小趋-客服",
        description="d",
        agent_type="customer",
        knowledge_base_ids=(),
    )
    assert explicit.config.knowledge_base_ids == ()


# --- 知识库桥：dataset UUID → 操作员的句柄 → 我们的知识库 id（PR-2）------------


class _FakeConsole:
    """A console whose per-dataset route answers with the handle we choose."""

    def __init__(self, handles: dict[str, str | None]) -> None:
        self.handles = handles
        self.asked: list[str] = []

    def dataset_knowledge_id(self, dataset_id: str) -> str:
        self.asked.append(dataset_id)
        handle = self.handles.get(dataset_id)
        if handle is None:
            raise DifyDslMalformed(f"Dify dataset {dataset_id} 没有 external_knowledge_id")
        return handle


class _Binding:
    """Stand-in for gateway_config.DifyKnowledgeBinding (structural on purpose)."""

    def __init__(self, tenant_id: str, kbs: tuple[str, ...]) -> None:
        self.tenant_id = tenant_id
        self.knowledge_base_ids = kbs


def _with_external_dataset(dataset_id: str = "df0f1d4a-3d6e-404e-9656-c88da6df3d1e") -> str:
    return _with_dataset(f"      - dataset:\n          enabled: true\n          id: {dataset_id}\n")


def test_a_declared_dataset_is_translated_through_the_registry() -> None:
    """**桥的作用**：Dify 的 dataset UUID 经句柄翻译成**我们的**知识库 id。"""
    from aiops_diagnostics.dify_dsl_pull import _resolve_declared_datasets

    client = _FakeConsole({"df0f1d4a-x": "kb-canary"})
    registry = {"kb-canary": _Binding("T-1", ("kb-aaa", "kb-bbb"))}
    got = _resolve_declared_datasets(("df0f1d4a-x",), registry, client, tenant_id="T-1")
    assert got == ("kb-aaa", "kb-bbb")
    assert client.asked == ["df0f1d4a-x"], "必须真的去问控制台要句柄"


def test_an_unregistered_handle_is_refused_not_guessed() -> None:
    """句柄不在登记表 ⇒ **拒**（绝不猜它属于哪个租户）。"""
    from aiops_diagnostics.dify_dsl_pull import _resolve_declared_datasets

    client = _FakeConsole({"df-x": "kb-nobody-registered"})
    with pytest.raises(DifyDslMalformed, match="没有它|登记"):
        _resolve_declared_datasets(("df-x",), {}, client, tenant_id="T-1")


def test_a_dataset_registered_to_another_tenant_is_refused() -> None:
    """登记行属于**另一个租户** ⇒ 拒（防把别人的知识库挂上来）。"""
    from aiops_diagnostics.dify_dsl_pull import _resolve_declared_datasets

    client = _FakeConsole({"df-x": "kb-canary"})
    registry = {"kb-canary": _Binding("T-OTHER", ("kb-aaa",))}
    with pytest.raises(DifyDslMalformed, match="另一个租户|租户"):
        _resolve_declared_datasets(("df-x",), registry, client, tenant_id="T-1")


def test_a_native_dataset_is_refused() -> None:
    """原生 dataset（没有句柄）⇒ 拒，且消息点明原因。"""
    from aiops_diagnostics.dify_dsl_pull import _resolve_declared_datasets

    client = _FakeConsole({})  # 任何 id 都拿不到句柄
    with pytest.raises(DifyDslMalformed):
        _resolve_declared_datasets(("df-native",), {}, client, tenant_id="T-1")


def test_the_mapper_calls_the_resolver_only_when_a_dataset_is_declared() -> None:
    """**没有声明 dataset ⇒ 完全不碰解析器**（"没声明"与"声明了"必须可区分）。"""
    from aiops_diagnostics.dify_dsl_pull import map_dsl_to_config

    calls: list[tuple[str, ...]] = []

    def resolver(ids: tuple[str, ...]) -> tuple[str, ...]:
        calls.append(ids)
        return ("kb-x",)

    config = map_dsl_to_config(REAL_DSL, agent_type="customer", dataset_resolver=resolver)
    assert config.knowledge_base_ids == ()
    assert calls == [], "DSL 没声明 dataset，解析器不该被调用"

    config = map_dsl_to_config(_with_external_dataset(), agent_type="customer", dataset_resolver=resolver)
    assert config.knowledge_base_ids == ("kb-x",)
    assert calls == [("df0f1d4a-3d6e-404e-9656-c88da6df3d1e",)]


def test_a_declared_dataset_with_no_resolver_still_refuses() -> None:
    """**不给解析器**（旧行为）⇒ 声明了 dataset 仍旧拒，绝不静默搬运 UUID。"""
    from aiops_diagnostics.dify_dsl_pull import map_dsl_to_config

    with pytest.raises(DifyDslMalformed, match="id 空间"):
        map_dsl_to_config(_with_external_dataset(), agent_type="customer")


def test_an_app_that_declares_no_tools_leaves_the_default_in_place() -> None:
    """没声明工具 ⇒ 不加约束（dataclass 默认 = 全部）。存量 app 全是这一支。"""
    from aiops_diagnostics.agent_contracts import ALL_TOOL_NAMES

    config = map_dsl_to_config(REAL_DSL, agent_type="customer")
    assert config.allowed_tools == ALL_TOOL_NAMES


def test_a_declared_tool_is_refused_rather_than_silently_ignored() -> None:
    """声明了工具但没人能翻译 ⇒ **拒**，不静默丢掉。

    这是本仓反复出现的形态：静默丢掉会让运营"我给这个 app 挂了工具"与线上行为
    各说各话，而且没有任何信号。实测两个 app 的 `agent_mode.tools` 都是空的，
    所以映射键**没有被发明出来**（见 `_declared_tool_names` 的 docstring）。
    """
    import yaml

    from aiops_diagnostics.dify_dsl_pull import DifyDslMalformed

    doc = yaml.safe_load(REAL_DSL)
    doc["model_config"]["agent_mode"] = {
        "enabled": True,
        "strategy": "router",
        "tools": [{"provider_type": "builtin", "provider_id": "p1", "tool_name": "order_snapshot"}],
    }
    with pytest.raises(DifyDslMalformed, match="工具"):
        map_dsl_to_config(yaml.safe_dump(doc, allow_unicode=True), agent_type="customer")


def test_a_declared_tool_goes_through_the_resolver_when_one_is_given() -> None:
    """给了翻译器就用它 —— 与知识库那条桥同形（翻译，不复制）。"""
    import yaml

    doc = yaml.safe_load(REAL_DSL)
    doc["model_config"]["agent_mode"] = {
        "enabled": True,
        "strategy": "router",
        "tools": [{"provider_type": "builtin", "provider_id": "p1", "tool_name": "mine"}],
    }
    seen: list[tuple[str, ...]] = []

    def resolver(names):
        seen.append(names)
        return ("order_snapshot",)

    config = map_dsl_to_config(
        yaml.safe_dump(doc, allow_unicode=True), agent_type="customer", tool_resolver=resolver
    )
    assert config.allowed_tools == ("order_snapshot",)
    assert seen == [("p1/mine",)], "翻译器收到的是 Dify 的身份（provider_id/tool_name）"


def test_a_pull_never_changes_the_live_model(monkeypatch, tmp_path: Path) -> None:
    """`model` 保持库中现值 —— 因为运行时**不读**它，而从 Dify 拉它会伤到发布。

    实测：真实模型来自 provider 配置（``codex_runtime`` 读 ``provider.model``）；
    `AgentConfig.model` 只有发布门与相等比较两处消费。所以从 Dify 覆盖它既不改变
    线上模型，又会在运营换了 Dify 侧模型名时让**整个发布失败**（不在 allowed_models
    里）或产生一次静默无效的改动。
    """
    _serve(monkeypatch, body=_export_body())
    manager = _manager(tmp_path)
    admin = _context()

    # 库中已有一个该名字的 agent，模型是清单那一份
    manager.create(
        admin,
        name="小趋-客服",
        description="",
        config=AgentConfig(
            agent_type="customer",
            prompt="旧正文",
            knowledge_base_ids=(),
            model="aiops-api",
            output_contract="blocks-v1",
        ),
    )
    draft = pull_agent_draft(
        manager, admin, _source(), name="小趋-客服", description="从 Dify 拉取", agent_type="customer"
    )

    assert draft.config.model == "aiops-api", "Dify 的 model.name 不该覆盖库中那份"
    assert "小趋" in draft.config.prompt, "正文仍然从 Dify 来"


def test_a_first_pull_still_takes_the_dsl_model(monkeypatch, tmp_path: Path) -> None:
    """库中还不存在时没有现值可保 ⇒ 取 DSL 的值（它要过发布门）。

    这条与上一条成对：只断言"不变"会在"从来没建过 agent"时把模型也变成空。
    """
    _serve(monkeypatch, body=_export_body())
    manager = _manager(tmp_path)
    draft = pull_agent_draft(
        manager, _context(), _source(), name="新的", description="", agent_type="customer"
    )
    assert draft.config.model == "deepseek-v4-flash"
