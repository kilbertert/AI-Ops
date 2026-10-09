"""Runtime registry tests (#584): tenant/entry → Dify app, fail-closed.

Two seams, both existing ones:

* the **gateway HTTP face** — a real ``GatewayRuntime`` on a temp database,
  driven through ``POST /v1/assistant/questions``, exactly like
  ``test_assistant_api._real_runtime_client``. Nothing is injected to shortcut
  the "did the entry reach the gate" question: the entry the runtime keys on is
  the one the platform rule resolved, so a passing test here proves the whole
  path rather than a stub of it.
* the **convergence seam** — ``load_manifest`` + ``reconcile_dify_registry``,
  in the same style as ``test_agent_manifest.py``.

The load-bearing claims are the fail-closed ones: an unregistered pair in a
configured registry selects nothing, and it does so without answering from
another agent's library or from the zero-order path.
"""

from __future__ import annotations

import os
import textwrap
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
from aiops_diagnostics.agent_manifest import (
    EnvironmentManifest,
    ManifestAgent,
    ManifestError,
    load_manifest,
    reconcile_dify_registry,
)
from aiops_diagnostics.dify_app_registry import (
    DifyAppBinding,
    DifyAppRegistry,
    DifyAppRegistryError,
)
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

TENANT = "T-1"
#: Routes to the generic QA branch: no FAQ hit, no order id, no dispute wording.
GENERIC_QUESTION = "为什么我的车充满电之后续航里程总是比官方标注少这么多"


# ── HTTP face ────────────────────────────────────────────────────────────


class _Caller:
    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        del token, third_session, platform_entry, source_key
        subject = SubjectRecord(b_user_id="c:C-1", c_user_id="C-1", tenant_id=TENANT)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=TENANT,
            data_scope=DataScope(type="self"),
            roles=frozenset({"ROLE_AGENT_ADMIN"}),
            permissions=frozenset({required_scope}),
        )


class _Directory:
    """A consumer-only caller: the entry it holds is the only one it has, so
    ``decision.platform`` is unambiguous for the ``consumer`` header."""

    def roles_for_c_user(self, c_user_id: str, tenant_id: str) -> tuple[PlatformRoleRecord, ...]:
        del c_user_id, tenant_id
        return (PlatformRoleRecord("B-1", "C-1", TENANT, "app"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str) -> tuple[PlatformRoleRecord, ...]:
        del b_user_id, tenant_id
        return ()


class _NeverAuthorizer:
    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        del context, order_no
        return False


class _PublishOk:
    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        del tenant_id, knowledge_base_ids


def _searching_client():
    """Retrieval wired, so ``_try_customer_rag`` is entered at all (it is only
    reached with a client and a signer).

    A real ``KbServiceClient`` subclass, because the runtime asserts
    ``isinstance`` on the client it hands to the QA harness; it is never dialled
    — the model call is stubbed at its own seam, and the fail-closed case
    returns before any search.
    """
    from aiops_diagnostics.knowledge_retrieval import KbServiceClient

    class _Searching(KbServiceClient):
        def __init__(self):
            super().__init__("http://127.0.0.1:1", tenant_id=TENANT)

        def for_tenant(self, tenant_id: str):
            return self

        def search(self, *_args, **_kwargs):
            return []

    return _Searching()


def _admin_context() -> ScopeContext:
    subject = SubjectRecord(b_user_id="B-1", tenant_id=TENANT)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=TENANT,
        data_scope=DataScope(type="self"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    )


def _customer_config(prompt: str) -> AgentConfig:
    return AgentConfig(
        agent_type="customer",
        prompt=prompt,
        knowledge_base_ids=("kb-a",),
        model="aiops-api",
        output_contract="blocks-v1",
    )


def _publish(store: AgentStore, names: tuple[str, ...]) -> None:
    manager = AgentManager(store, knowledge_resolver=_PublishOk(), allowed_models=("aiops-api",))
    admin = _admin_context()
    for name in names:
        created = manager.create(
            admin, name=name, description="", config=_customer_config(prompt=f"提示词:{name}")
        )
        manager.publish(admin, created.agent_id, expected_revision=created.revision)


def _http_client(tmp_path: Path, monkeypatch, *, published: tuple[str, ...]):
    """A TestClient over a REAL runtime, plus the list of prompts the model saw.

    ``classify_lightweight`` is silenced (routing is not what these tests are
    about, and it would otherwise be a live model call), and the model turn is
    stubbed at the seam the customer path actually calls. Everything upstream of
    that — the platform decision, the entry, the registry lookup, the agent
    selection — runs for real.
    """
    from aiops_diagnostics import gateway_runtime
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    os.chmod(settings.server_config_file, 0o600)

    store = AgentStore(settings.database_file)
    _publish(store, published)

    prompts: list[str] = []

    def _model(_question, selection, _agent_settings, **_kwargs):
        prompts.append(selection.prompt)
        return {"blocks": [{"kind": "text", "text": "答案"}], "retrieval_status": "found"}

    monkeypatch.setattr("aiops_diagnostics.qa_rag.run_customer_qa_answer", _model)
    monkeypatch.setattr(GatewayRuntime, "classify_lightweight", lambda *a, **k: None)

    def _no_zero_order(*_args, **_kwargs):
        raise AssertionError("the zero-order path was reached: the registry decision was bypassed")

    monkeypatch.setattr(gateway_runtime, "run_zero_order_answer", _no_zero_order)

    runtime = GatewayRuntime(
        GatewayStore(settings.database_file),
        settings,
        Settings(),
        kb_search_client=_searching_client(),
        media_signer=object(),
        agent_store=store,
    )
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,
        caller_resolver=_Caller(),
        order_authorizer=_NeverAuthorizer(),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app), settings, runtime, prompts


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}


def _ask(client: TestClient) -> dict:
    resp = client.post("/v1/assistant/questions", json={"question": GENERIC_QUESTION}, headers=_headers())
    assert resp.status_code == 202, resp.text
    return resp.json()


def _wait_result(client: TestClient, qa_id: str) -> dict:
    deadline = time.time() + 20
    while time.time() < deadline:
        body = client.get(f"/v1/assistant/questions/{qa_id}", headers=_headers()).json()
        if body["status"] in {"completed", "failed"}:
            return body
        threading.Event().wait(0.05)
    raise AssertionError("the QA job never reached a terminal state")


def test_unconfigured_registry_keeps_the_pre_registry_rule(tmp_path: Path, monkeypatch) -> None:
    """The gate ships inert: with no binding declared, the newest published
    agent serves, exactly as before this registry existed."""
    client, settings, runtime, prompts = _http_client(
        tmp_path,
        monkeypatch,
        published=("客服甲", "客服乙"),  # 客服乙 is newest
    )
    try:
        body = _wait_result(client, _ask(client)["qa_id"])
        assert body["status"] == "completed", body
        assert prompts == ["提示词:客服乙"], "未登记时仍是旧规则（最新已发布）"
    finally:
        runtime.shutdown()
        client.close()


def test_registered_pair_is_served_by_the_named_agent(tmp_path: Path, monkeypatch) -> None:
    """With a binding registered, the agent it names serves — not the newest."""
    client, settings, runtime, prompts = _http_client(tmp_path, monkeypatch, published=("客服甲", "客服乙"))
    try:
        DifyAppRegistry(settings.database_file).put(
            DifyAppBinding(TENANT, "consumer", "dify-app-1", "客服甲")
        )
        body = _wait_result(client, _ask(client)["qa_id"])
        assert body["status"] == "completed", body
        assert prompts == ["提示词:客服甲"], "登记后由登记表指定的 agent 服务"
    finally:
        runtime.shutdown()
        client.close()


def test_unregistered_pair_is_not_default_mapped(tmp_path: Path, monkeypatch) -> None:
    """The fail-closed core: with the registry configured, an unregistered pair
    selects nothing — no fallback to another agent, no zero-order answer."""
    client, settings, runtime, prompts = _http_client(tmp_path, monkeypatch, published=("客服甲",))
    try:
        # Configured for a DIFFERENT tenant: this environment has adopted the
        # registry while TENANT itself is unmapped.
        DifyAppRegistry(settings.database_file).put(
            DifyAppBinding("T-OTHER", "consumer", "dify-app-x", "客服甲")
        )
        body = _wait_result(client, _ask(client)["qa_id"])
        assert body["status"] == "completed", body
        result = body["result"]
        assert result["retrieval_status"] == "unavailable", (
            "未登记不是「库里没有」——什么都没检索，不能报 not_found"
        )
        assert result["blocks"], "必须是可见的终态文案，不是空结果"
        assert prompts == [], "未登记的租户不得被默认映射到任何 app"
    finally:
        runtime.shutdown()
        client.close()


def test_entry_is_part_of_the_key(tmp_path: Path, monkeypatch) -> None:
    """A binding for one entry does not serve the other entry."""
    client, settings, runtime, prompts = _http_client(tmp_path, monkeypatch, published=("客服甲",))
    try:
        DifyAppRegistry(settings.database_file).put(
            DifyAppBinding(TENANT, "operator", "dify-app-op", "客服甲")
        )
        # The caller resolves to `consumer` (it holds only a c_user identity),
        # so this pair is unmapped even though the tenant has a binding.
        body = _wait_result(client, _ask(client)["qa_id"])
        assert body["result"]["retrieval_status"] == "unavailable", (
            "入口是键的一半：operator 的登记不得服务 consumer 的请求"
        )
        assert prompts == []
    finally:
        runtime.shutdown()
        client.close()


def test_binding_naming_an_absent_agent_selects_nothing(tmp_path: Path, monkeypatch) -> None:
    """A binding that names an agent the store does not publish stays
    fail-closed at the seam — it must not degrade into "any published agent"."""
    client, settings, runtime, prompts = _http_client(tmp_path, monkeypatch, published=("客服甲",))
    try:
        DifyAppRegistry(settings.database_file).put(
            DifyAppBinding(TENANT, "consumer", "dify-app-1", "不存在的客服")
        )
        body = _wait_result(client, _ask(client)["qa_id"])
        assert body["result"]["retrieval_status"] == "unavailable"
        assert prompts == []
    finally:
        runtime.shutdown()
        client.close()


# ── convergence seam ─────────────────────────────────────────────────────


def _write_manifest(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _manifest_with_binding(agent_name: str = "客服甲", app_id: str = "dify-app-1"):
    return EnvironmentManifest(
        agents=(
            ManifestAgent(
                tenant_id=TENANT,
                name=agent_name,
                description="d",
                config=_customer_config(prompt="提示词"),
            ),
        ),
        dify_apps=(DifyAppBinding(TENANT, "consumer", app_id, agent_name),),
    )


def test_load_manifest_parses_dify_apps(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path / "env.toml",
        """
        [[agents]]
        tenant_id = "t1"
        name = "客服"
        agent_type = "customer"
        prompt = "你是客服"
        knowledge_base_ids = ["kb-1"]
        model = "aiops-api"

        [[dify_apps]]
        tenant_id = "t1"
        business_entry = "consumer"
        app_id = "dify-app-1"
        agent_name = "客服"
        """,
    )
    manifest = load_manifest(path)
    assert manifest.dify_apps == (DifyAppBinding("t1", "consumer", "dify-app-1", "客服"),)


def test_load_manifest_rejects_binding_without_declared_agent(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path / "env.toml",
        """
        [[agents]]
        tenant_id = "t1"
        name = "客服"
        agent_type = "customer"
        prompt = "你是客服"
        knowledge_base_ids = ["kb-1"]
        model = "aiops-api"

        [[dify_apps]]
        tenant_id = "t1"
        business_entry = "consumer"
        app_id = "dify-app-1"
        agent_name = "没人发布的名字"
        """,
    )
    with pytest.raises(ManifestError, match="未在"):
        load_manifest(path)


def test_load_manifest_rejects_bad_entry_and_duplicates(tmp_path: Path) -> None:
    base = """
        [[agents]]
        tenant_id = "t1"
        name = "客服"
        agent_type = "customer"
        prompt = "你是客服"
        knowledge_base_ids = ["kb-1"]
        model = "aiops-api"
    """
    bad_entry = _write_manifest(
        tmp_path / "bad-entry.toml",
        base
        + """
        [[dify_apps]]
        tenant_id = "t1"
        business_entry = "console"
        app_id = "dify-app-1"
        agent_name = "客服"
        """,
    )
    with pytest.raises(ManifestError, match="business_entry"):
        load_manifest(bad_entry)

    duplicate = _write_manifest(
        tmp_path / "dup.toml",
        base
        + """
        [[dify_apps]]
        tenant_id = "t1"
        business_entry = "consumer"
        app_id = "dify-app-1"
        agent_name = "客服"

        [[dify_apps]]
        tenant_id = "t1"
        business_entry = "consumer"
        app_id = "dify-app-2"
        agent_name = "客服"
        """,
    )
    with pytest.raises(ManifestError, match="重复"):
        load_manifest(duplicate)


def test_reconcile_dify_registry_is_idempotent_and_reports_drift(tmp_path: Path) -> None:
    registry = DifyAppRegistry(tmp_path / "gateway.db")
    manifest = _manifest_with_binding()

    first = reconcile_dify_registry(registry, manifest)
    assert [report.action for report in first] == ["registered"]
    assert registry.lookup(TENANT, "consumer") == DifyAppBinding(TENANT, "consumer", "dify-app-1", "客服甲")

    second = reconcile_dify_registry(registry, manifest)
    assert [report.action for report in second] == ["unchanged"]

    # A changed app id is drift and converges to a new row.
    drifted = _manifest_with_binding(app_id="dify-app-2")
    assert [report.action for report in reconcile_dify_registry(registry, drifted)] == ["registered"]
    assert registry.lookup(TENANT, "consumer").app_id == "dify-app-2"


def test_reconcile_dify_registry_prune_is_opt_in(tmp_path: Path) -> None:
    registry = DifyAppRegistry(tmp_path / "gateway.db")
    registry.put(DifyAppBinding(TENANT, "operator", "dify-app-old", "客服甲"))
    manifest = _manifest_with_binding()

    reports = reconcile_dify_registry(registry, manifest)
    assert [report.action for report in reports] == ["registered"]
    assert registry.lookup(TENANT, "operator") is not None, "默认不删：多余登记保持可见"

    reports = reconcile_dify_registry(registry, manifest, prune=True)
    assert "removed" in [report.action for report in reports]
    assert registry.lookup(TENANT, "operator") is None


def test_reconcile_dify_registry_dry_run_writes_nothing(tmp_path: Path) -> None:
    registry = DifyAppRegistry(tmp_path / "gateway.db")
    reports = reconcile_dify_registry(registry, _manifest_with_binding(), dry_run=True)
    assert [report.action for report in reports] == ["registered"]
    assert registry.all() == ()


def test_registry_validation_rejects_bad_rows(tmp_path: Path) -> None:
    registry = DifyAppRegistry(tmp_path / "gateway.db")
    for binding in (
        DifyAppBinding("bad tenant", "consumer", "dify-app-1", "客服"),
        DifyAppBinding(TENANT, "console", "dify-app-1", "客服"),
        DifyAppBinding(TENANT, "consumer", "", "客服"),
        DifyAppBinding(TENANT, "consumer", "dify-app-1", "  "),
    ):
        with pytest.raises(DifyAppRegistryError):
            registry.put(binding)
    assert registry.all() == ()
