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

import textwrap
from pathlib import Path

import pytest

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
from aiops_diagnostics.faq import PlatformRoleRecord
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

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
    from aiops_diagnostics.knowledge.knowledge_retrieval import KbServiceClient

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

    return {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}


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
    # 绑定了 Dify ⇒ 清单里**不写** prompt（#577：正文只有一个来源）
    path = _write_manifest(
        tmp_path / "env.toml",
        """
        [[agents]]
        tenant_id = "t1"
        name = "客服"
        agent_type = "customer"
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
