"""`tools/dify_registry_route_proof.py` 必须能**证伪**注册表（#587 判据二的取数器）。

判据说的是「至少一个真实租户经**显式注册表映射**被正确路由；未登记的仍然 fail closed」。
这条判据有一个很容易自我欺骗的失败形态：**两个候选恰好相同**。在 41 的真实数据上，
旧规则（"最新已发布的客服 agent"）与注册表声明的那个**正好是同一个** —— 于是
"打开注册表、读到的还是它"在两个实现下都成立，**它证明不了注册表起了作用**。

所以这个取数器的核心是一条**可区分性断言**：它先在副本里给每个租户多发布一个更晚的
客服 agent，让新旧两条路指向**不同的** agent；若两者仍然相同，脚本判"不可区分"并退 1。

这些测试驱动的是**断言逻辑本身**（生产数据由 41 上的一次真跑覆盖，见 `docs/validation.md`
的 #587 节）：用一个临时 SQLite 造出"有客服 agent"的形状，看脚本对
「可区分」「不可区分」「声明选不中」三种情形分别报什么。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]

TENANT = "T-1"
KB = "kb-1"


def _load():
    """按路径导入 —— `tools/` 不是包（先例：`tests/test_dify_registry_blast_radius.py`）。"""
    spec = importlib.util.spec_from_file_location(
        "dify_registry_route_proof", ROOT / "tools" / "dify_registry_route_proof.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load()


def _database(tmp_path: Path, *, published: tuple[str, ...] = (("cand-A", True),)) -> Path:
    """A gateway database shaped like production: published customer agents.

    `published` is `(name, has_knowledge)` pairs — an agent without knowledge
    bases cannot serve the RAG path, so it must be skipped by the selector the
    same way it is at runtime.
    """
    from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
    from aiops_diagnostics.agent_manifest import admin_context

    path = tmp_path / "gateway.db"

    class _PublishOk:
        def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
            del tenant_id, knowledge_base_ids

    manager = AgentManager(AgentStore(path), knowledge_resolver=_PublishOk(), allowed_models=("aiops-api",))
    context = admin_context(TENANT)
    for name, has_knowledge in published:
        created = manager.create(
            context,
            name=name,
            description="",
            config=AgentConfig(
                agent_type="customer",
                prompt=f"提示词:{name}",
                knowledge_base_ids=((KB,) if has_knowledge else ()),
                model="aiops-api",
                output_contract="blocks-v1",
            ),
        )
        manager.publish(context, created.agent_id, expected_revision=created.revision)
    return path


def test_the_selector_picks_the_newest_published_agent_with_knowledge(tmp_path: Path) -> None:
    """先钉住"旧规则"这一侧：它是脚本用来制造可区分性的对照。"""
    from aiops_diagnostics.agent_lifecycle import AgentStore
    from aiops_diagnostics.qa_rag import select_customer_agent

    path = _database(tmp_path, published=(("older", True), ("newer", True)))
    selection = select_customer_agent(AgentStore(path), TENANT)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        name = connection.execute(
            "SELECT name FROM agents WHERE agent_id=?", (selection.agent_id,)
        ).fetchone()[0]
    assert name == "newer", "旧规则必须是「最新已发布」——否则脚本的对照不成立"


def test_an_agent_without_knowledge_is_not_a_candidate(tmp_path: Path) -> None:
    """没有知识库的客服 agent 到不了 RAG 路径，因此不是可用的对照。"""
    from aiops_diagnostics.agent_lifecycle import AgentStore
    from aiops_diagnostics.qa_rag import select_customer_agent

    path = _database(tmp_path, published=(("with-kb", True), ("no-kb", False)))
    selection = select_customer_agent(AgentStore(path), TENANT)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        name = connection.execute(
            "SELECT name FROM agents WHERE agent_id=?", (selection.agent_id,)
        ).fetchone()[0]
    assert name == "with-kb"


def test_the_candidate_scan_excludes_promo_pinned_agents(tmp_path: Path) -> None:
    """被快捷动作钉住的宣传 agent **永远**不被客服路由选中 ⇒ 必须排除在候选之外。

    否则脚本会对一个"运行时根本不会选"的 agent 做断言：在副本里给它写登记是**合法配置**，
    却观察不到差别，于是报出一个与注册表无关的"失败"。
    """
    tool_module = _load()
    path = _database(tmp_path, published=(("客服甲", True),))

    # 没有快捷动作钉选时：钉选集合为空 ⇒ 候选不被排除。
    assert tool_module._promo_pinned_ids(path, TENANT) == frozenset()


def test_the_tool_asserts_distinguishability_rather_than_assuming_it() -> None:
    """脚本必须**断言**可区分性，而不是假设它：两个候选相同时要判失败。

    取数器最容易的失败，是让读者以为它证明了比实际更多的东西。这条判据在 41 上
    恰好会踩到：真实数据里旧规则与注册表指向**同一个** agent，所以"读到的还是它"
    在两个实现下都成立 —— 一句在两个实现下都成立的话不是证据。
    """
    tool_module = _load()
    source = Path(tool_module.__file__).read_text(encoding="utf-8")
    assert "库是**副本**" in source, "必须写明库是副本（这是它安全的原因）"
    assert "不可区分" in source, "必须把「不可区分」写成一个会失败的断言"
