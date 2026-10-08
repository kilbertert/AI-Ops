from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aiops_diagnostics.agent_lifecycle import AgentManager, AgentNotFound, AgentStore
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


def test_enabled_dataset_ids_become_knowledge_bindings_and_disabled_ones_do_not() -> None:
    dsl = _with_dataset(
        "      - dataset:\n"
        "          enabled: true\n"
        "          id: 4f4bc674ad8911f1ae704bfc8c544ea6\n"
        "      - dataset:\n"
        "          enabled: false\n"
        "          id: 8701c742b28111f18637d95f7710e3a3\n"
    )
    assert map_dsl_to_config(dsl, agent_type="customer").knowledge_base_ids == (
        "4f4bc674ad8911f1ae704bfc8c544ea6",
    )


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
