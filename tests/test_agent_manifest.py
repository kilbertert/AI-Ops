from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore
from aiops_diagnostics.agent_manifest import (
    EnvironmentManifest,
    ManifestAgent,
    ManifestError,
    load_manifest,
    reconcile,
)
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


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


def _manifest_agent(
    *,
    tenant_id: str = "tenant-a",
    name: str = "客服助手",
    prompt: str = "回答必须引用已授权的业务资料。",
    model: str = "aiops-api",
    state: str = "published",
    knowledge_base_ids: tuple[str, ...] = ("kb-a",),
) -> ManifestAgent:
    from aiops_diagnostics.agent_lifecycle import AgentConfig

    return ManifestAgent(
        tenant_id=tenant_id,
        name=name,
        description="d",
        config=AgentConfig(
            agent_type="customer",
            prompt=prompt,
            knowledge_base_ids=knowledge_base_ids,
            model=model,
            output_contract="blocks-v1",
        ),
        state=state,
    )


def _manifest(*agents: ManifestAgent) -> EnvironmentManifest:
    return EnvironmentManifest(agents=agents)


def _manager(tmp_path: Path, *, knowledge=None) -> AgentManager:
    return AgentManager(
        AgentStore(tmp_path / "gateway.db"),
        knowledge_resolver=knowledge or _Knowledge(),
        allowed_models=("aiops-api", "qwen3.8-max-0902"),
    )


def _actions(reports) -> list[str]:
    return [report.action for report in reports]


def test_load_manifest_defaults_and_required_fields(tmp_path: Path) -> None:
    path = tmp_path / "env.toml"
    path.write_text(
        textwrap.dedent(
            """
            [[agents]]
            tenant_id = "t1"
            name = "客服"
            description = "描述"
            agent_type = "customer"
            prompt = "你是客服"
            knowledge_base_ids = ["kb-1"]
            model = "aiops-api"
            """
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(path)
    assert len(manifest.agents) == 1
    agent = manifest.agents[0]
    assert agent.config.output_contract == "blocks-v1", "customer 默认 blocks-v1"
    assert agent.state == "published"

    path.write_text(
        textwrap.dedent(
            """
            [[agents]]
            tenant_id = "t1"
            name = "缺 prompt"
            agent_type = "customer"
            model = "aiops-api"
            state = "ghost"
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_reconcile_creates_and_publishes_new_agent(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    knowledge = manager.knowledge_resolver
    reports = reconcile(manager, _manifest(_manifest_agent()))
    assert _actions(reports) == ["created"]
    assert reports[0].version == 1
    assert knowledge.calls == [("tenant-a", ("kb-a",))], "发布前 KB 活性校验必须真实调用"
    store_agents = manager.list(_context())
    assert store_agents[0].status == "published"
    assert store_agents[0].published_version == 1


def test_reconcile_is_idempotent(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    reconcile(manager, _manifest(_manifest_agent()))
    second = reconcile(manager, _manifest(_manifest_agent()))
    assert _actions(second) == ["unchanged"]
    assert second[0].version == 1, "不产生新的发布版本"
    assert manager.list(_context())[0].published_version == 1


def test_reconcile_converges_published_drift(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    reconcile(manager, _manifest(_manifest_agent()))
    drifted = _manifest(_manifest_agent(prompt="新的业务提示词。"))
    reports = reconcile(manager, drifted)
    assert _actions(reports) == ["updated"]
    assert reports[0].version == 2, "漂移收敛产生不可变新版本"
    agent = manager.list(_context())[0]
    assert agent.config.prompt == "新的业务提示词。"


def test_reconcile_converges_draft(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    # 先建 draft 不发布
    context = _context()
    manager.create(context, name="客服助手", description="d", config=_manifest_agent().config)
    reports = reconcile(manager, _manifest(_manifest_agent()))
    assert _actions(reports) == ["published"], "配置一致的 draft 直接发布"
    # draft 漂移场景
    current = manager.list(context)[0]
    manager.fork_draft(context, current.agent_id, expected_revision=current.revision)
    drifted = _manifest(_manifest_agent(prompt="draft 阶段的新提示词。"))
    reports = reconcile(manager, drifted)
    assert _actions(reports) == ["updated"]
    assert manager.list(context)[0].config.prompt == "draft 阶段的新提示词。"


def test_reconcile_rejects_unknown_model_before_writing(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(ManifestError, match="不在白名单"):
        reconcile(manager, _manifest(_manifest_agent(model="gpt-4o")))
    # 零写入断言：库里什么都没有
    assert manager.list(_context()) == []


def test_reconcile_disabled_target_reports_manual_action(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    context = _context()
    reconcile(manager, _manifest(_manifest_agent()))
    published = manager.list(context)[0]
    manager.disable(context, published.agent_id, expected_revision=published.revision)
    reports = reconcile(manager, _manifest(_manifest_agent()))
    assert _actions(reports) == ["manual-action-required"]
    assert manager.list(context)[0].status == "disabled", "不动库"


def test_reconcile_prune_scoped_to_manifest_tenants(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    context_a, context_b = _context("tenant-a"), _context("tenant-b")
    # tenant-a: 清单内 + 一个多余 published;tenant-b: 一个清单外 agent
    reconcile(manager, _manifest(_manifest_agent()))
    manager.create(context_a, name="多余的", description="d", config=_manifest_agent().config)
    extra_a = [a for a in manager.list(context_a) if a.name == "多余的"][0]
    manager.publish(context_a, extra_a.agent_id, expected_revision=extra_a.revision)
    agent_b = _manifest_agent(tenant_id="tenant-b")
    manager.create(context_b, name="别动我", description="d", config=agent_b.config)

    reports = reconcile(manager, _manifest(_manifest_agent()), prune=True)
    assert "pruned-disabled" in _actions(reports)
    names_a = {a.name: a.status for a in manager.list(context_a)}
    assert names_a == {"客服助手": "published", "多余的": "disabled"}
    names_b = {a.name: a.status for a in manager.list(context_b)}
    assert names_b == {"别动我": "draft"}, "manifest 外租户绝不被 prune 触碰"


def test_reconcile_disabled_state_disables_existing(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    reconcile(manager, _manifest(_manifest_agent()))
    reports = reconcile(manager, _manifest(_manifest_agent(state="disabled")))
    assert _actions(reports) == ["disabled"]
    assert manager.list(_context())[0].status == "disabled"


def test_reconcile_dry_run_writes_nothing(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    reports = reconcile(manager, _manifest(_manifest_agent()), dry_run=True)
    assert _actions(reports) == ["created"]
    assert all(report.note == "dry-run" for report in reports)
    assert manager.list(_context()) == [], "dry-run 零 mutation"


def test_allowed_models_from_settings_single_derivation() -> None:
    """gateway HTTP 面与 admin reconcile 共用的白名单推导：providers 优先、
    agent.model 兜底、最终 aiops-api——防止两处推导漂移（评审发现 #4）。"""
    from types import SimpleNamespace

    from aiops_diagnostics.agent_lifecycle import allowed_models_from_settings

    providers = SimpleNamespace(providers=[SimpleNamespace(model="qwen3.8-max"), SimpleNamespace(model="")])
    assert allowed_models_from_settings(SimpleNamespace(agent=providers)) == ("qwen3.8-max",)
    fallback = SimpleNamespace(providers=[], model="agent-default")
    assert allowed_models_from_settings(SimpleNamespace(agent=fallback)) == ("agent-default",)
    assert allowed_models_from_settings(SimpleNamespace(agent=None)) == ("aiops-api",)
