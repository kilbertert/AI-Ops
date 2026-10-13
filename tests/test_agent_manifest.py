from __future__ import annotations

import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore
from aiops_diagnostics.agent_manifest import (
    _PROMPT_FROM_DIFY,
    EnvironmentManifest,
    ManifestAgent,
    ManifestError,
    load_manifest,
    prompt_from_dify,
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


# --- #577：一个 agent 的提示词只能有一个来源 ---------------------------------


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "env.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_a_bound_agent_may_omit_its_prompt(tmp_path: Path) -> None:
    """绑定了 Dify 的 agent **可以不写 prompt** —— 提示词由 Dify 那一侧编辑（#577）。

    这是「Dify 是权威编辑面」在清单格式上的落点：清单继续声明**运行时身份**
    （租户、名字、模型、契约、知识库），而**正文**不在这里。
    """
    manifest = load_manifest(
        _write(
            tmp_path,
            """
            [[agents]]
            tenant_id = "t1"
            name = "客服"
            agent_type = "customer"
            model = "aiops-api"

            [[dify_apps]]
            tenant_id = "t1"
            business_entry = "consumer"
            app_id = "app-1"
            agent_name = "客服"
            """,
        )
    )
    assert len(manifest.agents) == 1
    assert prompt_from_dify(manifest.agents[0]), "缺 prompt = 由 Dify 提供"


def test_a_bound_agent_may_not_also_carry_a_prompt(tmp_path: Path) -> None:
    """**两个权威**必须被拒 —— 这是本票加的那道门。

    实测过的漂移：某条绑定指向 Dify 上的 129 字存根，而 reconcile 发布的是清单里的
    758 字；线上到底用哪一份**没有规则**。允许这种状态，等于允许一份只有专门去查
    才看得见的漂移。
    """
    with pytest.raises(ManifestError, match="两个权威"):
        load_manifest(
            _write(
                tmp_path,
                """
                [[agents]]
                tenant_id = "t1"
                name = "客服"
                agent_type = "customer"
                model = "aiops-api"
                prompt = "清单里的正文"

                [[dify_apps]]
                tenant_id = "t1"
                business_entry = "consumer"
                app_id = "app-1"
                agent_name = "客服"
                """,
            )
        )


def test_an_unbound_agent_still_requires_its_prompt(tmp_path: Path) -> None:
    """没有 Dify 绑定的 agent **必须**写 prompt —— 否则它一个来源都没有。

    「prompt 变可选」只对绑定了 Dify 的那些成立；不这么做，这次改动会顺手允许
    "一个既没有 Dify、也没有正文的 agent"，那是把一个真缺陷放进来。
    """
    with pytest.raises(ManifestError, match="必须有 prompt"):
        load_manifest(
            _write(
                tmp_path,
                """
                [[agents]]
                tenant_id = "t1"
                name = "客服"
                agent_type = "customer"
                model = "aiops-api"
                """,
            )
        )


def test_converge_does_not_blank_the_prompt_of_a_dify_bound_agent(tmp_path: Path) -> None:
    """**无 prompt 的收敛不得把线上提示词清掉** —— 这条是本次最关键的一条。

    没有它，删掉清单里的 ``prompt`` 会让 ``config.prompt`` 变成一个**非空哨兵**，
    与库中现值不等 ⇒ ``drift`` 恒真 ⇒ 下一次 reconcile 就把线上提示词**覆盖成那个哨兵**。
    也就是说：少了这个分支，"把正文改由 Dify 编辑"这一步会让功能一起消失。

    判据是**行为面**的：一个已发布、prompt 是"真正的提示词"的 agent，用一份
    "只绑定、不写 prompt"的清单去收敛 ⇒ 发布出来的那一版 prompt **仍是原文**。
    """
    from aiops_diagnostics.agent_manifest import EnvironmentManifest, reconcile

    manager = _manager(tmp_path)
    published = manager.create(_context(), name="客服", description="d", config=_manifest_agent().config)
    manager.publish(_context(), published.agent_id, expected_revision=published.revision)
    live = _manifest_agent().config.prompt
    assert live

    manifest = EnvironmentManifest(
        agents=(
            ManifestAgent(
                tenant_id="tenant-a",
                name="客服",
                description="d",
                config=replace(_manifest_agent().config, prompt=_PROMPT_FROM_DIFY),
            ),
        ),
    )
    reconcile(manager, manifest)
    got = next(a for a in manager.list(_context()) if a.name == "客服")
    assert got.config.prompt == live, "收敛把线上提示词覆盖了"


def test_the_shipped_environment_manifest_loads() -> None:
    """仓里那份 `ops/environments/env-41.toml` 必须**始终装得下**。

    这条不是形式检查：本票加的那道守卫会让一份"既绑定 Dify 又写 prompt"的清单
    **装载失败**，而那份清单正是部署时要 `reconcile` 的东西 —— 它坏了的后果是
    "下一次收敛直接拒绝执行"，而不是某个测试红了。仓里没有任何测试或部署脚本
    读它（实测），所以需要这一条把它钉住。
    """
    repo_root = Path(__file__).parents[1]
    manifest = load_manifest(repo_root / "ops" / "environments" / "env-41.toml")
    assert manifest.agents and manifest.dify_apps, "清单不该是空的"
    for agent in manifest.agents:
        bound = any(b.tenant_id == agent.tenant_id and b.agent_name == agent.name for b in manifest.dify_apps)
        # 正文来源必须**恰好一个**：绑定了 Dify ⇔ 清单里不写 prompt
        assert prompt_from_dify(agent) is bound, f"{agent.tenant_id}/{agent.name}: 来源不唯一"


def test_a_brand_new_dify_bound_agent_is_not_created_with_the_placeholder(tmp_path: Path) -> None:
    """**空库 + 只绑定、不写正文 ⇒ 拒绝创建**，绝不把占位符发布成线上提示词。

    评审在 #739 上逮到：`_validate_config` 只查长度（实测 `1 <= len(prompt) <= 8000`），
    所以哨兵能通过校验并被发布 —— 那个入口从此用一段 grep 出来的字面量回答用户。
    """
    from aiops_diagnostics.agent_manifest import EnvironmentManifest, reconcile

    manager = _manager(tmp_path)
    manifest = EnvironmentManifest(
        agents=(
            ManifestAgent(
                tenant_id="tenant-a",
                name="全新客服",
                description="d",
                config=replace(_manifest_agent().config, prompt=_PROMPT_FROM_DIFY),
            ),
        ),
    )
    reports = reconcile(manager, manifest)
    assert reports[0].action == "manual-action-required", reports[0]
    assert not [a for a in manager.list(_context()) if a.name == "全新客服"], "不该被创建"


def test_switching_back_to_the_manifest_while_a_binding_lives_is_refused(tmp_path: Path) -> None:
    """从「Dify 编辑」切回「清单编辑」时，库里那条旧绑定必须先清掉（评审 #739）。

    装载期只能看见**文件里**的绑定：一份"改了正文、没带绑定"的清单看起来完全合法，
    而注册表里那条旧行仍在（`prune=False` 不会清）⇒ 线上双来源继续存在。
    """
    from aiops_diagnostics.agent_manifest import reconcile_dify_registry
    from aiops_diagnostics.dify_app_registry import DifyAppBinding, DifyAppRegistry

    registry = DifyAppRegistry(tmp_path / "gateway.db")
    registry.put(DifyAppBinding("tenant-a", "consumer", "app-1", "客服助手"))
    manifest = _manifest(_manifest_agent(prompt="改回清单提供的正文"))  # 无绑定、有正文

    with pytest.raises(ManifestError, match="仍绑着 Dify app"):
        reconcile_dify_registry(registry, manifest)


def test_the_switch_back_is_allowed_once_the_binding_is_gone(tmp_path: Path) -> None:
    """对照：库里**没有**那条绑定时，同样的清单合法 —— 守卫不是"一律拒绝"。"""
    from aiops_diagnostics.agent_manifest import reconcile_dify_registry
    from aiops_diagnostics.dify_app_registry import DifyAppRegistry

    manifest = _manifest(_manifest_agent(prompt="改回清单提供的正文"))
    reports = reconcile_dify_registry(DifyAppRegistry(tmp_path / "gateway.db"), manifest)
    assert reports == []
